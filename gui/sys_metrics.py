"""Real-time /proc sampling for the Overall tab.

`Sampler.sample()` returns a fresh `Snapshot` each call. The sampler is
stateful because CPU% is a delta: it needs the previous tick counts to
divide (jiffies_busy_now - jiffies_busy_prev) / (jiffies_total_now -
jiffies_total_prev). First call returns zeros for CPU%; second call is the
first real measurement.

Per-task scanning of `/proc/<pid>/stat` is the slow part. With ~500 procs
on a typical desktop, one full scan takes a few ms — fine at 1 Hz. We open
files lazily and tolerate disappearances (process exited between listdir
and read).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path


SC_CLK_TCK = os.sysconf("SC_CLK_TCK")  # usually 100 — jiffies per second
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")  # usually 4096 — for RSS bytes


# ---------------------------------------------------------------------------
# Snapshot — what one sample() call returns
# ---------------------------------------------------------------------------
@dataclass
class TaskRow:
    """One row for the Top-N task table. Real fields are filled; the
    scx_teddy-side fields (tier/slice/cluster) stay as None until a real
    export path exists — the UI shows '—' for those."""
    tid: int
    tgid: int | None
    ppid: int | None
    comm: str
    cpu_pct: float
    ram_mb: float


@dataclass
class Snapshot:
    cpu_total_pct: float
    per_cpu_pct: list[float]          # index = logical CPU
    ram_used_mb: float
    ram_total_mb: float
    tasks: list[TaskRow]
    wall_dt: float                    # seconds since previous sample


# ---------------------------------------------------------------------------
# Sampler — stateful between calls
# ---------------------------------------------------------------------------
@dataclass
class _CpuTicks:
    """jiffy counters from one row of /proc/stat. `total` includes idle;
    `busy = total - idle - iowait`. We treat iowait as idle to match what
    htop / `top` show."""
    total: int
    idle: int


@dataclass
class _TaskTicks:
    utime: int          # user-mode jiffies
    stime: int          # kernel-mode jiffies


class Sampler:
    """One per Streamlit session. `sample()` is called from the main thread
    (no locking)."""

    def __init__(self):
        # /proc/stat snapshots: index 0 = "cpu" aggregate, then per-CPU.
        self._prev_cpu: list[_CpuTicks] = []
        # /proc/<pid>/stat snapshots, keyed by tid.
        self._prev_task: dict[int, _TaskTicks] = {}
        self._prev_wall: float = 0.0

    # ------------------------------------------------------------------
    # /proc/stat (whole-machine + per-CPU utilisation)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_proc_stat() -> list[_CpuTicks]:
        """Parse /proc/stat. First 'cpu' line is the aggregate, then one
        'cpuN' line per logical CPU in N order. Fields are jiffies:
        user nice system idle iowait irq softirq steal …
        We collapse to (total, idle). 'iowait' is folded into idle to match
        htop's "CPU is waiting on disk = not busy" convention."""
        out: list[_CpuTicks] = []
        with open("/proc/stat") as f:
            for line in f:
                if not line.startswith("cpu"):
                    break
                parts = line.split()
                # parts[0] is the label ("cpu", "cpu0", ...); numbers follow.
                fields = [int(x) for x in parts[1:]]
                user, nice, system, idle = fields[0], fields[1], fields[2], fields[3]
                iowait = fields[4] if len(fields) > 4 else 0
                total = sum(fields)
                out.append(_CpuTicks(total=total, idle=idle + iowait))
        return out

    @staticmethod
    def _delta_pct(prev: _CpuTicks, now: _CpuTicks) -> float:
        d_total = now.total - prev.total
        d_idle = now.idle - prev.idle
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (d_total - d_idle) / d_total))

    # ------------------------------------------------------------------
    # /proc/meminfo
    # ------------------------------------------------------------------
    @staticmethod
    def _read_meminfo() -> tuple[float, float]:
        """Return (used_mb, total_mb). 'Used' matches what `free -m` shows
        as the 'used' column: total - free - buffers - cached - sreclaimable.
        That excludes the page cache, which is the number a layperson means
        when they say 'RAM in use'."""
        wanted = {"MemTotal", "MemFree", "Buffers", "Cached", "SReclaimable"}
        vals: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in wanted:
                    vals[key] = int(rest.strip().split()[0])  # kB
                    if len(vals) == len(wanted):
                        break
        total = vals.get("MemTotal", 0)
        free = vals.get("MemFree", 0)
        buffers = vals.get("Buffers", 0)
        cached = vals.get("Cached", 0)
        sreclaim = vals.get("SReclaimable", 0)
        used = total - free - buffers - cached - sreclaim
        return used / 1024, total / 1024  # kB → MB

    # ------------------------------------------------------------------
    # /proc/<pid>/stat
    # ------------------------------------------------------------------
    @staticmethod
    def _iter_tids():
        """Yield every kernel-visible thread tid. /proc/<pid>/task/<tid>/stat
        gives per-thread CPU counters; /proc/<pid>/stat is per-process. We
        scan both processes AND their threads so the table can show thread
        groups, which is what scx_teddy schedules."""
        proc = Path("/proc")
        for p in proc.iterdir():
            if not p.name.isdigit():
                continue
            task_dir = p / "task"
            if not task_dir.is_dir():
                continue
            try:
                for t in task_dir.iterdir():
                    if t.name.isdigit():
                        yield int(t.name), int(p.name)  # tid, tgid
            except OSError:
                continue  # process exited mid-scan

    @staticmethod
    def _read_task_stat(tid: int, tgid: int) -> tuple[_TaskTicks, str, int, int] | None:
        """Read /proc/<tgid>/task/<tid>/{stat,status} (or fall back if
        threads aren't accessible). Returns (ticks, comm, ppid, rss_pages)
        or None on disappearance. Field layout of /proc/.../stat is fragile
        because `comm` can contain spaces/parentheses — we slice on the
        last ')' to be safe."""
        path = Path(f"/proc/{tgid}/task/{tid}/stat")
        try:
            raw = path.read_text()
        except OSError:
            return None
        # Format: pid (comm) state ppid ... utime stime ... rss ...
        # `comm` is in parens and may itself contain ')'; split on the LAST ').
        close = raw.rfind(")")
        if close < 0:
            return None
        comm = raw[raw.find("(") + 1: close]
        rest = raw[close + 2:].split()
        # After ')' the fields are: state(0) ppid(1) pgrp pgid session
        # tty_nr tpgid flags minflt cminflt majflt cmajflt utime(13)
        # stime(14) cutime cstime priority nice nthr itreal stime0 vsize
        # rss(23) ...
        try:
            ppid = int(rest[1])
            utime = int(rest[11])
            stime = int(rest[12])
            rss_pages = int(rest[21])
        except (IndexError, ValueError):
            return None
        return _TaskTicks(utime=utime, stime=stime), comm, ppid, rss_pages

    # ------------------------------------------------------------------
    # Public: one sample
    # ------------------------------------------------------------------
    def sample(self) -> Snapshot:
        now = time.monotonic()
        wall_dt = max(1e-6, now - self._prev_wall) if self._prev_wall else 1.0
        self._prev_wall = now

        # CPU (aggregate + per-CPU)
        cpu_now = self._read_proc_stat()
        if not self._prev_cpu:
            cpu_total = 0.0
            per_cpu = [0.0] * (len(cpu_now) - 1)
        else:
            cpu_total = self._delta_pct(self._prev_cpu[0], cpu_now[0])
            # _prev_cpu[1:] and cpu_now[1:] should match in length normally,
            # but be defensive in case the CPU hotplugged.
            per_cpu = []
            for i in range(min(len(self._prev_cpu) - 1, len(cpu_now) - 1)):
                per_cpu.append(self._delta_pct(self._prev_cpu[i + 1], cpu_now[i + 1]))
        self._prev_cpu = cpu_now

        # Memory
        ram_used, ram_total = self._read_meminfo()

        # Per-task CPU% over the wall_dt window. CPU% here is "fraction of
        # one core", matching what htop calls CPU% — a 4-thread compile run
        # can show >100% on a multicore box.
        # ticks_used_in_dt / (SC_CLK_TCK * dt) → cores used → ×100 = pct.
        new_task_ticks: dict[int, _TaskTicks] = {}
        rows: list[TaskRow] = []
        for tid, tgid in self._iter_tids():
            r = self._read_task_stat(tid, tgid)
            if r is None:
                continue
            ticks, comm, ppid, rss_pages = r
            new_task_ticks[tid] = ticks
            prev = self._prev_task.get(tid)
            if prev is None:
                cpu_pct = 0.0
            else:
                d_jiffies = (ticks.utime + ticks.stime) - (prev.utime + prev.stime)
                cpu_pct = max(0.0, 100.0 * d_jiffies / (SC_CLK_TCK * wall_dt))
            rows.append(TaskRow(
                tid=tid, tgid=tgid, ppid=ppid, comm=comm,
                cpu_pct=cpu_pct,
                ram_mb=rss_pages * PAGE_SIZE / (1024 * 1024),
            ))
        self._prev_task = new_task_ticks

        return Snapshot(
            cpu_total_pct=cpu_total,
            per_cpu_pct=per_cpu,
            ram_used_mb=ram_used,
            ram_total_mb=ram_total,
            tasks=rows,
            wall_dt=wall_dt,
        )
