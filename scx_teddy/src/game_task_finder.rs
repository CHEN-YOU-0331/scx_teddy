//! Find the process family of a running game, and track it until it ends.
//!
//! Steam-environment scanning is one of the lookup methods here, not the
//! whole module: Steam injects `SteamGameId=<appid>` into a launched game's
//! environment (older builds used `STEAM_GAME=`), so reading
//! `/proc/<pid>/environ` surfaces the members of a game's process family.
//! A comm-name lookup is also provided for games typed in by hand.
//!
//! Integration note
//! ----------------
//! This module has no external crate dependencies. Public API:
//!
//!   - `scan_by_game_name(name) -> Option<Match>` : comm-based lookup.
//!   - `steam_scan_proc() -> Option<Match>`       : Steam-environment lookup.
//!   - `GameTracker`                              : state shared with the
//!                                                  scheduler thread.
//!   - `watch(cfg, tracker, shutdown, on_match)`  : stdin + timer event loop
//!                                                  with game-end tracking.
//!
//! `watch` multiplexes stdin and a periodic timer via select(2); callers
//! supply a callback so the loop carries no I/O policy of its own. The only
//! OS dependency is a tiny hand-written FFI declaration of `select` (see
//! the `ffi` module) so that no `libc` crate is required.

use std::collections::HashMap;
use std::fs;
use std::io::{self, BufRead};
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Condvar, Mutex};

// ===========================================================================
// Scanning
// ===========================================================================

/// Environment variable names that mark a Steam-launched process.
const STEAM_ENV_KEYS: [&str; 2] = ["SteamGameId", "STEAM_GAME"];

/// Steam infrastructure processes that are filtered out of scan results.
const STEAM_INFRA: [&str; 4] = ["reaper", "srt-bwrap", "pv-adverb", "steam.exe"];

/// A process discovered during a scan.
#[derive(Debug, Clone)]
pub struct ProcEntry {
    pub pid: i32,
    pub comm: String,
}

/// Result of a successful scan: the matched PPID and every process under it.
#[derive(Debug, Clone)]
pub struct Match {
    pub ppid: i32,
    pub procs: Vec<ProcEntry>,
}

/// Read /proc/<pid>/environ and return its environment variables as a map.
/// Returns an empty map if the file cannot be read.
fn read_environ(pid: i32) -> HashMap<String, String> {
    let mut env = HashMap::new();
    let raw = match fs::read(format!("/proc/{pid}/environ")) {
        Ok(raw) => raw,
        Err(_) => return env,
    };

    for kv in raw.split(|&b| b == 0) {
        if kv.is_empty() {
            continue;
        }
        let text = String::from_utf8_lossy(kv);
        if let Some((key, value)) = text.split_once('=') {
            env.insert(key.to_string(), value.to_string());
        }
    }
    env
}

/// Read a single-field file under /proc/<pid> (e.g. comm or cmdline).
/// NUL bytes are replaced with spaces. Returns an empty string on failure.
fn read_field(pid: i32, field: &str) -> String {
    let raw = match fs::read(format!("/proc/{pid}/{field}")) {
        Ok(raw) => raw,
        Err(_) => return String::new(),
    };
    // cmdline is NUL-separated; comm has a trailing newline.
    let replaced: Vec<u8> = raw
        .iter()
        .map(|&b| if b == 0 { b' ' } else { b })
        .collect();
    String::from_utf8_lossy(&replaced).trim().to_string()
}

/// Read the PPID from /proc/<pid>/stat. Returns 0 if it cannot be read.
fn get_ppid(pid: i32) -> i32 {
    let stat = match fs::read(format!("/proc/{pid}/stat")) {
        Ok(raw) => String::from_utf8_lossy(&raw).into_owned(),
        Err(_) => return 0,
    };
    // stat format: pid (comm) state ppid ...
    // comm may contain spaces/parens, so split after the last ')'.
    let rest = match stat.rfind(')') {
        Some(idx) => &stat[idx + 1..],
        None => &stat[..],
    };
    let fields: Vec<&str> = rest.split_whitespace().collect();
    // Treat missing or non-numeric fields as 0.
    fields.get(1).and_then(|s| s.parse().ok()).unwrap_or(0)
}

