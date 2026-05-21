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
use std::os::raw::c_int;
use std::os::unix::io::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};

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

/// Minimal FFI: select(2) for multiplexing stdin + the wake eventfd, and
/// the eventfd(2) / read / write / close syscalls used for the wake
/// channel. No `libc` crate dependency.
mod ffi {
    use std::os::raw::{c_int, c_uint, c_void};

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

    /// `EFD_CLOEXEC` from <sys/eventfd.h> — keep the wake fd from leaking
    /// across exec(). Value is the same as `O_CLOEXEC` on Linux.
    pub const EFD_CLOEXEC: c_int = 0o2000000;

    /// `CLOCK_MONOTONIC` from <time.h>. Used for timerfd; immune to
    /// wall-clock jumps (NTP / hibernate resume).
    pub const CLOCK_MONOTONIC: c_int = 1;
    /// `TFD_CLOEXEC` from <sys/timerfd.h>. Same value as O_CLOEXEC.
    pub const TFD_CLOEXEC: c_int = 0o2000000;

    /// `struct timespec`. Matches the kernel layout on 64-bit Linux.
    #[repr(C)]
    pub struct Timespec {
        pub tv_sec: i64,
        pub tv_nsec: i64,
    }

    /// `struct itimerspec` — initial expiration + repeating interval. The
    /// timerfd we set up here is a recurring tick, so both fields are the
    /// same duration. A zero-valued `it_value` disarms the timer.
    #[repr(C)]
    pub struct ITimerspec {
        pub it_interval: Timespec,
        pub it_value: Timespec,
    }

    extern "C" {
        pub fn select(
            nfds: c_int,
            readfds: *mut FdSet,
            writefds: *mut FdSet,
            exceptfds: *mut FdSet,
            timeout: *mut c_void, // NULL = block forever; we never pass non-NULL
        ) -> c_int;

        pub fn eventfd(initval: c_uint, flags: c_int) -> c_int;
        pub fn read(fd: c_int, buf: *mut c_void, count: usize) -> isize;
        pub fn write(fd: c_int, buf: *const c_void, count: usize) -> isize;
        pub fn close(fd: c_int) -> c_int;

        pub fn timerfd_create(clockid: c_int, flags: c_int) -> c_int;
        pub fn timerfd_settime(
            fd: c_int,
            flags: c_int,
            new_value: *const ITimerspec,
            old_value: *mut ITimerspec,
        ) -> c_int;
    }
}

// ===========================================================================
// Shared game-tracking state (scan thread <-> scheduler thread)
// ===========================================================================

/// State shared between the scan thread and the scheduler thread.
///
/// `game_ppid` / `alive_count` are atomics so the scheduler's hot path is
/// lock-free. The wake channel is an `eventfd(2)`: the scheduler writes a
/// non-zero u64 to wake the scan thread, the scan thread reads to clear,
/// and `select(2)` can multiplex it with stdin so the scan thread can wait
/// for either a user-typed game name OR a scheduler-side notification on
/// the same call.
///
/// Wake reasons are not encoded in the eventfd value — the scan thread,
/// on every wake, just re-examines `game_ppid` and the shutdown flag to
/// decide what to do.
pub struct GameTracker {
    /// Common parent PID of the currently tracked game's process family.
    /// 0 means no game is tracked (the scan thread is actively scanning).
    pub game_ppid: AtomicI32,
    /// Number of the tracked game's processes still alive. When it reaches 0
    /// the game is considered ended and the scan thread is woken.
    pub alive_count: AtomicI32,
    /// eventfd used to wake the scan thread from a blocking `select(2)`.
    /// Owned by the tracker; closed in `Drop`.
    wake_fd: c_int,
}

impl GameTracker {
    pub fn new() -> Self {
        // SAFETY: ffi call; EFD_CLOEXEC is a valid flag. A negative return
        // would mean we cannot create an eventfd, which is fatal for the
        // game-tracking machinery — panic with errno preserved by the OS
        // error message rather than silently degrading.
        let fd = unsafe { ffi::eventfd(0, ffi::EFD_CLOEXEC) };
        if fd < 0 {
            panic!("eventfd(2) failed: {}", io::Error::last_os_error());
        }
        GameTracker {
            game_ppid: AtomicI32::new(0),
            alive_count: AtomicI32::new(0),
            wake_fd: fd,
        }
    }

    /// True while a game is being tracked (set by the scan thread on a match).
    pub fn is_tracking(&self) -> bool {
        self.game_ppid.load(Ordering::Acquire) != 0
    }

    /// Raw fd of the wake eventfd. The scan thread passes it to `select(2)`.
    pub(crate) fn wake_fd(&self) -> c_int {
        self.wake_fd
    }

