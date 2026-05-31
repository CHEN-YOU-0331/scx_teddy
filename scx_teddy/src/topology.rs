//! CPU topology discovery for the big/little (hybrid) scheduler base.
//!
//! Logical CPUs are grouped into "kinds" by their `cpuinfo_max_freq`, read
//! from the generic cpufreq sysfs path (`/sys/devices/system/cpu/cpufreq/
//! policy*/`). That path exists on virtually every Linux box with frequency
//! scaling and is not tied to Intel-only `cpu_atom`/`cpu_core` dirs, so the
//! same scan works on Intel P+E, ARM big.LITTLE, future tri-tier silicon, and
//! homogeneous machines (one kind) alike.
//!
//! Kinds are numbered fastest-first: kind 0 is the highest-max_freq group.
//! Each CPU also carries `freq_n`/`freq_d` so the scheduler can normalise a
//! runtime measured on a slow core to the fastest core's clock:
//! `normalised = runtime * freq_n / freq_d`, where `freq_d` is the fastest
//! CPU's max_freq and `freq_n` is this CPU's max_freq.
//!
//! Offline CPUs are excluded for free: the cpufreq policy dirs only list
//! online CPUs in `related_cpus`, so the main path never sees them (and scx
//! ignores unavailable CPUs on its side too).
//!
//! The result is packed straight into the BPF rodata consts declared in
//! `main.bpf.c` (`cpu_num`, `cpu_kind_num`, `cpus_fast_to_slow`,
//! `cpus_slow_to_fast`, `cpu_info`) before the skeleton is loaded.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

/// Mirror of the BPF `MAX_CPU` in intf.h. Keep in sync.
pub const MAX_CPU: usize = 512;

/// Per-CPU topology entry, mirrors `cpu_info_t` in intf.h.
#[derive(Debug, Clone, Copy, Default)]
pub struct CpuInfo {
    /// 0 = fastest kind.
    pub cpu_kind: u8,
    /// This CPU's max_freq (kHz).
    pub freq_n: u32,
    /// Fastest CPU's max_freq (kHz).
    pub freq_d: u32,
}

/// Full machine topology, ready to be packed into BPF rodata.
#[derive(Debug, Clone)]
pub struct Topology {
    /// Number of online logical CPUs we discovered.
    pub cpu_num: u8,
    /// Number of distinct frequency kinds (>= 1).
    pub cpu_kind_num: u8,
    /// CPU ids sorted fastest → slowest (first `cpu_num` entries valid).
    pub cpus_fast_to_slow: Vec<u8>,
    /// CPU ids sorted slowest → fastest.
    pub cpus_slow_to_fast: Vec<u8>,
    /// Indexed by logical CPU id.
    pub cpu_info: Vec<CpuInfo>,
}

/// Read each logical CPU's `cpuinfo_max_freq` (kHz) via the cpufreq policy
/// dirs. Returns a map cpu_id → max_freq. CPUs without a cpufreq policy
/// (e.g. scaling disabled, or some VMs) are simply absent; the caller falls
/// back to a single homogeneous kind covering `fallback_cpu_count`.
fn read_cpufreq() -> BTreeMap<u32, u32> {
    let mut out = BTreeMap::new();
    let base = Path::new("/sys/devices/system/cpu/cpufreq");
    let Ok(entries) = fs::read_dir(base) else {
        return out;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if !name.starts_with("policy") {
            continue;
        }
        let path = entry.path();
        let Ok(max_s) = fs::read_to_string(path.join("cpuinfo_max_freq")) else {
            continue;
        };
        let Ok(max_khz) = max_s.trim().parse::<u32>() else {
            continue;
        };
        // related_cpus lists every logical CPU sharing this policy.
        let Ok(related) = fs::read_to_string(path.join("related_cpus")) else {
            continue;
        };
        for tok in related.split_whitespace() {
            if let Ok(cpu) = tok.parse::<u32>() {
                out.insert(cpu, max_khz);
            }
        }
    }
    out
}

