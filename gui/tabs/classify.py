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

import pandas as pd
import streamlit as st

import scx_runner as runner
import sys_topology

from ._common import proc_log_to_session, kick_rerun_soon


def _model_choices() -> list[str]:
    """Trained models reachable this session: the /tmp data dir plus the most
    recent one (which may live on an SSD custom dir, outside /tmp)."""
    models = [str(p) for p in runner.list_models()]
    if runner.last_model is not None and str(runner.last_model) not in models:
        models.insert(0, str(runner.last_model))
    return models


def _cpu_kind_label(k: int, groups: list) -> str:
    """A cpu_kind value rendered as `"<int> · <meaning>"`. 0 = any CPU; 1-based
    into the fast→slow groups, so kind k names groups[k-1] (P-core / E-core /
    tier-N). The int is the leading token, which makes the label trivially
    reversible (see _cpu_kind_int) without threading the group list everywhere."""
    if k == 0:
        return "0 · any"
    if 1 <= k <= len(groups):
        return f"{k} · {groups[k - 1].name}"
    return f"{k} · kind {k}"


def _cpu_kind_int(label: str) -> int:
    """Inverse of _cpu_kind_label: the int is the leading token before ` · `."""
    try:
        return int(str(label).split("·", 1)[0].strip())
    except (ValueError, IndexError):
        return runner.CONFIG_DEFAULT_CPU_KIND


def _cpu_kind_options(groups: list) -> list[str]:
    """All valid cpu_kind labels for this machine: 0 (any) plus one per kind."""
    return [_cpu_kind_label(k, groups) for k in range(len(groups) + 1)]


def _row_from_entry(cluster: str, entry: dict | None, groups: list) -> dict:
    """One editor row. `entry` is a cluster's config dict (from a loaded file)
    or None to fall back to the GUI defaults. Only the fields the editor shows
    are pulled out; slice_mode/extra keys are dropped (the editor is fixed-slice
    only, so re-serializing always writes a clean fixed entry).

    cpu_kind and cpu_prefer are held as labelled strings (so the table shows
    dropdowns) and mapped back to ints on save."""
    entry = entry or {}
    prefer_int = entry.get("cpu_prefer", runner.CONFIG_DEFAULT_CPU_PREFER)
    kind_int = entry.get("cpu_kind", runner.CONFIG_DEFAULT_CPU_KIND)
    return {
        "cluster": cluster,
        "prio": entry.get("prio", runner.CONFIG_DEFAULT_PRIO),
        "slice_ns": entry.get("slice_ns", runner.CONFIG_DEFAULT_SLICE_NS),
        "cpu_kind": _cpu_kind_label(int(kind_int), groups),
        "cpu_prefer": runner.CPU_PREFER_LABELS.get(
            int(prefer_int), runner.CPU_PREFER_LABELS[0]),
    }


def _seed_editor_df(n_clusters: int, groups: list,
                    loaded: tuple[dict, dict] | None = None) -> pd.DataFrame:
    """One row per cluster id (0..n-1) plus a final `default` row. The column
    set is the row label + the editable knobs. `groups` is the discovered CPU
    topology, used to label the cpu_kind dropdown.

    `loaded` = (clusters, default) parsed from an existing config file. The
    table is always sized to `n_clusters` (the model's cluster count): a loaded
    cluster id beyond that is dropped, a missing one is filled with defaults.
    `loaded=None` seeds every row with the GUI defaults (the "Edit in GUI"
    fresh-start case)."""
    loaded_clusters, loaded_default = (loaded if loaded is not None else ({}, {}))
    rows = [_row_from_entry(str(i), loaded_clusters.get(str(i)), groups)
            for i in range(n_clusters)]
    rows.append(_row_from_entry("default", loaded_default, groups))
    return pd.DataFrame(rows)


