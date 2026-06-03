// SPDX-License-Identifier: GPL-2.0
//! scx_teddy - A BPF scheduler based on task runtime characteristics

use std::cell::RefCell;
use std::collections::HashMap;
use std::io::Write;
use std::io::BufRead;
use std::mem::MaybeUninit;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use plain::Plain;

use libbpf_rs::skel::OpenSkel;
use libbpf_rs::skel::SkelBuilder;
use libbpf_rs::MapCore;
use libbpf_rs::MapFlags;

mod classifier;
mod task_stats;
mod game_task_finder;
mod topology;

use task_stats::TaskStats;
use crate::task_stats::TaskEvent;

mod bpf_skel {
    include!(concat!(env!("OUT_DIR"), "/bpf_skel.rs"));
}

mod bpf_intf {
    include!(concat!(env!("OUT_DIR"), "/intf.rs"));
}

#[allow(clippy::wildcard_imports)]
use bpf_skel::*;

#[derive(Debug, Deserialize, Clone)]
#[serde(tag = "slice_mode")]
enum SliceConfig {
    /// slice = avg_runtime_ns + sigma * stddev_runtime_ns
    #[serde(rename = "adaptive")]
    Adaptive { slice_sigma: f64 },
    /// slice = fixed value in ns
    #[serde(rename = "fixed")]
    Fixed { slice_ns: u64 },
}

#[derive(Debug, Deserialize, Clone)]
struct ClusterSchedConfig {
    prio: i32,
    /// DSQ slot / CPU-kind binding (1-based; 1 = fastest kind). 0 (the default
    /// when omitted) means the shared DSQ — runnable on any CPU kind. A value
    /// of `k` pins the cluster's tasks to the kind-only DSQ for kind `k`.
    #[serde(default)]
    cpu_kind: u8,
    /// CPU speed preference for select_cpu: 0 = none, 1 = prefer fastest,
    /// 2 = prefer slowest. Omitted (0) lets the BPF side auto-derive it from
    /// cpu_kind when the kind is the fastest/slowest tier.
    #[serde(default)]
    cpu_prefer: u8,
    #[serde(flatten)]
    slice: SliceConfig,
}

#[derive(Debug, Deserialize)]
struct SchedConfig {
    clusters: HashMap<String, ClusterSchedConfig>,
    default: ClusterSchedConfig,
}

impl ClusterSchedConfig {
    /// Compute the slice in ns for a task given its named runtime stats.
    fn compute_slice_ns(&self, named_stats: &[(&str, f64)]) -> u64 {
        match &self.slice {
            SliceConfig::Adaptive { slice_sigma } => {
                let lookup = |name: &str| -> f64 {
                    named_stats.iter()
                        .find(|(n, _)| *n == name)
                        .map(|(_, v)| *v)
                        .unwrap_or(0.0)
                };
                let avg_ms = lookup("avg_runtime_ms");
                let cv = lookup("runtime_cv");
                let avg_ns = avg_ms * 1_000_000.0;
                let std_ns = avg_ms * cv * 1_000_000.0;
                let slice = avg_ns + slice_sigma * std_ns;
                (slice.max(1000.0)) as u64 // at least 1us
            }
            SliceConfig::Fixed { slice_ns } => *slice_ns,
        }
    }
}

#[derive(Parser, Debug)]
#[command(name = "scx_teddy")]
#[command(about = "scx_teddy - A BPF scheduler based on task runtime characteristics", long_about = None)]
struct Args {
    /// Verbose output
    #[arg(short, long, default_value_t = false)]
    verbose: bool,

    /// Data collection interval in seconds
    #[arg(short, long, default_value_t = 600)]
    collect_duration: u64,

    /// Mode: "collect" to generate event.csv, "classify" to use a trained model.
    #[arg(short, long, default_value = "collect")]
    mode: String,

    /// Minimum event count to include a task in event.csv (filter inactive tasks)
    #[arg(long, default_value_t = 2)]
    min_events: u64,

    /// Output CSV file path (collect mode)
    #[arg(short, long, default_value = "event.csv")]
    output: String,

