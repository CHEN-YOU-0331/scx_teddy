"""Classify tab — runs `sudo -E scx_teddy --mode classify --model M --config C`.

Pick a trained model JSON and edit a scheduling config table, press Start,
stream the log, Stop with SIGINT. This is the "use the model" counterpart to
Collect — same thin-shell pattern (everything shells out through `runner`,
control is SIGINT, the GUI can die and scx_teddy keeps running).

The model picker + config table are the shared `_config_editor` widget (the
specialization-target panel reuses the same one for the target family's config).

Live specialization (point at a target ppid, give that family its own
model/config) is driven through the /tmp control files via the shared
`_target.render_target_panel` — written while a run is live and hot-swapped by
scx_teddy on its next poll, no restart needed.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import scx_runner as runner

from ._common import proc_log_to_session, kick_rerun_soon
from . import _target
from . import _config_editor


# Session keys holding a target set that's been Applied but not yet handed to a
# scheduler (scheduler not running at Apply time) — Start picks these up as
# --target-model/--target-config so the set survives scx_teddy's init (which
# rebuilds the /tmp control files and would wipe anything written there early).
PENDING_TMODEL = "classify_pending_target_model"
PENDING_TCONFIG = "classify_pending_target_config"


def _render_target_set_editor(running: bool) -> None:
    """Target family's own model + config, sitting right under the default
    editor (a config table belongs next to the other config table). The target
    picks its OWN model (may differ from the default — the point of two
    SchedSets). Apply behaves differently by whether the scheduler is live:

      - running  → write control_model/control_config; scx_teddy hot-swaps it.
      - not yet  → stash the model + a serialized /tmp config in session_state;
                   the Start handler passes them as --target-* (writing the
                   control files now would be wiped by scx_teddy's init).
    """
    st.markdown("##### Target family model + config (optional)")
    st.caption("Gives the specialization target its OWN model + config (it can "
               "differ from the default above). ⚠️ This only takes effect once "
               "you also pick a **target ppid** below — without a target, no "
               "task is in the target family.")

    tmodel_str, tdf = _config_editor.render_model_and_config(
        "classify_tset", model_label="Target model JSON", with_save=True)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Apply target set", key="classify_apply_tset",
                     use_container_width=True):
            model_p = Path(tmodel_str) if tmodel_str else None
            if model_p is None or not model_p.exists():
                st.error(f"Target model not found: {tmodel_str}")
            elif tdf is None:
                st.error("No config table — pick a readable target model first.")
            else:
                try:
                    clusters, default = _config_editor.df_to_config(tdf)
                    config_p = runner.write_config(clusters, default)
                    if running:
                        runner.set_target_set(model_p, config_p)
                        st.toast("Target set → control files (hot-swapped).")
                    else:
                        st.session_state[PENDING_TMODEL] = str(model_p)
                        st.session_state[PENDING_TCONFIG] = str(config_p)
                        st.toast("Target set staged — applied on Start "
                                 "(--target-*).")
                except Exception as e:  # noqa: BLE001 — surface sudo/IO failure
                    st.error(f"Failed to apply target set: {e}")
    with c2:
        if st.button("Clear target set", key="classify_clear_tset",
                     use_container_width=True):
            st.session_state.pop(PENDING_TMODEL, None)
            st.session_state.pop(PENDING_TCONFIG, None)
            if running:
                try:
                    runner.clear_target_set()
                    st.toast("Target set cleared → default.")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Failed to clear control files: {e}")
            else:
                st.toast("Staged target set cleared.")

    pend = st.session_state.get(PENDING_TMODEL)
    if pend and not running:
        st.caption(f"⏳ Staged for next Start: target model `{pend}`")


def render():
    st.title("Classify · run scx_teddy with a trained model")

    handle = st.session_state.get("classify_handle")
    running = handle is not None and handle.is_running()

    # Model picker + config table (shared widget). Both modes serialize the
    # edited table to a fresh /tmp config on Start and point --config there —
    # the loaded file (if any) is only a seed, the original is never touched.
    model_str, edited_df = _config_editor.render_model_and_config(
        "classify", model_label="Model JSON", with_save=True)

    # Target family's own model + config, stacked under the default editor.
    st.divider()
    _render_target_set_editor(running)
    st.divider()

    # --- Specialization target ppid (manual / scanner) ----------------------
    # Which ppid the target set above applies to. Written to control_ppid; works
    # whether or not a run is live.
    _target.render_target_panel("classify")

    # --- Predict period (-c) -------------------------------------------------
    # In classify mode -c is how often the scheduler re-classifies every task.
    # scx_teddy's own default (600s) is far too slow to feel interactive, so we
    # default to 1s and let the user dial it up if prediction cost matters.
    period = st.number_input(
        "Predict period (s) · -c", min_value=1, value=1, step=1,
        help="How often scx_teddy re-classifies tasks. scx_teddy's built-in "
             "default is 600s; the GUI uses 1s unless you change it.")

    c_start, c_stop = st.columns(2)
    with c_start:
        if st.button("▶ Start classify", disabled=running, key="classify_start",
                     use_container_width=True, type="primary"):
            model_p = Path(model_str)
            # Serialize the edited table to a fresh /tmp config; no table (no
            # readable model) → no config to write → error out below.
            config_p = None
            if edited_df is not None:
                clusters, default = _config_editor.df_to_config(edited_df)
                config_p = runner.write_config(clusters, default)

            if not runner.SCX_TEDDY_BIN.exists():
                st.error(f"scx_teddy binary missing at {runner.SCX_TEDDY_BIN}.")
            elif not model_str or not model_p.exists():
                st.error(f"Model not found: {model_str}")
            elif config_p is None:
                st.error("No config table — pick a readable model first.")
            else:
                # A target set staged while the scheduler was down flows in here
                # as --target-* (scx_teddy's init would wipe control files
                # written before launch). Both paths or neither.
                tm = st.session_state.get(PENDING_TMODEL)
                tc = st.session_state.get(PENDING_TCONFIG)
                target_model = Path(tm) if tm else None
                target_config = Path(tc) if tc else None
                argv = runner.build_classify_argv(
                    model_p, config_p, duration=int(period),
                    target_model=target_model, target_config=target_config)
                st.session_state["classify_log"] = ["$ " + runner.pretty_argv(argv)]
                try:
                    h = runner.ProcessHandle(
                        argv,
                        on_line=lambda s: (proc_log_to_session("classify_log", s),
                                           kick_rerun_soon()),
                        on_exit=lambda rc: (proc_log_to_session(
                            "classify_log", f"[exited rc={rc}]"),
                            kick_rerun_soon()),
                        label="classify")
                    # Freeze what's actually running for the status line, so it
                    # reflects the launch — not whatever the pickers show now.
                    st.session_state["classify_handle"] = h
                    st.session_state["classify_running_model"] = str(model_p)
                    st.session_state["classify_running_config"] = str(config_p)
                    st.rerun()
                except OSError as e:
                    st.error(f"Launch failed: {e}")

    with c_stop:
        if st.button("■ Stop (SIGINT)", disabled=not running, key="classify_stop",
                     use_container_width=True):
            handle.stop()
            st.toast("Sent SIGINT — scx_teddy is tearing down the scheduler.")

    if running:
        st.success(
            f"Classifying · model `{st.session_state.get('classify_running_model','?')}` "
            f"· config `{st.session_state.get('classify_running_config','?')}`")
    elif handle is not None:
        st.info("Finished · scheduler stopped.")

    st.markdown("##### Log")
    st.code("\n".join(st.session_state.get("classify_log", []))
            or "(idle — pick a model + config, then Start)", language="text")
