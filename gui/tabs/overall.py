"""Overall tab — real-time system pulse from /proc.

htop-like dashboard: total CPU/RAM, per-CPU strip, top-N task table.

Data source is entirely `/proc` (via sys_metrics.Sampler) — scx_teddy is
NOT involved in sampling. The cluster / prio / cpu_kind / slice columns are
joined in by tid from scx_teddy's classify snapshot (`runner.read_snapshot()`),
if one exists: those are the only fields /proc can't give us. No classify run
(or a task that stayed asleep this cycle, so it isn't in the snapshot) → that
task's scx_teddy columns stay blank. We deliberately don't ask scx_teddy to publish any
*extra* per-second state for the dashboard — the snapshot is the same data the
scheduler already computes each classify cycle.

Refresh model: the Streamlit script reruns every ~1 s (sleep+rerun loop in
app.py); on each rerun this tab calls `sampler.sample()` once. Sampler is
stateful (CPU% is a delta) and lives in `st.session_state`, so it survives
reruns. Hist buffers are also session_state so the sparklines persist
across tab switches even though no sample happens while we're elsewhere
(short-pause is OK; multi-minute gaps will show a flat region — fine for
demo).
"""

from __future__ import annotations

from collections import deque

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import scx_runner as runner
import sys_metrics
import sys_topology


# How many samples to keep in the rolling sparkline buffers. ~2 minutes at
# 1 Hz; long enough to look like "history", short enough that the cost of
# converting to a list each plot is invisible.
HIST_LEN = 120

# Colour palette for the small panels at top, kept consistent with the
# stash mock so the look reads as one product. RGB strings (not hex) so the
# sparkline helper can derive a low-alpha fill colour.
CPU_LINE = "rgb(255,107,107)"
RAM_LINE = "rgb(77,171,247)"


def _ensure_state():
    """Lazy-init the per-session sampler, topology, and history buffers."""
    if "overall_sampler" not in st.session_state:
        st.session_state.overall_sampler = sys_metrics.Sampler()
        st.session_state.overall_groups = sys_topology.discover()
        st.session_state.overall_cpu_hist = deque(maxlen=HIST_LEN)
        st.session_state.overall_ram_hist = deque(maxlen=HIST_LEN)