    /// Write the CSV every collect cycle (collect mode). By default the CSV is
    /// only written once on shutdown; enable this to checkpoint each cycle so a
    /// crash or `kill -9` does not lose the run's data.
    #[arg(long, default_value_t = false)]
    csv_checkpoint: bool,

    /// Maximum total run time in seconds (collect mode). Once reached, the
    /// in-memory CSV is written out and the scheduler exits. 0 means no limit.
    #[arg(long, default_value_t = 0)]
    max_runtime: u64,

    /// Path to trained model JSON (classify mode)
    #[arg(long)]
    model: Option<String>,

    /// Path to scheduling config JSON (classify mode)
    #[arg(long)]
    config: Option<String>,
}

unsafe impl Plain for TaskEvent {}

fn thread_cpu_time() -> Duration {
    #[repr(C)]
    struct Timespec { tv_sec: i64, tv_nsec: i64 }
    const CLOCK_THREAD_CPUTIME_ID: i32 = 3;
    extern "C" {
        fn clock_gettime(clk_id: i32, tp: *mut Timespec) -> i32;
    }
    let mut ts = Timespec { tv_sec: 0, tv_nsec: 0 };
    unsafe { clock_gettime(CLOCK_THREAD_CPUTIME_ID, &mut ts); }
    Duration::new(ts.tv_sec as u64, ts.tv_nsec as u32)
}

/// Shared task statistics.
///
/// Two nested RefCells:
/// - The outer `RefCell<HashMap<...>>` lets the ring-buffer callback and
///   the main loop take turns mutating the map structure (insert / remove
///   entries). They run on the same thread, so RefCell (not a Mutex) is
///   enough — the borrows never overlap.
/// - Each entry is wrapped in `RefCell<TaskStats>` so the classify loop
///   can iterate via `iter()` (immutable borrow of the map) and STILL
///   look up another entry's `ancestor` to do a Union-Find halving step,
///   without fighting the borrow checker over a `&mut HashMap` parameter.
///   Per-entry borrow check is runtime, but the cost is tiny (a counter
///   load/store, ~1-3 ns) compared to a HashMap lookup (~100-200 ns).
type StatsMap = Rc<RefCell<HashMap<i32, RefCell<TaskStats>>>>;

/// Process-progress logger. When `--verbose` is set it opens a log file and
/// every `log!` call appends a line to it; otherwise it holds `None` and
/// `log!` is a no-op. Keeping it quiet by default leaves the terminal free
/// for the Steam-scan interactive interface on the scan thread.
struct Logger {
    file: Option<std::fs::File>,
}

impl Logger {
    /// Path of the log file written in verbose mode.
    const LOG_PATH: &'static str = "teddy.log";

    /// Create a logger. In verbose mode the log file is created (truncating
    /// any previous run); a failure to open it is reported but not fatal —
    /// the logger simply falls back to no-op.
    fn new(verbose: bool) -> Self {
        let file = if verbose {
            match std::fs::File::create(Self::LOG_PATH) {
                Ok(f) => Some(f),
                Err(e) => {
                    eprintln!("warning: cannot open {}: {e}", Self::LOG_PATH);
                    None
                }
            }
        } else {
            None
        };
        Logger { file }
    }

    /// Append one line to the log file. No-op when not in verbose mode.
    fn line(&mut self, msg: &str) {
        if let Some(f) = self.file.as_mut() {
            let _ = writeln!(f, "{}", msg);
        }
    }
}

/// Append a formatted line to a `Rc<RefCell<Logger>>`. No-op in non-verbose
/// mode. Usage mirrors `println!`: `log!(logger, "x = {}", x)`.
macro_rules! log {
    ($logger:expr, $($arg:tt)*) => {
        $logger.borrow_mut().line(&format!($($arg)*))
    };
}

