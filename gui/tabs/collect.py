"""Collect tab — runs `sudo -E scx_teddy --mode collect`, streams its log,
manages the saved CSVs in /tmp.

All side effects flow through `runner` (`scx_runner.py`, sibling of
`app.py`): start/stop the scheduler, pick the timestamped output path,
copy CSVs out of tmpfs, clear /tmp.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import scx_runner as runner

from ._common import proc_log_to_session, kick_rerun_soon
from . import _target


def render():
    st.title("Collect · run scx_teddy and stream its log")

    # Output dir picker — default /tmp tmpfs, optional SSD path.
    c1, c2 = st.columns([1, 3])
    with c1:
        custom = st.checkbox("Custom output dir", value=False)
    with c2:
        if custom:
            out_dir_str = st.text_input(
                "Output directory",
                value=st.session_state.get("collect_out_dir",
                                          str(runner.DEFAULT_DATA_DIR)),
                label_visibility="collapsed")
            st.session_state["collect_out_dir"] = out_dir_str
            out_dir = Path(out_dir_str)
        else:
            out_dir = None
            st.caption(f"→ output goes to **{runner.DEFAULT_DATA_DIR}** "
                       "(tmpfs / RAM). Tick the box to send straight to SSD.")

    c1, c2, c3 = st.columns(3)
    cycle = c1.number_input("Cycle (s)", min_value=1, value=600, step=10)
    maxrt = c2.number_input("Max runtime (s, 0=∞)", min_value=0, value=0, step=30)
    checkpoint = c3.checkbox(
        "Checkpoint CSV each cycle", value=False,
        help="Write the CSV every cycle instead of once on shutdown. Off by "
             "default: a mid-run checkpoint can record a task's `ancestor` "
             "before a freshly detected target ppid has propagated, mislabelling "
             "the family. Enable only if you need crash-safety over correctness.")

    # Optional: pick a specialization target ppid. In collect mode this just
    # marks the family — the CSV's `ancestor` column converges to the target so
    # the family's tasks are identifiable in analysis.
    _target.render_target_panel("collect")

    handle = st.session_state.get("collect_handle")
    running = handle is not None and handle.is_running()

    c_start, c_stop, c_clear = st.columns([1, 1, 1])
    with c_start:
        if st.button("▶ Start collect", disabled=running,
                     use_container_width=True, type="primary"):
            if not runner.SCX_TEDDY_BIN.exists():
                st.error(f"scx_teddy binary missing at {runner.SCX_TEDDY_BIN}.")
            else:
                csv = runner.timestamped_csv(directory=out_dir)
                argv = runner.build_collect_argv(
                    output=csv, duration=int(cycle),
                    max_runtime=int(maxrt), checkpoint=checkpoint)
                st.session_state["collect_log"] = ["$ " + runner.pretty_argv(argv)]
                st.session_state["collect_csv"] = str(csv)
                try:
                    h = runner.ProcessHandle(
                        argv,
                        on_line=lambda s: (proc_log_to_session("collect_log", s),
                                           kick_rerun_soon()),
                        on_exit=lambda rc: (proc_log_to_session(
                            "collect_log", f"[exited rc={rc}]"),
                            kick_rerun_soon()),
                        label="collect")
                    runner.note_csv(csv)
                    st.session_state["collect_handle"] = h
                    st.rerun()
                except OSError as e:
                    st.error(f"Launch failed: {e}")

    with c_stop:
        if st.button("■ Stop (SIGINT)", disabled=not running,
                     use_container_width=True):
            handle.stop()
            st.toast("Sent SIGINT — collect is flushing the CSV.")

    with c_clear:
        if st.button("🧹 Clear .csv in /tmp", use_container_width=True):
            n = runner.clear_data("*.csv")
            st.toast(f"Deleted {n} .csv from {runner.DEFAULT_DATA_DIR}.")

    # Status + path
    if running:
        st.success(f"Running · writing to `{st.session_state.get('collect_csv','?')}`")
    elif handle is not None:
        st.info(f"Finished · last output: `{st.session_state.get('collect_csv','?')}`")

    # Log
    st.markdown("##### Log")
    log = "\n".join(st.session_state.get("collect_log", []))
    st.code(log or "(idle — press Start)", language="text")

    # Saved-CSVs list with multi-select copy-to.
    st.markdown("##### Saved CSVs in /tmp (RAM — lost on reboot)")
    csvs = runner.list_csvs()
    if not csvs:
        st.caption("No CSVs yet.")
        return
    names = [p.name for p in csvs]
    picked = st.multiselect("Select to copy", names,
                            label_visibility="collapsed")
    c1, c2 = st.columns([2, 1])
    with c1:
        dest = st.text_input(
            "Copy destination directory",
            value=st.session_state.get("copy_dest", str(Path.home())))
        st.session_state["copy_dest"] = dest
    with c2:
        st.write("")
        if st.button("📋 Copy selected", use_container_width=True):
            copied = 0
            try:
                name_to_path = {p.name: p for p in csvs}
                for n in picked:
                    copied += len(runner.copy_to(name_to_path[n], Path(dest)))
                st.toast(f"Copied {copied} file(s) → {dest}")
            except OSError as e:
                st.error(f"Copy failed: {e}")