def _df_to_config(df: pd.DataFrame) -> tuple[dict, dict]:
    """Split the editor table back into ({clusters}, default) scx_teddy reads.
    The `default` row becomes the top-level default; every other row is a
    numbered cluster entry."""
    clusters: dict[str, dict] = {}
    default = runner.make_cluster_entry()
    for _, r in df.iterrows():
        # cpu_kind and cpu_prefer are held as labels in the table; map back.
        prefer = runner.CPU_PREFER_BY_LABEL.get(
            str(r["cpu_prefer"]), runner.CONFIG_DEFAULT_CPU_PREFER)
        entry = runner.make_cluster_entry(
            prio=r["prio"], slice_ns=r["slice_ns"],
            cpu_kind=_cpu_kind_int(r["cpu_kind"]),
            cpu_prefer=prefer)
        if str(r["cluster"]).strip().lower() == "default":
            default = entry
        else:
            clusters[str(r["cluster"]).strip()] = entry
    return clusters, default


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
        # Each widget owns its value via its own key — we read straight from
        # the widget return, never write back into its key. Writing
        # `classify_model` ourselves on every rerun (then feeding it back as
        # `value=`) is what made the table flicker: an empty edit was clobbered
        # by the stale stored value on the next rerun.
        if model_custom:
            # Seed the text box from the last selection only the first time.
            st.session_state.setdefault("classify_model_text", default_model)
            model_str = st.text_input(
                "Model JSON path", key="classify_model_text",
                label_visibility="collapsed")
        else:
            model_str = st.selectbox(
                "Model JSON",
                options=models if models else [""],
                index=models.index(default_model) if default_model in models else 0,
                format_func=lambda s: s or "(no models found — tick custom)",
                label_visibility="collapsed")

    # --- Config: always an editable table; the source seeds it --------------
    # Two seed sources: "Edit in GUI" starts from defaults, "Existing file"
    # loads a config off disk. Either way the table is the source of truth and
    # is re-serialized to a fresh /tmp config on Start (--config points at the
    # /tmp copy, never the original). The table is always sized to the model's
    # n_clusters, so a loaded file with more/fewer clusters is reshaped: extra
    # cluster ids are dropped, missing ones filled with defaults.
    st.markdown("##### Scheduling config")
    config_mode = st.radio(
        "Config source", ["Edit in GUI", "Existing file"], index=0,
        horizontal=True, label_visibility="collapsed", key="classify_config_mode")

    edited_df = None  # set once a table renders; consumed at Start time

    # "Existing file" adds a path box whose file seeds the table.
    src_path = ""
    if config_mode == "Existing file":
        st.session_state.setdefault(
            "classify_config_path", str(runner.REPO_ROOT / "config.json"))
        src_path = st.text_input("Scheduling config JSON path to load & edit",
                                 key="classify_config_path")

    # The table needs the model's cluster count. Without a readable model we
    # can't size it, so it vanishes (Start/Stop below still render so a live
    # run stays controllable).
    n = runner.model_n_clusters(Path(model_str)) if model_str else None
    if n is None:
        st.info("Pick a readable model JSON above — the config table is "
                "sized from its cluster count.")
    else:
        loaded = None
        if config_mode == "Existing file":
            if src_path and Path(src_path).exists():
                loaded = runner.read_config(Path(src_path))
                if loaded is None:
                    st.warning(f"Couldn't parse `{src_path}` — seeding with "
                               "defaults instead.")
            elif src_path:
                st.warning(f"`{src_path}` not found — seeding with defaults.")

        cap = (f"Model reports **{n}** clusters → {n} cluster rows + a default "
               "row.")
        if loaded is not None:
            cap += " Loaded values shown; missing clusters use defaults."
        st.caption(cap)

        # CPU topology drives the cpu_kind dropdown labels (0 = any, then one
        # entry per kind named P-core / E-core / tier-N). Independent of
        # scx_teddy — same self-contained topology the Overall tab uses.
        groups = sys_topology.discover()

        # Reseed only when the source signature changes (mode + file + n);
        # otherwise keep the user's in-table edits across reruns.
        sig = (config_mode, src_path, n)
        if st.session_state.get("classify_cfg_sig") != sig:
            st.session_state["classify_cfg_df"] = _seed_editor_df(n, groups, loaded)
            st.session_state["classify_cfg_sig"] = sig

        edited_df = st.data_editor(
            st.session_state["classify_cfg_df"],
            key="classify_cfg_editor", hide_index=True,
            width="stretch", disabled=["cluster"],
            column_config={
                "cluster": st.column_config.TextColumn(
                    "cluster", help="Cluster id from the model, or `default`."),
                "prio": st.column_config.NumberColumn(
                    "prio", help="0 = highest, 11 = lowest.", min_value=0,
                    max_value=11, step=1),
                "slice_ns": st.column_config.NumberColumn(
                    "slice_ns", help="Fixed time slice in ns (floored at "
                    "100000 — the real minimum the scheduler grants).",
                    min_value=runner.CONFIG_DEFAULT_SLICE_NS, step=1000),
                "cpu_kind": st.column_config.SelectboxColumn(
                    "cpu_kind", help="Which CPU kind to pin to. '0 · any' = "
                    "shared DSQ (any CPU); otherwise 1-based, 1 = fastest tier.",
                    options=_cpu_kind_options(groups), required=True),
                "cpu_prefer": st.column_config.SelectboxColumn(
                    "cpu_prefer", help="select_cpu speed preference. "
                    "'no preference' lets the scheduler auto-derive it from "
                    "cpu_kind; 'prefer fast' / 'prefer slow' force it.",
                    options=list(runner.CPU_PREFER_LABELS.values()),
                    required=True),
            })
        st.session_state["classify_cfg_df"] = edited_df

        # Save edits back to the loaded file — only in "Existing file" mode and
        # only on an explicit click guarded by a confirm box. Start always uses
        # a fresh /tmp copy, so writing the original is never automatic: the
        # user has to opt in here, which is the whole point (don't clobber a
        # config by accident). Note this writes whatever cluster set the table
        # currently has, so a reshaped (more/fewer clusters) config can be
        # persisted back too.
        if config_mode == "Existing file" and src_path:
            confirm = st.checkbox(
                f"Confirm overwrite of `{src_path}`", key="classify_save_confirm")
            if st.button("💾 Save back to file", disabled=not confirm,
                         key="classify_save"):
                try:
                    clusters, default = _df_to_config(edited_df)
                    runner.save_config_to(Path(src_path), clusters, default)
                    st.toast(f"Saved → {src_path}")
                except OSError as e:
                    st.error(f"Save failed: {e}")

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
            model_p = Path(model_str)
            # Both modes serialize the edited table to a fresh /tmp config and
            # point --config there — the loaded file (if any) is only a seed, so
            # the original on disk is never touched. No table (no readable
            # model) → no config to write → error out below.
            config_p = None
            if edited_df is not None:
                clusters, default = _df_to_config(edited_df)
                config_p = runner.write_config(clusters, default)

            if not runner.SCX_TEDDY_BIN.exists():
                st.error(f"scx_teddy binary missing at {runner.SCX_TEDDY_BIN}.")
            elif not model_str or not model_p.exists():
                st.error(f"Model not found: {model_str}")
            elif config_p is None:
                st.error("No config table — pick a readable model first.")
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