// Process event received from ring buffer
fn process_event(
    data: &[u8],
    stats: &StatsMap,
    tracker: &Arc<game_task_finder::GameTracker>,
) -> i32 {
    let event = plain::from_bytes::<TaskEvent>(data).unwrap();

    // Update statistics
    let mut stats = stats.borrow_mut();

    if event.parent >= 0 {
        let initial_ancestor = if event.parent == 0 { 1 } else { event.parent };
        let cell = stats.entry(event.tid)
            .or_insert_with(|| RefCell::new(TaskStats::new(initial_ancestor)));
        cell.borrow_mut().update(event);
    } else if event.parent == -1 {
        // A task exited. Two game-detection consequences:
        //
        // 1. If the dying task IS the tracked game's PPID, the game ended
        //    for real — clear the tracker and wake the scan thread. This
        //    is the reliable signal; the alive-count path below depends on
        //    the ancestor having converged via climb_one_step and on every
        //    game-family task actually firing an exit event, neither of
        //    which is guaranteed in time.
        // 2. Otherwise, if the ancestor has converged to the tracked PPID,
        //    decrement the alive count as a fallback signal (transient
        //    cutscene drops still go through here).
        let tracked = tracker.game_ppid.load(Ordering::Acquire);
        if tracked != 0 && event.tid == tracked {
            tracker.clear();
            tracker.signal_wake();
        }
        if let Some(cell) = stats.get(&event.tid) {
            let mut ts = cell.borrow_mut();
            ts.exit = 1;
            tracker.note_process_exit(ts.ancestor);
        }
    }

    0
}

fn csv_header() -> String {
    let feature_names = TaskStats::get_feature_names();
    let mut header = String::from("tid,tgid,ancestor,comm");
    for name in &feature_names {
        header.push(',');
        header.push_str(name);
    }
    header
}

/// Read a field (e.g. "Tgid", "PPid") from /proc/<tid>/status.
fn read_proc_field(tid: i32, field: &str) -> Option<i32> {
    let path = format!("/proc/{}/status", tid);
    let content = std::fs::read_to_string(path).ok()?;
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix(field) {
            if let Some(val) = rest.strip_prefix(':') {
                return val.trim().parse().ok();
            }
        }
    }
    None
}