def _sparkline(values, colour: str, height: int = 70) -> go.Figure:
    """Minimal area chart — no axes, no labels. The metric value above
    carries the magnitude; the sparkline only shows the trend."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        y=list(values), mode="lines",
        line=dict(color=colour, width=2),
        fill="tozeroy",
        fillcolor=colour.replace(")", ", 0.15)").replace("rgb", "rgba"),
    ))
    fig.update_layout(
        template="plotly_dark", height=height,
        margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, range=[0, 100]),
    )
    return fig


def _per_cpu_strip(per_cpu, groups: list[sys_topology.CoreGroup]) -> go.Figure:
    """One bar per logical CPU, colour comes from the group it belongs to.
    Works for any number of frequency tiers (1, 2, or future 3+)."""
    n = len(per_cpu)
    colours = []
    for c in range(n):
        g = sys_topology.group_of(c, groups)
        colours.append(g.color if g else "#888")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[f"CPU{c}" for c in range(n)],
        y=list(per_cpu),
        marker_color=colours,
        hovertemplate="%{x}: %{y:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_dark", height=180,
        margin=dict(l=10, r=10, t=10, b=30), showlegend=False,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        yaxis=dict(range=[0, 100], title=None, ticksuffix="%"),
        xaxis=dict(title=None, tickfont=dict(size=10)),
    )
    return fig


def _group_legend_md(groups: list[sys_topology.CoreGroup]) -> str:
    """Render a one-line markdown legend of the discovered groups, used
    as the heading for the per-CPU strip. Looks like:
    "P-core 0–11 @ 4.8 GHz · E-core 12–19 @ 3.5 GHz"."""
    pieces = []
    for g in groups:
        cpu_range = (f"{g.cpus[0]}–{g.cpus[-1]}"
                     if g.cpus[-1] - g.cpus[0] == len(g.cpus) - 1
                     else ",".join(str(c) for c in g.cpus))
        freq = f"{g.max_freq_ghz:.1f} GHz" if g.max_freq_khz > 0 else ""
        label = f"<span style='color:{g.color}'>{g.name} {cpu_range}</span>"
        if freq:
            label += f" @ {freq}"
        pieces.append(label)
    return " · ".join(pieces)


def render():
    st.title("Overall · system pulse")
    _ensure_state()
    _live_body()


@st.fragment(run_every="1s")
def _live_body():
    """The auto-refreshing part of the tab. As a fragment with run_every, this
    reruns itself every second WITHOUT rerunning the whole app — and only while
    Overall is the visible tab. That's the whole point: previously a global
    1 Hz st.rerun() (kept alive once Overall had ever been opened) would wipe
    out half-typed text in the Collect/Train/Classify tabs. Scoping the refresh
    to this fragment means typing elsewhere is no longer interrupted.

    Everything stateful (Sampler, history deques) still lives in session_state
    (seeded by _ensure_state before this is called), so it survives both
    fragment reruns and tab switches."""
    sampler: sys_metrics.Sampler = st.session_state.overall_sampler
    groups: list[sys_topology.CoreGroup] = st.session_state.overall_groups
    cpu_hist: deque = st.session_state.overall_cpu_hist
    ram_hist: deque = st.session_state.overall_ram_hist

    # Sample once per rerun. The first ever sample returns zeros (CPU% is a
    # delta and there's no prior tick to subtract from); we still push it so
    # the history grows immediately.
    snap = sampler.sample()
    cpu_hist.append(snap.cpu_total_pct)
    ram_pct = (100 * snap.ram_used_mb / snap.ram_total_mb
               if snap.ram_total_mb else 0)
    ram_hist.append(ram_pct)

    # ---- Top metrics row -------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total CPU", f"{snap.cpu_total_pct:.1f}%")
        st.plotly_chart(_sparkline(cpu_hist, CPU_LINE),
                        width="stretch",
                        config={"displayModeBar": False})
    with c2:
        st.metric("RAM used",
                  f"{snap.ram_used_mb/1024:.1f} / "
                  f"{snap.ram_total_mb/1024:.0f} GB")
        st.plotly_chart(_sparkline(ram_hist, RAM_LINE),
                        width="stretch",
                        config={"displayModeBar": False})
    with c3:
        st.metric("Active tasks", f"{len(snap.tasks)}")
        st.caption(f"sampling /proc/<pid>/task/<tid>/stat · "
                   f"Δt = {snap.wall_dt:.2f}s")
    with c4:
        # Show group composition rather than P/E counts (more honest on
        # hybrid != Intel-style hardware). One line per group.
        lines = []
        for g in groups:
            lines.append(f"<span style='color:{g.color}'>●</span> "
                         f"{g.name}: {len(g.cpus)}")
        st.markdown("**Core groups**<br>" + "<br>".join(lines),
                    unsafe_allow_html=True)

    st.divider()

    # ---- Per-CPU strip ---------------------------------------------------
    st.markdown(f"##### Per-CPU utilisation · {_group_legend_md(groups)}",
                unsafe_allow_html=True)
    st.plotly_chart(_per_cpu_strip(snap.per_cpu_pct, groups),
                    width="stretch",
                    config={"displayModeBar": False})

    # ---- Filters ---------------------------------------------------------
    # Three optional filters: comm substring (case-insensitive), tgid list,
    # ppid list. All-empty = show everything. Each widget owns its value via
    # its own key (no value= seed) — these live inside the 1 Hz fragment, so
    # passing value=session_state[key] would let a refresh clobber a half-typed
    # filter on the next tick. Key-only state survives the fragment reruns.
    st.markdown("##### Filter")
    f1, f2, f3 = st.columns(3)
    comm_q = f1.text_input(
        "comm contains", placeholder="e.g. firefox", key="ov_comm",
        help="Case-insensitive substring match against the task's comm.")
    tgid_q = f2.text_input(
        "tgid in", placeholder="e.g. 1234, 5678", key="ov_tgid",
        help="Comma-separated tgids; empty = any.")
    ppid_q = f3.text_input(
        "ppid in", placeholder="e.g. 1, 4321", key="ov_ppid",
        help="Comma-separated ppids; empty = any.")

    def _parse_int_list(s: str) -> set[int]:
        out: set[int] = set()
        for piece in s.replace(" ", "").split(","):
            if piece.isdigit():
                out.add(int(piece))
        return out

    tgid_set = _parse_int_list(tgid_q)
    ppid_set = _parse_int_list(ppid_q)
    comm_needle = comm_q.strip().lower()

    def _matches(t):
        if comm_needle and comm_needle not in t.comm.lower():
            return False
        if tgid_set and t.tgid not in tgid_set:
            return False
        if ppid_set and t.ppid not in ppid_set:
            return False
        return True

    filtered = [t for t in snap.tasks if _matches(t)] if (
        comm_needle or tgid_set or ppid_set) else snap.tasks
    filter_active = (comm_needle or tgid_set or ppid_set)

    # ---- Task table ------------------------------------------------------
    # Sort all tasks by CPU% descending and show them all. Streamlit's
    # dataframe widget virtualises rows (only the visible window is rendered),
    # so a few thousand rows is fine — render cost is roughly the same as
    # 20 rows because the off-screen rows aren't drawn until the user scrolls.
    # The scx_teddy-side fields are placeholder dashes — present so the
    # column layout matches the eventual real version and reviewers can see
    # "this is where tier/cluster will live".
    # Classify snapshot, keyed by tid — but only trust it when THIS GUI session
    # is the one running classify. The snapshot file lingers in /tmp after a run
    # ends, so reading it unconditionally would show stale data from a previous
    # run (e.g. right after reopening the GUI). The live classify handle in
    # session_state is the authority: no handle (or it exited) → no snapshot,
    # columns stay blank. Reopening the GUI clears session_state, so a leftover
    # file is correctly ignored.
    classify_handle = st.session_state.get("classify_handle")
    classify_live = classify_handle is not None and classify_handle.is_running()
    snapshot = (runner.read_snapshot() or {}) if classify_live else {}

    top = sorted(filtered, key=lambda t: t.cpu_pct, reverse=True)
    rows = []
    for t in top:
        s = snapshot.get(t.tid)
        rows.append({
            "tid": t.tid,
            "tgid": t.tgid,
            "comm": t.comm,
            "ppid": t.ppid,
            "CPU%": round(t.cpu_pct, 1),
            "RAM(MB)": round(t.ram_mb, 1),
            # 🎯 marks a task in the target family (its ancestor converged to
            # the control ppid, so it was scheduled with the target set). Blank
            # — not "" vs "🎯" both being strings is fine here, the column is
            # all-string — for non-target so only targets stand out visually.
            "target": "🎯" if (s and s.get("is_target")) else "",
            # Joined from the classify snapshot by tid. Use None (not "—") for
            # the miss case so each column keeps one numeric dtype — a mixed
            # str/float column can't serialize to Arrow. Streamlit renders a
            # missing numeric cell as a blank, which reads the same as a dash.
            "cluster": s["cluster"] if s else None,
            "prio": s["prio"] if s else None,
            "cpu_kind": s["cpu_kind"] if s else None,
            "slice(ms)": round(s["slice_ns"] / 1e6, 3) if s else None,
        })
    df = pd.DataFrame(rows)
    if filter_active:
        header = (f"##### {len(filtered)} matching tasks "
                  f"of {len(snap.tasks)} · sorted by CPU%")
    else:
        header = f"##### {len(snap.tasks)} tasks · sorted by CPU%"
    if classify_live:
        note = (f"  <span style='color:#888;font-size:0.85rem'>"
                f"(cluster / prio / cpu_kind / slice from classify snapshot · "
                f"{len(snapshot)} tasks classified this cycle)</span>")
    else:
        note = ("  <span style='color:#888;font-size:0.85rem'>"
                "(cluster / prio / cpu_kind / slice blank — classify not "
                "running in this session)</span>")
    st.markdown(header + note, unsafe_allow_html=True)
    st.dataframe(
        df, width="stretch", hide_index=True, height=560,
        column_config={
            "CPU%": st.column_config.ProgressColumn(
                "CPU%", min_value=0, max_value=100, format="%.1f%%"),
            "RAM(MB)": st.column_config.NumberColumn("RAM(MB)", format="%.0f"),
            "target": st.column_config.TextColumn(
                "🎯", help="Marked when this task is in the target family "
                "(scheduled with the target set, not the default set)."),
            "cluster": st.column_config.NumberColumn(
                "cluster", help="KMeans cluster id this task was classified into."),
            "prio": st.column_config.NumberColumn(
                "prio", help="Scheduling priority from the config "
                "(0 = highest, 11 = lowest)."),
            "cpu_kind": st.column_config.NumberColumn(
                "cpu_kind", help="CPU-kind binding (0 = any; 1-based, "
                "1 = fastest)."),
            "slice(ms)": st.column_config.NumberColumn(
                "slice(ms)", format="%.3f",
                help="Time slice granted to this task, in milliseconds."),
        },
    )
