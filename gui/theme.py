"""Centralised colour / style choices, so the whole dashboard stays
consistent and a future re-skin only touches one file."""

# i5-13500 hybrid layout, mirroring the actual demo machine. Used for the
# per-CPU strip and to colour P/E badges.
PCORES = list(range(0, 12))   # 6 physical P-cores × SMT2
ECORES = list(range(12, 20))  # 8 E-cores
TOTAL_CPUS = len(PCORES) + len(ECORES)

# Warm hues for P-cores (high-perf, "hot"), cool hues for E-cores
# (efficiency, "calm"). Picked so a glance at the CPU strip immediately
# says "left half = perf, right half = efficiency".
PCORE_COLOR = "#ff6b6b"   # coral red
ECORE_COLOR = "#4dabf7"   # sky blue

# scx_teddy tiers (from intf.h: CRITICAL / INTERACTIVE / NORMAL / BATCH).
# Brighter for higher priority — eye drawn to critical tasks.
TIER_COLORS = {
    0: "#ff5252",  # CRITICAL — red, alarming
    1: "#ffb84d",  # INTERACTIVE — amber
    2: "#69db7c",  # NORMAL — green
    3: "#868e96",  # BATCH — grey, fades into the background
}
TIER_NAMES = {0: "CRITICAL", 1: "INTERACTIVE", 2: "NORMAL", 3: "BATCH"}

# Cluster palette (matches matplotlib tab10 so it lines up with viz.py
# t-SNE if we eventually wire the real model in).
CLUSTER_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def pe_badge(p_pct: float) -> str:
    """Three-state P/E affinity label for the task table. Thresholds picked
    so 'Either' covers genuinely mixed tasks, not just measurement noise."""
    if p_pct >= 0.85:
        return "P-only"
    if p_pct <= 0.15:
        return "E-only"
    return "Either"


def cluster_color(idx: int) -> str:
    return CLUSTER_COLORS[idx % len(CLUSTER_COLORS)]
