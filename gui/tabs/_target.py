"""Shared specialization-target ppid panel for the Collect and Classify tabs.

scx_teddy specializes a task family chosen *outside* it, via the /tmp control
files (see scx_runner.write_control and target_finder_helper/README.md). This
panel picks the target *ppid* — manually, or by running a scanner from
target_finder_helper/ (e.g. the Steam example), which keeps writing control_ppid
on its own.

Both tabs render this so "which ppid to optimize" is reachable from collect
(the CSV's `ancestor` column then marks the family) and from classify (the
family gets specialized scheduling live).

The target family's own model/config (control_model/control_config) is NOT
here — it lives in the Classify tab next to the default config editor, because
a config table belongs with the other config table and it must reach the Start
handler to flow into --target-* when the scheduler isn't running yet.

Streamlit note: every widget owns its value via a key prefixed with
`key_prefix`, so the Collect and Classify copies never collide and we never
write back into a widget's own key (that clobbers in-progress edits).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import scx_runner as runner

from ._common import proc_log_to_session, kick_rerun_soon


def _scanner_running() -> bool:
    h = st.session_state.get("scanner_handle")
    return h is not None and h.is_running()


def _render_manual_ppid(key_prefix: str, scanning: bool) -> None:
    """Manual ppid entry, writing control_ppid directly."""
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        ppid = st.number_input(
            "Target ppid", min_value=0, step=1, value=0,
            key=f"{key_prefix}_target_ppid_input",
            label_visibility="collapsed")
    with c2:
        if st.button("Set target", key=f"{key_prefix}_set_ppid",
                     use_container_width=True, disabled=scanning):
            try:
                runner.set_target_ppid(int(ppid))
                st.toast(f"control_ppid → {int(ppid)}")
            except Exception as e:  # noqa: BLE001 — surface any sudo/IO failure
                st.error(f"Failed to write control_ppid: {e}")
    with c3:
        if st.button("Clear (0)", key=f"{key_prefix}_clear_ppid",
                     use_container_width=True, disabled=scanning):
            try:
                runner.clear_target_ppid()
                st.toast("control_ppid → 0")
            except Exception as e:  # noqa: BLE001
                st.error(f"Failed to clear control_ppid: {e}")
    if scanning:
        st.caption("A scanner is running and managing the target — stop it "
                   "(Scanner mode) before setting a ppid by hand.")


def _render_scanner(key_prefix: str, scanning: bool) -> None:
    """Run a scanner from target_finder_helper/ that keeps writing control_ppid.
    The dropdown is populated from the directory, so a future scanner shows up
    with no code change here."""
    scanners = runner.list_scanners()
    if not scanners:
        st.warning(f"No scanner scripts found in {runner.SCANNER_DIR}.")
        return

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        names = [str(p) for p in scanners]
        scanner_str = st.selectbox(
            "Scanner", options=names, format_func=lambda s: Path(s).name,
            key=f"{key_prefix}_scanner_choice")
    with c2:
        interval = st.number_input(
            "Scan interval (s)", min_value=1, value=5, step=1,
            key=f"{key_prefix}_scan_interval")
    with c3:
        st.write("")
        if not scanning:
            if st.button("▶ Start", key=f"{key_prefix}_start_scanner",
                         use_container_width=True):
                argv = runner.build_scanner_argv(Path(scanner_str), int(interval))
                st.session_state["scanner_log"] = ["$ " + runner.pretty_argv(argv)]
                try:
                    h = runner.ProcessHandle(
                        argv,
                        on_line=lambda s: (proc_log_to_session("scanner_log", s),
                                           kick_rerun_soon()),
                        on_exit=lambda rc: (proc_log_to_session(
                            "scanner_log", f"[exited rc={rc}]"), kick_rerun_soon()),
                        label="scanner")
                    st.session_state["scanner_handle"] = h
                    st.rerun()
                except OSError as e:
                    st.error(f"Launch failed: {e}")
        else:
            if st.button("■ Stop", key=f"{key_prefix}_stop_scanner",
                         use_container_width=True):
                # SIGINT: the scanner's Ctrl-C handler writes 0, clearing target.
                st.session_state["scanner_handle"].stop()
                st.toast("Sent SIGINT — scanner is clearing the target.")

    if "scanner_log" in st.session_state:
        st.code("\n".join(st.session_state["scanner_log"]), language="text")


def _render_ppid_picker(key_prefix: str) -> None:
    """Pick the target ppid via one of two modes, chosen by a radio (like the
    config editor's source radio): manual entry, or a scanner. Only the selected
    mode's UI shows, so the panel stays clean. A live scanner forces Scanner
    mode (so its Stop button stays reachable)."""
    scanning = _scanner_running()

    # Live readback of whatever is currently in control_ppid (scanner or manual).
    cur = runner.read_control_ppid()
    if cur:
        st.success(f"Current target ppid: **{cur}**")
    else:
        st.info("No target set (control_ppid = 0) — whole system uses the "
                "default scheduling.")

    # The radio owns its value via its key (no value=/index= to clobber on
    # rerun). A live scanner overrides the choice to Scanner regardless, so the
    # Stop button stays reachable — done after the widget so we don't fight its
    # stored state.
    mode = st.radio("ppid source", ["Manual", "Scanner"],
                    label_visibility="collapsed", horizontal=True,
                    key=f"{key_prefix}_ppid_mode")
    if scanning:
        mode = "Scanner"

    if mode == "Manual":
        _render_manual_ppid(key_prefix, scanning)
    else:
        _render_scanner(key_prefix, scanning)


def render_target_panel(key_prefix: str) -> None:
    """The specialization-target ppid controls (manual + scanner). `key_prefix`
    namespaces every widget key so two tabs can render this without colliding.

    The target family's own model/config (control_model/control_config) is NOT
    here — it lives next to the Classify default editor (a config table belongs
    with the other config table, and it must reach the Start handler to flow
    into --target-* when the scheduler isn't running yet). This panel is
    ppid-only, so it's safe inside an expander (all button-driven, no fragile
    in-progress editor state to lose on a background rerun)."""
    with st.expander("🎯 Specialization target (ppid)", expanded=False):
        _render_ppid_picker(key_prefix)