/// Read command name from /proc/<tid>/comm.
fn read_proc_comm(tid: i32) -> String {
    let path = format!("/proc/{}/comm", tid);
    std::fs::read_to_string(path)
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// Format one task's stats into a CSV row. `ancestor` is the Union-Find
/// root from `climb_to_root` (1 = not game, or the tracked game PPID), not
/// the real parent — so it comes from TaskStats, not /proc.
fn task_csv_row(tid: i32, task_stats: &TaskStats) -> String {
    let tgid = read_proc_field(tid, "Tgid")
        .map(|v| v.to_string()).unwrap_or_default();
    let comm = read_proc_comm(tid);
    let values: Vec<String> = task_stats.get_stats().iter()
        .map(|v| format!("{}", v)).collect();
    format!("{},{},{},{},{}", tid, tgid, task_stats.ancestor, comm, values.join(","))
}

/// Write `rows` to a fresh CSV at `path`, header first. The output path is
/// checked for non-existence at startup, so this is a plain write — no merge
/// with any prior file. Returns the number of rows written.
fn write_csv(path: &str, rows: &[(i32, String)]) -> Result<usize> {
    let mut file = std::fs::File::create(path)
        .context("Failed to create output CSV")?;
    writeln!(file, "{}", csv_header())
        .context("Failed to write CSV header")?;
    for (_, row) in rows {
        writeln!(file, "{}", row)
            .context("Failed to write CSV row")?;
    }
    Ok(rows.len())
}

/// Pack every eligible task in `stats_map` into CSV rows and write them out via
/// `write_csv`. `stats_map` is the single source of truth — no buffer is kept
/// between cycles. Returns the number of rows written.
fn collect_data(
    stats_map: &HashMap<i32, RefCell<TaskStats>>,
    output: &str,
    min_events: u64,
    game_ppid: i32,
) -> Result<usize> {
    let rows: Vec<(i32, String)> = stats_map.iter()
        .filter_map(|(&tid, cell)| {
            let mut ts = cell.borrow_mut();
            if ts.exit == 0 && ts.event_count >= min_events {
                climb_to_root(&mut ts, stats_map, game_ppid);
                Some((tid, task_csv_row(tid, &ts)))
            } else {
                None
            }
        })
        .collect();
    write_csv(output, &rows)
}

/// Advance one task's Union-Find ancestor pointer by ONE halving step
/// toward the parent-chain root (init=1 or `game_ppid`).
///
/// Caller holds a `&mut TaskStats` (already borrowed from the HashMap
/// entry via `RefCell::borrow_mut`) and the `&HashMap` (immutable, from
/// the outer iter). Per-entry RefCell on HashMap values lets both coexist
/// at the borrow-checker level — the only constraint is that `ts` must
/// not be the same RefCell as `stats_map[ts.ancestor]`, otherwise the
/// inner `.borrow()` would panic. In practice that requires a task to be
/// its own ancestor, which can't happen.
///
/// Halving: `ts.ancestor = stats[ts.ancestor].ancestor`. If the
/// intermediate pid is not in `stats_map` (it never sent an event but
/// lives in the kernel), fall back to `/proc/<ancestor>/status`'s PPid.
/// /proc failure defaults to 1 (conservative: "not game").
///
/// "Already converged → skip" is the CALLER's job: when `ts.ancestor` is
/// already 1 or `game_ppid` the caller should not invoke this at all.
fn climb_one_step(
    ts: &mut TaskStats,
    stats_map: &HashMap<i32, RefCell<TaskStats>>,
    game_ppid: i32,
) {
    let a = ts.ancestor;
    let new_a = match stats_map.get(&a) {
        Some(parent_cell) => parent_cell.borrow().ancestor,
        None => read_proc_field(a, "PPid").unwrap_or(1),
    };
    // /proc can return PPid 0 for kernel-thread family (parent is
    // kthreadd / swapper). Fold to 1 so the climb has a single non-game
    // root.
    ts.ancestor = if new_a == 0 { 1 } else { new_a };
}

/// Climb `ts.ancestor` to a root (1 or `game_ppid`) in one call, for
/// `collect_data` which runs once and can't converge over cycles. The step
/// cap guards against a cycle in the ancestor pointers.
fn climb_to_root(
    ts: &mut TaskStats,
    stats_map: &HashMap<i32, RefCell<TaskStats>>,
    game_ppid: i32,
) {
    const MAX_STEPS: usize = 4096;
    for _ in 0..MAX_STEPS {
        if ts.ancestor == 1 || (game_ppid != 0 && ts.ancestor == game_ppid) {
            return;
        }
        climb_one_step(ts, stats_map, game_ppid);
    }
    ts.ancestor = 1;
}

/// Where the per-cycle classify snapshot is written for the GUI's Overall tab
/// to read. Fixed path under tmpfs; the dir is created lazily on first write.
/// This is purely a GUI feed — scx_teddy works identically with no reader.
const SNAPSHOT_DIR: &str = "/tmp/scx_teddy";
const SNAPSHOT_PATH: &str = "/tmp/scx_teddy/snapshot.json";

/// One task's scheduling state at the end of a classify cycle. Only the fields
/// the GUI can't get from /proc itself — it already reads comm/tgid/ppid live
/// and joins these in by `tid`, so we deliberately do NOT re-read /proc here
/// (no extra IO on the cycle, no duplication with the GUI's own sampling).
#[derive(Serialize)]
struct TaskSnapshot {
    tid: i32,
    cluster: usize,
    prio: i32,
    slice_ns: u64,
    cpu_kind: u8,
    cpu_prefer: u8,
}

/// Atomically publish the cycle's snapshot: write a sibling `.tmp` then rename
/// over the real path. rename(2) within a directory is atomic, so the GUI
/// (which may read at any moment) only ever sees a complete file — never a
/// half-written one. Best-effort: any IO error is logged and swallowed so a
/// missing /tmp or a full disk can never disturb the scheduler.
fn write_snapshot(tasks: &[TaskSnapshot], logger: &Rc<RefCell<Logger>>) {
    let write = || -> std::io::Result<()> {
        std::fs::create_dir_all(SNAPSHOT_DIR)?;
        let tmp = format!("{SNAPSHOT_PATH}.tmp");
        let json = serde_json::to_vec(tasks)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))?;
        std::fs::write(&tmp, &json)?;
        std::fs::rename(&tmp, SNAPSHOT_PATH)
    };
    if let Err(e) = write() {
        log!(logger, "  [snapshot] write to {} failed: {}", SNAPSHOT_PATH, e);
    }
}