/// Normalize a comm string for comparison: strip all whitespace, lowercase.
fn normalize(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(|c| c.to_lowercase())
        .collect()
}

/// Iterate numeric pid entries under /proc, invoking `f` for each pid.
/// Returns early if /proc cannot be read.
fn for_each_pid(mut f: impl FnMut(i32)) {
    let entries = match fs::read_dir("/proc") {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        if let Ok(pid) = name.to_string_lossy().parse::<i32>() {
            f(pid);
        }
    }
}

/// List all direct children of `parent_pid` by scanning /proc and matching PPID.
pub fn children_of(parent_pid: i32) -> Vec<ProcEntry> {
    let mut children = Vec::new();
    for_each_pid(|pid| {
        if get_ppid(pid) == parent_pid {
            children.push(ProcEntry {
                pid,
                comm: read_field(pid, "comm"),
            });
        }
    });
    children
}

/// Scan /proc for a process whose comm matches `game_name` (after normalize),
/// then return that process's PPID together with all processes that share it
/// as their parent (i.e. siblings under the same PPID).
///
/// Returns None if no process matches the given game name.
pub fn scan_by_game_name(game_name: &str) -> Option<Match> {
    let target = normalize(game_name);
    let mut found: Option<i32> = None;

    for_each_pid(|pid| {
        if found.is_none() && normalize(&read_field(pid, "comm")) == target {
            found = Some(pid);
        }
    });

    let ppid = get_ppid(found?);
    Some(Match {
        ppid,
        procs: children_of(ppid),
    })
}

/// Scan every process for one carrying a Steam game id in its environment.
/// On a hit, return the PPID of the first match and every process living
/// under that PPID. Steam infrastructure processes are skipped so the match
/// lands on a real game process. Returns None when nothing is found.
pub fn steam_scan_proc() -> Option<Match> {
    let mut found: Option<i32> = None;

    for_each_pid(|pid| {
        if found.is_some() {
            return;
        }
        let env = read_environ(pid);
        let has_steam_id = STEAM_ENV_KEYS.iter().any(|k| env.contains_key(*k));
        if !has_steam_id {
            return;
        }
        // Skip Steam infrastructure; keep looking for a real game process.
        if !STEAM_INFRA.contains(&read_field(pid, "comm").as_str()) {
            found = Some(pid);
        }
    });

    let ppid = get_ppid(found?);
    Some(Match {
        ppid,
        procs: children_of(ppid),
    })
}

// ===========================================================================
// Watch loop (stdin + periodic timer, multiplexed via select(2))
// ===========================================================================

/// Minimal FFI: just enough of select(2) to multiplex one fd with a timeout,
/// so this module needs no `libc` crate.
mod ffi {
    use std::os::raw::c_int;

    /// `fd_set` on Linux is a bitmask of `FD_SETSIZE` (1024) bits.
    const FD_SETSIZE: usize = 1024;
    const BITS: usize = 8 * std::mem::size_of::<u64>();

    #[repr(C)]
    pub struct FdSet {
        bits: [u64; FD_SETSIZE / BITS],
    }

    impl FdSet {
        pub fn new() -> Self {
            FdSet {
                bits: [0; FD_SETSIZE / BITS],
            }
        }
        pub fn set(&mut self, fd: c_int) {
            let fd = fd as usize;
            self.bits[fd / BITS] |= 1 << (fd % BITS);
        }
        pub fn is_set(&self, fd: c_int) -> bool {
            let fd = fd as usize;
            self.bits[fd / BITS] & (1 << (fd % BITS)) != 0
        }
    }

    #[repr(C)]
    pub struct Timeval {
        pub tv_sec: i64,
        pub tv_usec: i64,
    }

    extern "C" {
        pub fn select(
            nfds: c_int,
            readfds: *mut FdSet,
            writefds: *mut FdSet,
            exceptfds: *mut FdSet,
            timeout: *mut Timeval,
        ) -> c_int;
    }
}

// ===========================================================================
// Shared game-tracking state (scan thread <-> scheduler thread)
// ===========================================================================

