#!/usr/bin/env python3
"""Steam game scanner — publishes the running game's ppid to scx_teddy.

This is one example of an *external scanner*: a standalone program that decides
which task family scx_teddy should specialize, and writes that family's ppid
into the control file. scx_teddy itself does no scanning — it just re-reads

    /tmp/scx_teddy/control

every few seconds (see --control-interval) and treats the integer there as the
specialization target (0 = none). So a scanner in ANY language can drive it:
decide a ppid however you like, then write it to that file. This one keys off
Steam's environment marker, but swapping the detection logic (comm match,
cgroup, window title, …) is just changing scan_once() — the contract with
scx_teddy never changes.

How it detects a game: Steam injects SteamGameId=<appid> (older: STEAM_GAME=)
into every process it launches for a game. Reading /proc/<pid>/environ (a
NUL-separated KEY=VALUE list) finds the whole game family.

Choosing the ppid: a naive "first hit wins" is fragile. Under Proton the tree
looks like

    pv-adverb ─┬─ armoredcore6.exe ...
               ├─ explorer.exe ...
               ├─ services.exe ...
               ├─ python3 ─── steam.exe ...   <- also carries SteamGameId
               └─ ...

If we happened to scan the python3→steam.exe branch first we'd pick the wrong,
tiny ppid. Instead we bucket every hit by its ppid and pick the ppid with the
MOST members — the real game family is by far the largest group under one
parent, so the count breaks the tie correctly.
"""

import os
import sys
import time
from collections import Counter

# Steam's game markers (the key, without the '='). Either may be present.
STEAM_ENV_KEYS = ("SteamGameId", "STEAM_GAME")

# Proton/Steam plumbing that also inherits the marker but isn't the game itself;
# excluded so it doesn't skew the per-ppid member counts.
STEAM_INFRA = {"reaper", "srt-bwrap", "pv-adverb", "steam.exe"}

CONTROL_PATH = "/tmp/scx_teddy/control"
SCAN_INTERVAL_SECS = 5


def read_environ(pid: str) -> dict[str, str]:
    """Parse /proc/<pid>/environ into a dict. Unreadable → empty dict."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return {}
    env = {}
    for kv in raw.split(b"\0"):
        if not kv:
            continue
        key, sep, value = kv.decode("utf-8", errors="replace").partition("=")
        if sep:
            env[key] = value
    return env


def read_comm(pid: str) -> str:
    """Read /proc/<pid>/comm. Unreadable → empty string."""
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            return f.read().decode("utf-8", errors="replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def read_ppid(pid: str) -> int:
    """Read PPID from /proc/<pid>/stat. Unreadable/malformed → 0.

    stat is `pid (comm) state ppid ...`; comm may contain spaces and parens, so
    split after the last ')'.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    rest = stat[stat.rfind(")") + 1:].split()
    try:
        return int(rest[1]) if len(rest) > 1 else 0
    except ValueError:
        return 0


def scan_once() -> int:
    """Return the ppid of the largest Steam-game family, or 0 if none running.

    Buckets every process carrying a Steam game marker (minus known plumbing)
    by its parent pid, then returns the parent with the most children.
    """
    by_ppid: Counter[int] = Counter()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        env = read_environ(pid)
        if not any(k in env for k in STEAM_ENV_KEYS):
            continue
        if read_comm(pid) in STEAM_INFRA:
            continue
        by_ppid[read_ppid(pid)] += 1

    if not by_ppid:
        return 0
    # Most-common ppid wins (the real game family is the biggest group).
    return by_ppid.most_common(1)[0][0]


def publish(ppid: int) -> None:
    """Atomically write the target ppid into the control file (.tmp + rename),
    so scx_teddy never reads a half-written value."""
    tmp = CONTROL_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(CONTROL_PATH), exist_ok=True)
        with open(tmp, "w") as f:
            f.write(str(ppid))
        os.replace(tmp, CONTROL_PATH)
    except OSError as e:
        print(f"[scanner] failed to write {CONTROL_PATH}: {e}", file=sys.stderr)


def main() -> None:
    interval = SCAN_INTERVAL_SECS
    if len(sys.argv) > 1:
        try:
            interval = max(1, int(sys.argv[1]))
        except ValueError:
            print(f"usage: {sys.argv[0]} [scan_interval_secs]", file=sys.stderr)
            sys.exit(2)

    print(f"[scanner] watching for Steam games, writing ppid -> {CONTROL_PATH} "
          f"every {interval}s (Ctrl-C to stop)")
    last = None
    try:
        while True:
            ppid = scan_once()
            if ppid != last:
                publish(ppid)
                if ppid:
                    print(f"[scanner] game family ppid = {ppid}")
                else:
                    print("[scanner] no game running — cleared target (0)")
                last = ppid
            time.sleep(interval)
    except KeyboardInterrupt:
        # Clear the target on exit so a stale ppid doesn't linger.
        publish(0)
        print("\n[scanner] stopped, cleared target")


if __name__ == "__main__":
    main()