/// Run one classify cycle: predict each eligible task's cluster and write the
/// resulting {prio, slice} into `update_map`. Only tasks with new data since
/// the last cycle are processed (`take_features_if_needed`).
///
/// Each task that hasn't already converged also gets ONE Union-Find
/// halving step on its `ancestor` pointer toward the game-detection root
/// (1 = init / not game, or `game_ppid` = tracked game). One cycle per
/// second means an N-deep chain converges in N cycles, which is fine.
///
/// Loop shape: thanks to per-entry `RefCell<TaskStats>` we can `iter()`
/// the map (immutable borrow of the whole HashMap) AND `borrow_mut()`
/// each entry's TaskStats AND look up another entry's ancestor at the
/// same time. Borrow check moves from the static type system into the
/// runtime counters, but the cost is negligible (~1-3 ns per borrow vs.
/// ~100-200 ns per HashMap lookup), and we get to keep the C-style
/// "task pointer + map pointer → climb" call shape.
fn run_classify_cycle(
    stats_map: &HashMap<i32, RefCell<TaskStats>>,
    update_map: &libbpf_rs::Map,
    classifier: &dyn classifier::Classifier,
    cfg: &SchedConfig,
    min_events: u64,
    tracker: &game_task_finder::GameTracker,
    logger: &Rc<RefCell<Logger>>,
) -> Result<()> {
    let n = classifier.n_clusters();
    let mut cluster_tids: Vec<Vec<i32>> = vec![Vec::new(); n];
    // GUI Overall feed: one entry per task predicted this cycle (i.e. that
    // woke at least once since last cycle). Tasks that stayed asleep aren't
    // here — the GUI leaves their tier/slice/cluster columns blank.
    let mut snapshot: Vec<TaskSnapshot> = Vec::new();

    let wall_start = Instant::now();
    let cpu_start = thread_cpu_time();
    let mut predict_count: usize = 0;

    let game_ppid = tracker.game_ppid.load(Ordering::Acquire);

    for (&tid, cell) in stats_map.iter() {
        let mut ts = cell.borrow_mut();
        if ts.exit != 0 || ts.event_count < min_events {
            continue;
        }

        // One halving step, only when not yet converged.
        if ts.ancestor != 1 && (game_ppid == 0 || ts.ancestor != game_ppid) {
            climb_one_step(&mut ts, stats_map, game_ppid);
        }

        let Some((features, named_stats)) = ts.take_features_if_needed() else {
            continue;
        };
        let cluster = classifier.predict(&features);
        predict_count += 1;
        cluster_tids[cluster].push(tid);

        let cluster_cfg = cfg.clusters
            .get(&cluster.to_string())
            .unwrap_or(&cfg.default);

        let prio = cluster_cfg.prio;
        let slice_ns = cluster_cfg.compute_slice_ns(&named_stats);

        snapshot.push(TaskSnapshot {
            tid, cluster, prio, slice_ns,
            cpu_kind: cluster_cfg.cpu_kind,
            cpu_prefer: cluster_cfg.cpu_prefer,
        });

        // Write sched_info_t {prio: s32, kind: u8, cpu_prefer: u8, slice: u64}
        // to update_map. C layout (8-byte aligned):
        //   prio[0..4] | kind[4] | cpu_prefer[5] | pad[6..8] | slice[8..16]
        let tid_key = tid.to_ne_bytes();
        let mut val_buf = [0u8; 16];
        val_buf[0..4].copy_from_slice(&prio.to_ne_bytes());
        val_buf[4] = cluster_cfg.cpu_kind;
        val_buf[5] = cluster_cfg.cpu_prefer;
        val_buf[8..16].copy_from_slice(&slice_ns.to_ne_bytes());
        update_map.update(&tid_key, &val_buf, MapFlags::ANY)?;
    }

    // Publish the snapshot for the GUI. Done before the timing log so the
    // file reflects this cycle as promptly as possible.
    write_snapshot(&snapshot, logger);

    let batch_wall_us = wall_start.elapsed().as_micros();
    let batch_cpu_us  = (thread_cpu_time() - cpu_start).as_micros();
    let avg_per_task_ns = if predict_count > 0 {
        (batch_cpu_us * 1000) / predict_count as u128
    } else { 0 };

    log!(logger, "Classification results (updated {} tasks):",
        cluster_tids.iter().map(|v| v.len()).sum::<usize>());
    log!(logger, "  [timing] batch wall={}us cpu={}us avg={}ns/task over {} tasks (incl. feature build + map update)",
        batch_wall_us, batch_cpu_us, avg_per_task_ns, predict_count);
    for (i, tids) in cluster_tids.iter().enumerate() {
        let cluster_cfg = cfg.clusters
            .get(&i.to_string())
            .unwrap_or(&cfg.default);
        log!(logger, "  Cluster {} (prio={}, {} tasks)",
            i, cluster_cfg.prio, tids.len());
    }
    Ok(())
}

