"""Classify tab — runs `sudo -E scx_teddy --mode classify --model M --config C`.

The minimal version: pick a trained model JSON and a scheduling config JSON,
press Start, stream the log, Stop with SIGINT. This is the "use the model"
counterpart to Collect — same thin-shell pattern (everything shells out
through `runner`, control is SIGINT, the GUI can die and scx_teddy keeps
running).

Interaction (live bind/unbind via the stdin command protocol) lands in a
later commit; `runner.ProcessHandle.send_line()` is already the channel for
it. For now this tab only starts/stops a classify run.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import scx_runner as runner

from ._common import proc_log_to_session, kick_rerun_soon


def _model_choices() -> list[str]:
    """Trained models reachable this session: the /tmp data dir plus the most
    recent one (which may live on an SSD custom dir, outside /tmp)."""
    models = [str(p) for p in runner.list_models()]
    if runner.last_model is not None and str(runner.last_model) not in models:
        models.insert(0, str(runner.last_model))
    return models


def render():
    st.title("Classify · run scx_teddy with a trained model")

    # --- Model picker (dropdown of known models + free-text override) -------
    models = _model_choices()
    default_model = (str(runner.last_model) if runner.last_model is not None
                     else (models[0] if models else ""))
    c1, c2 = st.columns([1, 3])
    with c1:
        model_custom = st.checkbox("Custom model path", value=not models,
                                   key="classify_model_custom")
    with c2:
        if model_custom:
            model_str = st.text_input(
                "Model JSON path",
                value=st.session_state.get("classify_model", default_model),
                label_visibility="collapsed")
        else:
            model_str = st.selectbox(
                "Model JSON",
                options=models if models else [""],
                index=models.index(default_model) if default_model in models else 0,
                format_func=lambda s: s or "(no models found — tick custom)",
                label_visibility="collapsed")
    st.session_state["classify_model"] = model_str

    # --- Config picker (free text; default to repo config.json) -------------
    default_config = st.session_state.get(
        "classify_config", str(runner.REPO_ROOT / "config.json"))
    config_str = st.text_input("Scheduling config JSON path", value=default_config)
    st.session_state["classify_config"] = config_str

    # --- Predict period (-c) -------------------------------------------------
    # In classify mode -c is how often the scheduler re-classifies every task.
    # scx_teddy's own default (600s) is far too slow to feel interactive, so we
    # default to 1s and let the user dial it up if prediction cost matters.
    period = st.number_input(
        "Predict period (s) · -c", min_value=1, value=1, step=1,
        help="How often scx_teddy re-classifies tasks. scx_teddy's built-in "
             "default is 600s; the GUI uses 1s unless you change it.")

    handle = st.session_state.get("classify_handle")
    running = handle is not None and handle.is_running()

    c_start, c_stop = st.columns(2)
    with c_start:
        if st.button("▶ Start classify", disabled=running, key="classify_start",
                     use_container_width=True, type="primary"):
            model_p, config_p = Path(model_str), Path(config_str)
            if not runner.SCX_TEDDY_BIN.exists():
                st.error(f"scx_teddy binary missing at {runner.SCX_TEDDY_BIN}.")
            elif not model_str or not model_p.exists():
                st.error(f"Model not found: {model_str}")
            elif not config_str or not config_p.exists():
                st.error(f"Config not found: {config_str}")
            else:
                argv = runner.build_classify_argv(
                    model_p, config_p, duration=int(period))
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
                    st.session_state["classify_handle"] = h
                    st.rerun()
                except OSError as e:
                    st.error(f"Launch failed: {e}")

    with c_stop:
        if st.button("■ Stop (SIGINT)", disabled=not running, key="classify_stop",
                     use_container_width=True):
            handle.stop()
            st.toast("Sent SIGINT — scx_teddy is tearing down the scheduler.")

    if running:
        st.success(f"Classifying · model `{st.session_state.get('classify_model','?')}` "
                   f"· config `{st.session_state.get('classify_config','?')}`")
    elif handle is not None:
        st.info("Finished · scheduler stopped.")

    st.markdown("##### Log")
    st.code("\n".join(st.session_state.get("classify_log", []))
            or "(idle — pick a model + config, then Start)", language="text")