/// State shared between the scan thread and the scheduler thread.
///
/// The two atomics are the only fields the scheduler's hot path touches, so
/// it never blocks: it `load`s `game_ppid` and, for a dying game process,
/// `fetch_sub`s `alive_count`. The `Mutex`/`Condvar` pair exists only so the
/// scan thread can sleep while a game runs and be woken when it ends — the
/// scheduler locks the mutex for the single instant it sends that notify.
pub struct GameTracker {
    /// Common parent PID of the currently tracked game's process family.
    /// 0 means no game is tracked (the scan thread is actively scanning).
    pub game_ppid: AtomicI32,
    /// Number of the tracked game's processes still alive. When it reaches 0
    /// the game is considered ended and the scan thread is woken.
    pub alive_count: AtomicI32,
    /// `wake` flips to true to release the scan thread from `wait_for_game_end`
    /// — set both when the game ends and on shutdown.
    wake: Mutex<bool>,
    wake_cv: Condvar,
}

impl GameTracker {
    pub fn new() -> Self {
        GameTracker {
            game_ppid: AtomicI32::new(0),
            alive_count: AtomicI32::new(0),
            wake: Mutex::new(false),
            wake_cv: Condvar::new(),
        }
    }

    /// True while a game is being tracked (set by the scan thread on a match).
    pub fn is_tracking(&self) -> bool {
        self.game_ppid.load(Ordering::Acquire) != 0
    }

    /// Called by the scheduler thread when a tracked game process exits:
    /// decrement the alive count and, if it hit zero, wake the scan thread.
    /// A no-op when no game is tracked.
    ///
    /// Note: hitting zero does *not* clear `game_ppid` here. The alive count
    /// is a snapshot taken when the game was detected, so a game shedding a
    /// few threads (e.g. during a cutscene load) can drive it to zero while
    /// the game is still very much alive. The scan thread, once woken,
    /// re-scans and only treats the game as ended if that scan finds nothing
    /// — see `watch`.
    pub fn note_process_exit(&self, dead_ppid: i32) {
        let tracked = self.game_ppid.load(Ordering::Acquire);
        if tracked == 0 || dead_ppid != tracked {
            return;
        }
        // fetch_sub returns the value *before* subtracting, so 1 means this
        // call brought the count to 0.
        if self.alive_count.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.signal_wake();
        }
    }

    /// Scan thread: the tracked game has truly ended (a re-scan found nothing).
    /// Clear the tracked PPID so the next loop iteration scans afresh.
    fn clear(&self) {
        self.game_ppid.store(0, Ordering::Release);
    }

    /// Wake the scan thread out of `wait_for_game_end`. Used both for "game
    /// ended" and for shutdown.
    pub fn signal_wake(&self) {
        let mut woken = self.wake.lock().unwrap();
        *woken = true;
        self.wake_cv.notify_all();
    }

    /// Scan thread: record a freshly detected game and block until it ends
    /// (or shutdown). `procs` is the game's process family from a scan.
    fn track_and_wait(&self, ppid: i32, proc_count: i32) {
        self.alive_count.store(proc_count, Ordering::Release);
        self.game_ppid.store(ppid, Ordering::Release);
        self.wait_for_game_end();
    }

    /// Block the scan thread until `signal_wake` is called.
    fn wait_for_game_end(&self) {
        let mut woken = self.wake.lock().unwrap();
        while !*woken {
            woken = self.wake_cv.wait(woken).unwrap();
        }
        *woken = false;
    }
}

impl Default for GameTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Configuration for the watch loop.
pub struct WatchConfig {
    /// How often the environment-based Steam scan runs, in seconds.
    pub scan_interval_secs: i64,
}

impl Default for WatchConfig {
    fn default() -> Self {
        WatchConfig {
            scan_interval_secs: 5,
        }
    }
}

/// What triggered a match, passed to the watch callback.
#[derive(Debug, Clone)]
pub enum Trigger {
    /// A game name typed on stdin triggered a comm-based scan.
    GameName(String),
    /// The periodic timer triggered an environment-based Steam scan.
    Timer,
}