/// Reject any cluster (or the default) whose `cpu_kind` exceeds the machine's
/// kind count. Valid range is `0..=cpu_kind_num` (0 = shared / any kind, 1 =
/// fastest kind). A binding to a non-existent kind would put tasks in a DSQ no
/// CPU pulls from, starving them — so fail loudly at startup instead.
fn validate_config_kinds(cfg: &SchedConfig, cpu_kind_num: u8) -> Result<()> {
    let check = |name: &str, c: &ClusterSchedConfig| -> Result<()> {
        if c.cpu_kind > cpu_kind_num {
            anyhow::bail!(
                "config {}: cpu_kind={} exceeds this machine's {} kind(s) \
                 (valid: 0=shared, 1..={})",
                name, c.cpu_kind, cpu_kind_num, cpu_kind_num
            );
        }
        Ok(())
    };
    check("default", &cfg.default)?;
    for (k, c) in &cfg.clusters {
        check(&format!("cluster {k}"), c)?;
    }
    Ok(())
}

/// Copy the discovered topology into the BPF rodata consts (`cpu_num`,
/// `cpu_kind_num`, `cpus_fast_to_slow`, `cpus_slow_to_fast`, `cpu_info`).
/// Must run after `open()` and before `load()` so libbpf rewrites the values
/// while the verifier still treats them as constants.
fn pack_topology(open_skel: &mut OpenBpfSkel, topo: &topology::Topology) {
    let rodata = open_skel
        .maps
        .rodata_data
        .as_deref_mut()
        .expect("BPF rodata section missing");

    rodata.cpu_num = topo.cpu_num;
    rodata.cpu_kind_num = topo.cpu_kind_num;

    // The rodata arrays are fixed-size [.. ; MAX_CPU]; copy in the prefix we
    // filled and leave the rest at its zero default.
    for (dst, &src) in rodata.cpus_fast_to_slow.iter_mut().zip(&topo.cpus_fast_to_slow) {
        *dst = src;
    }
    for (dst, &src) in rodata.cpus_slow_to_fast.iter_mut().zip(&topo.cpus_slow_to_fast) {
        *dst = src;
    }
    for (dst, src) in rodata.cpu_info.iter_mut().zip(&topo.cpu_info) {
        dst.cpu_kind = src.cpu_kind;
        dst.freq_n = src.freq_n;
        dst.freq_d = src.freq_d;
    }
}

