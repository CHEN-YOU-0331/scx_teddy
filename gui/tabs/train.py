"""Train tab — runs `train.py` on a collected CSV, producing a model JSON
and its sibling `_result.json`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import scx_runner as runner

from ._common import proc_log_to_session, kick_rerun_soon


def render():
    st.title("Train · run train.py on a collected CSV")

    csvs = runner.list_csvs()
    csv_names = [str(p) for p in csvs]
    # Default to last_csv when set (most recent output, possibly SSD).
    default_csv = (str(runner.last_csv) if runner.last_csv is not None
                   else (csv_names[0] if csv_names else ""))
    csv_path = st.selectbox(
        "Input CSV",
        options=csv_names if csv_names else [""],
        index=csv_names.index(default_csv) if default_csv in csv_names else 0,
        format_func=lambda s: s or "(no CSVs found)")

    c1, c2 = st.columns([1, 3])
    with c1:
        custom = st.checkbox("Custom model out dir", value=False,
                             key="train_custom_out")
    with c2:
        if custom:
            out_dir_str = st.text_input(
                "Model output directory",
                value=st.session_state.get("train_out_dir",
                                          str(runner.DEFAULT_DATA_DIR)),
                label_visibility="collapsed")
            st.session_state["train_out_dir"] = out_dir_str
            out_dir = Path(out_dir_str)
        else:
            out_dir = None
            st.caption(f"→ model goes to **{runner.DEFAULT_DATA_DIR}** "
                       "(auto `model_<timestamp>.json`).")

    k_text = st.text_input("K (blank = elbow auto)", value="")

    handle = st.session_state.get("train_handle")
    running = handle is not None and handle.is_running()

    c_run, c_clear = st.columns(2)
    with c_run:
        if st.button("▶ Train", disabled=running,
                     use_container_width=True, type="primary"):
            if not csv_path or not Path(csv_path).exists():
                st.error(f"Input CSV not found: {csv_path}")
            else:
                k: int | None | str
                try:
                    k = int(k_text) if k_text.strip() else None
                except ValueError:
                    st.error("K must be an integer or blank.")
                    k = "_BAD_"
                if k != "_BAD_":
                    model_out = runner.timestamped_path(
                        "model", "json", directory=out_dir)
                    argv = runner.build_train_argv(Path(csv_path),
                                                   model_out, k=k)  # type: ignore[arg-type]
                    st.session_state["train_log"] = ["$ " + runner.pretty_argv(argv)]
                    st.session_state["train_model"] = str(model_out)
                    try:
                        h = runner.ProcessHandle(
                            argv,
                            on_line=lambda s: (proc_log_to_session("train_log", s),
                                               kick_rerun_soon()),
                            on_exit=lambda rc: (proc_log_to_session(
                                "train_log", f"[exited rc={rc}]"),
                                kick_rerun_soon()),
                            label="train")
                        runner.note_model(model_out)
                        st.session_state["train_handle"] = h
                        st.rerun()
                    except OSError as e:
                        st.error(f"Launch failed: {e}")

    with c_clear:
        if st.button("🧹 Clear .json in /tmp", use_container_width=True):
            n = runner.clear_data("*.json")
            st.toast(f"Deleted {n} .json from {runner.DEFAULT_DATA_DIR}.")

    if running:
        st.success(f"Training · `{st.session_state.get('train_model','?')}`")
    elif handle is not None:
        st.info(f"Finished · last model: `{st.session_state.get('train_model','?')}`")

    st.markdown("##### Log")
    st.code("\n".join(st.session_state.get("train_log", []))
            or "(idle — press Train)", language="text")

    # Saved models
    st.markdown("##### Saved models in /tmp")
    models = runner.list_models()
    if not models:
        st.caption("No models yet.")
    else:
        st.dataframe(
            pd.DataFrame([{"file": m.name,
                           "size (KB)": round(m.stat().st_size / 1024, 1)}
                          for m in models]),
            hide_index=True, use_container_width=True, height=180)