/// Block until stdin is readable or `timeout_secs` elapses.
/// Returns true if stdin is readable, false on timeout or on an error
/// (an error is treated as a timeout so the loop performs a periodic scan
/// rather than spinning).
fn wait_for_stdin(stdin_fd: i32, timeout_secs: i64) -> bool {
    let mut readfds = ffi::FdSet::new();
    readfds.set(stdin_fd);
    let mut timeout = ffi::Timeval {
        tv_sec: timeout_secs,
        tv_usec: 0,
    };

    // SAFETY: readfds and timeout are valid, correctly sized, and outlive the
    // call; nfds = stdin_fd + 1 covers the single fd we registered.
    let ret = unsafe {
        ffi::select(
            stdin_fd + 1,
            &mut readfds,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut timeout,
        )
    };

    ret > 0 && readfds.is_set(stdin_fd)
}

/// Re-run the scan that originally found a game, to confirm it is still
/// running. A `GameName` trigger re-scans by that comm name; a `Timer`
/// trigger re-runs the Steam-environment scan.
fn rescan(trigger: &Trigger) -> Option<Match> {
    match trigger {
        Trigger::GameName(name) => scan_by_game_name(name),
        Trigger::Timer => steam_scan_proc(),
    }
}

/// Run the stdin + periodic-timer watch loop with dynamic game tracking.
///
/// While no game is tracked the loop waits up to `cfg.scan_interval_secs` for
/// a line on stdin: a line triggers a `scan_by_game_name`, a timeout triggers
/// a `steam_scan_proc`. On a successful scan it invokes `on_match`, records
/// the game in `tracker`, then **sleeps** until the scheduler thread reports
/// (via `tracker`) that the game's alive process count hit zero.
///
/// That zero is only a hint, not proof the game ended: the count is a
/// snapshot from detection time, so a game shedding threads (a cutscene load,
/// say) can reach zero while still running. So on wake the loop **re-scans**:
/// if the game is still found it is silently re-tracked (no notice printed);
/// only if the re-scan finds nothing is the game declared ended.
///
/// The loop also exits when `shutdown` is set; the caller is expected to set
/// it and then call `tracker.signal_wake()` so a sleeping scan thread wakes.
///
/// `on_match` carries all output/side-effect policy, so this loop stays
/// reusable: pass a closure that does whatever you need.
pub fn watch(
    cfg: WatchConfig,
    tracker: &GameTracker,
    shutdown: &AtomicBool,
    mut on_match: impl FnMut(Trigger, &Match),
) {
    let stdin = io::stdin();
    let stdin_fd = stdin.as_raw_fd();

    loop {
        if shutdown.load(Ordering::Acquire) {
            break;
        }

        // Scan for a game: either a name typed on stdin, or the periodic
        // environment scan on timeout.
        let found = if wait_for_stdin(stdin_fd, cfg.scan_interval_secs) {
            let mut line = String::new();
            match stdin.lock().read_line(&mut line) {
                Ok(0) => break, // EOF (Ctrl-D)
                Ok(_) => {}
                Err(e) => {
                    eprintln!("stdin read error: {e}");
                    break;
                }
            }
            let name = line.trim().to_string();
            if name.is_empty() {
                continue;
            }
            scan_by_game_name(&name).map(|m| (Trigger::GameName(name), m))
        } else {
            steam_scan_proc().map(|m| (Trigger::Timer, m))
        };

        let Some((trigger, mut m)) = found else {
            continue;
        };
        on_match(trigger.clone(), &m);

        // Track the game and sleep; on each wake, re-scan to tell a real exit
        // from a transient drop to zero. Stay in this inner loop, silently
        // re-tracking, until a re-scan finds nothing (or shutdown).
        loop {
            tracker.track_and_wait(m.ppid, m.procs.len() as i32);
            if shutdown.load(Ordering::Acquire) {
                break;
            }
            match rescan(&trigger) {
                // Still running — a transient zero (e.g. cutscene). Re-track
                // quietly; the user never sees this.
                Some(again) => m = again,
                // Re-scan found nothing: the game has truly ended.
                None => {
                    tracker.clear();
                    println!("[game] Game appears to have ended — you can type a game/task name to add one.");
                    break;
                }
            }
        }
    }
}