/// Coarse logical-CPU count for the no-cpufreq fallback only. Reads the range
/// expression in `/sys/devices/system/cpu/online` (e.g. "0-19" or "0,2-5")
/// and sums it — it does NOT inspect each CPU's online state individually
/// (cpufreq absence is the only path that reaches here). Defaults to 1 so the
/// fallback always yields a usable single-kind topology.
fn fallback_cpu_count() -> u32 {
    let Ok(s) = fs::read_to_string("/sys/devices/system/cpu/online") else {
        // Last resort: count cpuN dirs.
        return fs::read_dir("/sys/devices/system/cpu")
            .map(|rd| {
                rd.flatten()
                    .filter(|e| {
                        e.file_name().to_str().is_some_and(|n| {
                            n.strip_prefix("cpu").is_some_and(|r| {
                                !r.is_empty() && r.bytes().all(|b| b.is_ascii_digit())
                            })
                        })
                    })
                    .count() as u32
            })
            .unwrap_or(1);
    };
    let mut n = 0u32;
    for piece in s.trim().split(',') {
        if let Some((a, b)) = piece.split_once('-') {
            if let (Ok(a), Ok(b)) = (a.parse::<u32>(), b.parse::<u32>()) {
                n += b - a + 1;
            }
        } else if piece.parse::<u32>().is_ok() {
            n += 1;
        }
    }
    n.max(1)
}

impl Topology {
    /// Discover the machine topology by scanning cpufreq sysfs. Always returns
    /// a usable topology: on hardware without cpufreq it collapses to a single
    /// kind with `freq_n == freq_d` (so normalisation is a no-op).
    pub fn discover() -> Self {
        let freq_by_cpu = read_cpufreq();

        // Fallback: no cpufreq data → one kind, all CPUs, no normalisation.
        if freq_by_cpu.is_empty() {
            let n = fallback_cpu_count().min(MAX_CPU as u32);
            let cpus: Vec<u8> = (0..n as u8).collect();
            let cpu_info = vec![CpuInfo { cpu_kind: 0, freq_n: 1, freq_d: 1 }; n as usize];
            let mut slow = cpus.clone();
            slow.reverse();
            return Topology {
                cpu_num: n as u8,
                cpu_kind_num: 1,
                cpus_fast_to_slow: cpus,
                cpus_slow_to_fast: slow,
                cpu_info,
            };
        }

        // Distinct max_freqs, sorted high → low: index = kind id (0 = fastest).
        let mut freqs: Vec<u32> = freq_by_cpu.values().copied().collect();
        freqs.sort_unstable_by(|a, b| b.cmp(a));
        freqs.dedup();
        let kind_of_freq: BTreeMap<u32, u8> =
            freqs.iter().enumerate().map(|(i, &f)| (f, i as u8)).collect();
        let fastest = *freqs.first().expect("non-empty after is_empty check");

        // cpu_info indexed by cpu id. Size to highest cpu id + 1 so direct
        // indexing in BPF is valid; gaps (offline cpus) stay default/zero.
        let max_id = *freq_by_cpu.keys().max().unwrap() as usize;
        let mut cpu_info = vec![CpuInfo::default(); max_id + 1];
        for (&cpu, &khz) in &freq_by_cpu {
            cpu_info[cpu as usize] = CpuInfo {
                cpu_kind: kind_of_freq[&khz],
                freq_n: khz,
                freq_d: fastest,
            };
        }

        // Order CPUs fast → slow: by kind (ascending = fast first), then cpu
        // id for a stable, reproducible order within a kind.
        let mut ordered: Vec<u32> = freq_by_cpu.keys().copied().collect();
        ordered.sort_by(|&a, &b| {
            cpu_info[a as usize]
                .cpu_kind
                .cmp(&cpu_info[b as usize].cpu_kind)
                .then(a.cmp(&b))
        });
        let cpus_fast_to_slow: Vec<u8> = ordered.iter().map(|&c| c as u8).collect();
        let cpus_slow_to_fast: Vec<u8> = cpus_fast_to_slow.iter().rev().copied().collect();

        Topology {
            cpu_num: freq_by_cpu.len() as u8,
            cpu_kind_num: freqs.len() as u8,
            cpus_fast_to_slow,
            cpus_slow_to_fast,
            cpu_info,
        }
    }

    /// Human-readable one-line summary per kind, for startup logging.
    pub fn summary(&self) -> String {
        // kind → (freq kHz, cpu ids).
        let mut by_kind: BTreeMap<u8, (u32, Vec<u8>)> = BTreeMap::new();
        for (id, info) in self.cpu_info.iter().enumerate() {
            if info.freq_n == 0 {
                continue; // gap entry (offline / unfilled cpu id)
            }
            by_kind
                .entry(info.cpu_kind)
                .or_insert_with(|| (info.freq_n, Vec::new()))
                .1
                .push(id as u8);
        }
        let parts: Vec<String> = by_kind
            .iter()
            .map(|(kind, (khz, cpus))| {
                format!(
                    "kind{} ({:.2}GHz, {} cpus)",
                    kind,
                    *khz as f64 / 1_000_000.0,
                    cpus.len()
                )
            })
            .collect();
        format!(
            "{} CPUs, {} kind(s): {}",
            self.cpu_num,
            self.cpu_kind_num,
            parts.join(", ")
        )
    }
}