    /// Drain the eventfd after `select(2)` reported it readable. eventfd is
    /// a counter; one read returns and zeroes the accumulated value, so a
    /// burst of wakes coalesces into one wake-up — which is exactly what we
    /// want (we re-examine state on wake, not the count).
    pub(crate) fn consume_wake(&self) {
        let mut buf = [0u8; 8];
        // SAFETY: ffi call with a valid pointer and length; we ignore the
        // return value (EAGAIN can't happen on a blocking eventfd that
        // select(2) just reported ready; any other error means the kernel
        // is in trouble and we'll find out on the next syscall).
        unsafe {
            ffi::read(self.wake_fd, buf.as_mut_ptr() as *mut _, buf.len());
        }
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

    /// Clear the tracked PPID so the scan thread will scan afresh next time
    /// it inspects `game_ppid`. Callable from either thread; the scheduler
    /// uses it when it sees the game's own PPID exit (process_event), the
    /// scan thread uses it after a re-scan confirms the game is gone.
    pub fn clear(&self) {
        self.game_ppid.store(0, Ordering::Release);
    }

    /// Wake the scan thread out of `select(2)`. Writing 1 onto the eventfd
    /// counter wakes any reader; the scan thread re-examines state and
    /// decides what to do.
    pub fn signal_wake(&self) {
        let one: u64 = 1;
        // SAFETY: ffi call with a valid pointer / length; return value can
        // be ignored — a failed write here just means the scan thread will
        // not wake right now, and the next signal_wake or any other wake
        // event will cover it.
        unsafe {
            ffi::write(
                self.wake_fd,
                &one as *const u64 as *const _,
                std::mem::size_of::<u64>(),
            );
        }
    }

    /// Scan thread: record a freshly detected game. No longer blocks here —
    /// the watch loop drives all blocking via `select(2)` on the eventfd.
    fn track(&self, ppid: i32, proc_count: i32) {
        self.alive_count.store(proc_count, Ordering::Release);
        self.game_ppid.store(ppid, Ordering::Release);
    }
}

impl Drop for GameTracker {
    fn drop(&mut self) {
        if self.wake_fd >= 0 {
            // SAFETY: ffi call on a fd we own and are about to forget.
            unsafe { ffi::close(self.wake_fd); }
        }
    }
}

impl Default for GameTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Configuration for the watch loop.
pub struct WatchConfig {
    /// Period of the recurring environment-scan tick, in seconds. Only
    /// applies in the scan state; while a game is tracked the timer is not
    /// watched. Set to 0 to disable the periodic tick entirely.
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
    /// A wake from the scheduler thread triggered an environment-based scan.
    Timer,
}

/// Block until any of stdin, wake_fd, or timer_fd is readable. Returns
/// `(stdin, wake, timer)` readiness flags. No timeout — the timerfd is
/// what produces periodic ticks; everything else is event-driven.
///
/// A negative `timer_fd` means no timer is armed (when `scan_interval_secs
/// == 0`); we skip registering it.
fn select_scan_fds(stdin_fd: c_int, wake_fd: c_int, timer_fd: c_int) -> (bool, bool, bool) {
    let mut readfds = ffi::FdSet::new();
    readfds.set(stdin_fd);
    readfds.set(wake_fd);
    if timer_fd >= 0 {
        readfds.set(timer_fd);
    }
    let nfds = stdin_fd.max(wake_fd).max(timer_fd) + 1;

    // SAFETY: readfds outlives the call; NULL timeout blocks until a
    // watched fd becomes readable or a signal interrupts. EINTR returns
    // negative with no fd set, treated as a spurious wake.
    let ret = unsafe {
        ffi::select(
            nfds,
            &mut readfds,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };

    if ret <= 0 {
        return (false, false, false);
    }
    (
        readfds.is_set(stdin_fd),
        readfds.is_set(wake_fd),
        timer_fd >= 0 && readfds.is_set(timer_fd),
    )
}

/// Create a recurring timerfd ticking every `interval_secs` seconds, on
/// CLOCK_MONOTONIC. Returns -1 if `interval_secs == 0` (caller treats as
/// "no timer") or on syscall failure (degrade to no-tick rather than
/// abort the whole watch loop).
fn arm_scan_timer(interval_secs: i64) -> c_int {
    if interval_secs <= 0 {
        return -1;
    }
    // SAFETY: ffi syscall.
    let fd = unsafe { ffi::timerfd_create(ffi::CLOCK_MONOTONIC, ffi::TFD_CLOEXEC) };
    if fd < 0 {
        eprintln!("timerfd_create failed: {}", io::Error::last_os_error());
        return -1;
    }
    let spec = ffi::ITimerspec {
        it_interval: ffi::Timespec { tv_sec: interval_secs, tv_nsec: 0 },
        // First tick fires almost immediately so a game that's already
        // running at scheduler startup gets picked up without waiting a
        // full interval. 1 ns is the smallest non-zero (zero would disarm).
        it_value:    ffi::Timespec { tv_sec: 0, tv_nsec: 1 },
    };
    // SAFETY: spec is valid for the call's duration; old_value NULL is fine.
    let rc = unsafe { ffi::timerfd_settime(fd, 0, &spec, std::ptr::null_mut()) };
    if rc < 0 {
        eprintln!("timerfd_settime failed: {}", io::Error::last_os_error());
        // SAFETY: fd was a successful timerfd_create.
        unsafe { ffi::close(fd); }
        return -1;
    }
    fd
}

/// Drain a ready timerfd. Reading returns the number of expirations since
/// last read as a u64; the value is uninteresting, we just clear the fd
/// so select(2) stops reporting it immediately ready.
fn consume_timer(timer_fd: c_int) {
    let mut buf = [0u8; 8];
    // SAFETY: ffi call, valid buffer.
    unsafe {
        ffi::read(timer_fd, buf.as_mut_ptr() as *mut _, buf.len());
    }
}

/// Block on the wake eventfd only (used while a game is being tracked, so
/// stdin is ignored until the game ends). Returns when the eventfd fires.
fn wait_for_wake(wake_fd: c_int) {
    let mut readfds = ffi::FdSet::new();
    readfds.set(wake_fd);
    // SAFETY: see select_stdin_or_wake.
    unsafe {
        ffi::select(
            wake_fd + 1,
            &mut readfds,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        );
    }
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

/// Run the watch loop with dynamic game tracking.
///
/// Two states, distinguished by `tracker.game_ppid`:
///
/// - `game_ppid == 0` — scan state. Block on `select(2)` over stdin, the
///   wake eventfd, and the periodic scan timerfd. A stdin line triggers
///   `scan_by_game_name`; a timer tick or scheduler-side wake triggers
///   `steam_scan_proc`. Catching the timerfd here is what makes the
///   "scheduler started while the game is already running" case work —
///   nothing else will wake us, so we tick ourselves.
/// - `game_ppid != 0` — tracking state. Block on the wake eventfd only;
///   the timer is not watched while tracking. On wake, either the
///   scheduler reported the alive count hit zero (a transient drop —
///   confirm by re-scanning) or the game's own PPID exit was caught
///   (process_event cleared game_ppid, so we see 0 here and fall back
///   into the scan state silently).
///
/// Shutdown: `shutdown.store(true)` + `tracker.signal_wake()`. The scan
/// thread checks `shutdown` on every loop iteration.
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
    let wake_fd = tracker.wake_fd();
    let timer_fd = arm_scan_timer(cfg.scan_interval_secs);

    // Track the most recent trigger so a tracking-state wake can re-scan
    // via the same lookup method that originally found the game.
    let mut last_trigger: Option<Trigger> = None;

    loop {
        if shutdown.load(Ordering::Acquire) {
            break;
        }

        if tracker.game_ppid.load(Ordering::Acquire) == 0 {
            // ---- scan state ----
            let (stdin_ready, wake_ready, timer_ready) =
                select_scan_fds(stdin_fd, wake_fd, timer_fd);

            if wake_ready {
                tracker.consume_wake();
                if shutdown.load(Ordering::Acquire) {
                    break;
                }
            }
            if timer_ready {
                consume_timer(timer_fd);
            }

            // Either the periodic timer or a scheduler-side wake should
            // run the environment scan. (Right now the scheduler doesn't
            // emit wakes while game_ppid==0, but if a future caller does,
            // the right reaction is "re-scan", same as the timer tick.)
            if (timer_ready || wake_ready) && !shutdown.load(Ordering::Acquire) {
                if let Some(m) = steam_scan_proc() {
                    on_match(Trigger::Timer, &m);
                    tracker.track(m.ppid, m.procs.len() as i32);
                    last_trigger = Some(Trigger::Timer);
                }
            }

            if stdin_ready {
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
                if let Some(m) = scan_by_game_name(&name) {
                    let trigger = Trigger::GameName(name);
                    on_match(trigger.clone(), &m);
                    tracker.track(m.ppid, m.procs.len() as i32);
                    last_trigger = Some(trigger);
                }
            }
        } else {
            // ---- tracking state ----
            // The wake fd is the only thing that pulls us out of here.
            // Wakes mean: alive count hit zero (transient cutscene drop —
            // confirm by re-scanning) OR game PPID exited (process_event
            // cleared game_ppid; we'll see 0 next iteration and switch
            // back to scan state) OR shutdown.
            wait_for_wake(wake_fd);
            tracker.consume_wake();

            if shutdown.load(Ordering::Acquire) {
                break;
            }
            // If game_ppid was cleared (PPID exited path), do nothing
            // here — the next loop iteration enters scan state and
            // continues. Otherwise, re-scan to disambiguate transient
            // zero vs real exit.
            if tracker.game_ppid.load(Ordering::Acquire) == 0 {
                println!("[game] Game appears to have ended — you can type a game/task name to add one.");
                continue;
            }
            let Some(trigger) = last_trigger.as_ref() else {
                continue;
            };
            match rescan(trigger) {
                // Still running — a transient zero (e.g. cutscene).
                // Re-track quietly; the user never sees this.
                Some(again) => tracker.track(again.ppid, again.procs.len() as i32),
                // Re-scan found nothing: the game has truly ended.
                None => {
                    tracker.clear();
                    println!("[game] Game appears to have ended — you can type a game/task name to add one.");
                }
            }
        }
    }

    if timer_fd >= 0 {
        // SAFETY: we own this fd and the loop is done with it.
        unsafe { ffi::close(timer_fd); }
    }
}
