"""Helpers shared across the page modules.

Mostly the session-state plumbing used by Collect/Train when streaming
subprocess output: Streamlit reruns the script on every widget event, so a
running ProcessHandle's log can't live in a Python local — it has to be in
`st.session_state`. These helpers keep that boilerplate in one place.
"""

from __future__ import annotations

import time

import streamlit as st


def proc_log_to_session(key: str, text: str) -> None:
    """Append one log line to the per-tab buffer in session_state. The cap
    avoids unbounded growth during an hours-long collect run."""
    buf = st.session_state.setdefault(key, [])
    buf.append(text)
    if len(buf) > 1000:
        del buf[: len(buf) - 1000]


def kick_rerun_soon() -> None:
    """Streamlit doesn't auto-rerun when a background thread writes to
    session_state. Mutating a session_state key here marks the session dirty
    so the next paint shows the new lines. The auto-refresh loop in app.py
    does the actual rerun on its tick."""
    st.session_state["_log_dirty"] = time.time()