fn main() -> Result<()> {
    let args = Args::parse();

    match args.mode.as_str() {
        "collect" | "classify" => {}
        _ => anyhow::bail!(
            "Invalid mode '{}'. Use 'collect' or 'classify'.",
            args.mode
        ),
    }

    // Collect mode refuses to overwrite an existing CSV: bail early so a run
    // never destroys prior data. Pick a new path or remove the old file.
    if args.mode == "collect" && std::path::Path::new(&args.output).exists() {
        anyhow::bail!(
            "Output file '{}' already exists; choose a different --output path.",
            args.output
        );
    }

    // Process-progress logger: quiet unless --verbose, in which case every
    // log! call goes to a file. The terminal is left for the Steam-scan UI.
    let logger = Rc::new(RefCell::new(Logger::new(args.verbose)));

    // Load model and config for classify mode
    let (model, sched_config) = if args.mode == "classify" {
        let model_path = args.model.as_deref()
            .context("Classify mode requires --model <path>")?;
        let m = classifier::load_model(model_path)?;
        log!(logger, "Loaded model from {} ({} clusters)", model_path, m.n_clusters());

        let config_path = args.config.as_deref()
            .context("Classify mode requires --config <path>")?;
        let content = std::fs::read_to_string(config_path)
            .with_context(|| format!("Failed to read config: {}", config_path))?;
        let cfg: SchedConfig = serde_json::from_str(&content)
            .context("Failed to parse scheduling config")?;
        log!(logger, "Loaded scheduling config from {}", config_path);

        (Some(m), Some(cfg))
    } else {
        (None, None)
    };

    log!(logger, "scx_teddy scheduler starting...");

    // Build and load eBPF skeleton
    let skel_builder = BpfSkelBuilder::default();
    let mut open_object = MaybeUninit::uninit();
    let mut open_skel = skel_builder.open(&mut open_object).context("Failed to open BPF object")?;

    // Initialize SCX enums from kernel BTF (SCX_DSQ_LOCAL_ON, etc.)
    scx_utils::import_enums!(open_skel);

    // Discover CPU topology (big/little kinds by max_freq) and pack it into the
    // BPF rodata consts before load, so the verifier sees them as constants.
    let topo = topology::Topology::discover();
    log!(logger, "topology: {}", topo.summary());
    // Surface the kind count on stdout so a user writing config.json knows the
    // valid cpu_kind range (1..=cpu_kind_num; 0 = shared).
    println!("[topology] {}", topo.summary());
    pack_topology(&mut open_skel, &topo);

    // Reject configs that bind a cluster to a CPU kind this machine doesn't
    // have, before load — otherwise those tasks land in a DSQ that no CPU ever
    // pulls from and starve.
    if let Some(cfg) = &sched_config {
        validate_config_kinds(cfg, topo.cpu_kind_num)?;
    }

    let mut skel = open_skel.load().context("Failed to load BPF object")?;

    let _futex_wait = skel.progs.trace_futex_wait.attach()?;

    // Load and attach the scheduler struct_ops
    let _struct_ops = skel
        .maps
        .teddy_ops
        .attach_struct_ops()
        .context("Failed to attach struct_ops")?;

    // Shared game-tracking state. `process_event` (scheduler hot path) and the
    // scan thread both touch it; the atomics inside keep the hot path lock-free.
    let tracker = Arc::new(game_task_finder::GameTracker::new());

    // Statistics storage
    let stats: StatsMap = Rc::new(RefCell::new(HashMap::new()));
    let stats_clone = Rc::clone(&stats);
    let tracker_rb = Arc::clone(&tracker);

    let mut builder = libbpf_rs::RingBufferBuilder::new();
    builder
        .add(&skel.maps.events,
             move |data| process_event(data, &stats_clone, &tracker_rb))
        .context("Failed to add ringbuf")?;
    let ringbuf = builder.build().context("Failed to build ringbuf")?;

    let scheduler_config = &skel.maps.scheduler_config;
    let update_map = &skel.maps.update_map;

    log!(logger, "scx_teddy scheduler loaded successfully!");

    // Shutdown flag: set by Ctrl+C, watched by the main loop and the scan
    // thread. The scan thread may be asleep inside `watch`, so the handler
    // also wakes it via the tracker.
    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_ctrlc = Arc::clone(&shutdown);
    let tracker_ctrlc = Arc::clone(&tracker);
    ctrlc::set_handler(move || {
        shutdown_ctrlc.store(true, Ordering::Release);
        tracker_ctrlc.signal_wake();
    })
    .expect("Error setting Ctrl+C handler");

    // Steam game-detection thread. `watch()` blocks on select(2)/stdin and
    // then sleeps while a game runs, so it runs on its own thread. It owns
    // nothing of the Rc-based scheduler state; it shares only the tracker
    // (atomics + condvar) and the shutdown flag, both Arc-cloned in.
    let tracker_scan = Arc::clone(&tracker);
    let shutdown_scan = Arc::clone(&shutdown);
    let scan_thread = thread::spawn(move || {
        game_task_finder::watch(
            game_task_finder::WatchConfig::default(),
            &tracker_scan,
            &shutdown_scan,
            |trigger, m| {
                let src = match trigger {
                    game_task_finder::Trigger::GameName(name) =>
                        format!("game-name '{name}'"),
                    game_task_finder::Trigger::Timer => "steam-env scan".to_string(),
                };
                println!("[steam] game detected via {src}: ppid={} ({} processes)",
                    m.ppid, m.procs.len());
            },
        );
    });

    let mut start_time = Instant::now();
    let duration = Duration::from_secs(args.collect_duration);
    let collect_mode = model.is_none();

    // Overall run-time limit (collect mode only): once this deadline passes the
    // loop stops and the CSV is flushed. None means no limit.
    let run_deadline = if collect_mode && args.max_runtime > 0 {
        Some(start_time + Duration::from_secs(args.max_runtime))
    } else {
        None
    };

    // Main loop - keep scheduler running
    while !shutdown.load(Ordering::Acquire)
        && !scx_utils::uei_exited!(&skel, uei)
        && run_deadline.is_none_or(|d| Instant::now() < d)
    {
        if start_time.elapsed() >= duration {
            // Pause pushing events into the ring buffer while this cycle runs,
            // so the buffer cannot overflow during prediction / CSV work.
            let key = 0u32.to_ne_bytes();
            scheduler_config.update(&key, &1u32.to_ne_bytes(), MapFlags::ANY)?;

            if let (Some(classifier), Some(cfg)) = (&model, &sched_config) {
                run_classify_cycle(&stats.borrow(), update_map,
                    classifier.as_ref(), cfg, args.min_events, &tracker, &logger)?;
            } else if args.csv_checkpoint {
                // Collect mode writes the CSV every cycle only with this flag;
                // otherwise it is flushed once on shutdown.
                let game_ppid = tracker.game_ppid.load(Ordering::Acquire);
                let n = collect_data(&stats.borrow(), &args.output, args.min_events, game_ppid)?;
                log!(logger, "CSV written: {} rows", n);
            }

            start_time = Instant::now();
            scheduler_config.update(&key, &0u32.to_ne_bytes(), MapFlags::ANY)?;
        }
        ringbuf.poll(Duration::from_millis(1000))?;
    }

    log!(logger, "scx_teddy scheduler exiting...");

    // Stop the scan thread. The loop may have exited via run_deadline or a UEI
    // rather than Ctrl+C, in which case `shutdown` is still false — set it and
    // wake the scan thread out of any condvar wait. If it is instead blocked
    // in select(2) on stdin, it sees `shutdown` after the current timeout
    // (<= scan_interval_secs), so the join can take up to that long.
    shutdown.store(true, Ordering::Release);
    tracker.signal_wake();
    let _ = scan_thread.join();

    // Flush the CSV on shutdown (collect mode).
    if collect_mode {
        let game_ppid = tracker.game_ppid.load(Ordering::Acquire);
        let n = collect_data(&stats.borrow(), &args.output, args.min_events, game_ppid)?;
        log!(logger, "CSV written: {} rows", n);
    }

    scx_utils::uei_report!(&skel, uei)
        .context("UEI report")?;

    Ok(())
}
