"""CPU topology discovery via sysfs.

The dashboard groups logical CPUs by max frequency. On hybrid hardware
(Intel P+E, ARM big.LITTLE) that yields two groups; on homogeneous hardware
it yields one; future tri-tier silicon (rumoured P + LP-E + E) will yield
three with zero code change.

Deliberately NOT using `/sys/devices/cpu_atom/` and `/sys/devices/cpu_core/`
— those exist only on Intel hybrid. The cpufreq policy path lives on
virtually every Linux machine with frequency scaling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Colour palette used to tag groups by index (sorted high→low max_freq).
# First entry = highest-freq group (e.g. P-core), last entry = lowest. Up to
# 5 distinct groups before we wrap; in practice we'll see 1–3 in the wild.
_GROUP_PALETTE = [
    "#ff6b6b",  # coral — fastest (P-core / big)
    "#4dabf7",  # sky   — slower  (E-core / LITTLE)
    "#82c91e",  # lime  — slowest (LP-E / future tri-tier)
    "#fab005",  # amber
    "#cc5de8",  # plum
]


@dataclass(frozen=True)
class CoreGroup:
    """One frequency-tier of logical CPUs."""
    name: str           # human-readable label, e.g. "P-core" / "E-core" / "tier-0"
    cpus: tuple[int, ...]
    max_freq_khz: int
    color: str          # hex, picked from _GROUP_PALETTE

    @property
    def max_freq_ghz(self) -> float:
        return self.max_freq_khz / 1_000_000

    def __contains__(self, cpu: int) -> bool:
        return cpu in self.cpus


def _read_cpufreq() -> dict[int, int]:
    """Map logical CPU → cpuinfo_max_freq (kHz). CPUs without a cpufreq
    policy (rare; usually means freq scaling disabled in kernel) are
    omitted; the caller treats them as "unknown" and lumps into one
    fallback group."""
    out: dict[int, int] = {}
    policies = Path("/sys/devices/system/cpu/cpufreq").glob("policy*")
    for p in policies:
        try:
            related = (p / "related_cpus").read_text().split()
            max_khz = int((p / "cpuinfo_max_freq").read_text().strip())
        except (OSError, ValueError):
            continue
        for c in related:
            out[int(c)] = max_khz
    return out


def _cpu_count() -> int:
    """Total logical CPUs (online). Used as the fallback when cpufreq is
    unavailable so we still produce one group covering everything."""
    try:
        online = Path("/sys/devices/system/cpu/online").read_text().strip()
    except OSError:
        # Last-ditch: count /sys/devices/system/cpu/cpuN dirs.
        return len(list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*")))
    # online is a range expression like "0-19" or "0,2-5".
    n = 0
    for piece in online.split(","):
        if "-" in piece:
            a, b = piece.split("-")
            n += int(b) - int(a) + 1
        else:
            n += 1
    return n


def _name_for_index(idx: int, total: int) -> str:
    """Pick a label for a group given its position (0 = highest freq).

    Two-tier is special-cased to the familiar P-core / E-core wording, which
    is what someone running this on a hybrid laptop expects to see. Other
    counts fall back to generic ``tier-N``."""
    if total == 1:
        return "core"
    if total == 2:
        return "P-core" if idx == 0 else "E-core"
    return f"tier-{idx}"


def discover() -> list[CoreGroup]:
    """Return one CoreGroup per distinct max_freq, sorted high→low.

    Edge cases:
      - cpufreq unavailable (e.g. inside some VMs): one group named "core"
        covering all CPUs, freq=0.
      - All CPUs same freq: one group, named per the two/three-tier rule
        above (i.e. "core").
    """
    freq_by_cpu = _read_cpufreq()
    if not freq_by_cpu:
        n = _cpu_count()
        return [CoreGroup(name="core", cpus=tuple(range(n)),
                          max_freq_khz=0, color=_GROUP_PALETTE[0])]

    # Bucket by exact max_freq. Sort descending so index 0 is the fastest.
    by_freq: dict[int, list[int]] = {}
    for cpu, khz in freq_by_cpu.items():
        by_freq.setdefault(khz, []).append(cpu)
    sorted_freqs = sorted(by_freq.keys(), reverse=True)

    groups = []
    for i, khz in enumerate(sorted_freqs):
        cpus = tuple(sorted(by_freq[khz]))
        groups.append(CoreGroup(
            name=_name_for_index(i, len(sorted_freqs)),
            cpus=cpus,
            max_freq_khz=khz,
            color=_GROUP_PALETTE[i % len(_GROUP_PALETTE)],
        ))
    return groups


def group_of(cpu: int, groups: list[CoreGroup]) -> CoreGroup | None:
    """Find which group a logical CPU belongs to (or None if unknown)."""
    for g in groups:
        if cpu in g:
            return g
    return None


def total_cpus(groups: list[CoreGroup]) -> int:
    """Sum of CPUs across groups — used to size the per-CPU strip."""
    return sum(len(g.cpus) for g in groups)
