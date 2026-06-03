"""scx_teddy dashboard — Streamlit entry point.

Three top-level tabs, all backed by real data:

  - **Collect** — runs `sudo -E scx_teddy --mode collect`, streams the log.
  - **Train** — runs `train.py` on a collected CSV.
  - **Static t-SNE** — loads a CSV and plots its t-SNE (plotly), optionally
    coloured by a trained model's *_result.json, or highlighting a target
    tgid/ppid/tid.

The tab-rendering bodies live in `tabs/` (one file each). The directory is
deliberately NOT called `pages/` because Streamlit treats a `pages/` sibling
to the entry script as a multipage-app auto-nav — it would show every file
there as a sidebar link, duplicating our top tabs. Renaming sidesteps that.

`scx_runner.py` sits next to this file — it wraps the subprocess work
(spawning scx_teddy / train.py), timestamped filenames, and the
list/copy/clear helpers for the saved CSVs and models.

Note for developers: a live-dashboard mockup (Overall / Per-task / Cluster
fed by fake data) lives in `_mock_stash/`. It is gitignored on purpose —
fake data shouldn't ship with the repo. To re-enable the mock tabs while
iterating on visuals locally, flip the `MOCK_TABS_ENABLED` flag below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Local imports: theme/scx_runner are siblings of this file, and tabs/ is a
# subpackage. Adding the script's own directory makes both work without
# tripping over the package layout.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# _mock_stash also goes on the path so the optional mock tabs can import.
sys.path.insert(0, str(Path(__file__).resolve().parent / "_mock_stash"))

# Tab modules. Imported via package path so the relative `from ._common`
# imports inside them resolve.
from tabs import collect as page_collect  # noqa: E402
from tabs import train as page_train  # noqa: E402
from tabs import static_tsne as page_static_tsne  # noqa: E402
from tabs import classify as page_classify  # noqa: E402
from tabs import overall as page_overall  # noqa: E402


# -----------------------------------------------------------------------------
# Page chrome
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="scx_teddy dashboard",
    page_icon="🐻",
    layout="wide",
    initial_sidebar_state="expanded",
)

# CSS polish. Two things matter here:
#   1. Tighter padding / smaller headers — Streamlit defaults are blog-y, not
#      dashboard-y; we want more room for charts.
#   2. **Visible, distinguishable tabs.** The stock dark theme draws tabs as
#      faint text with a 1-px bottom line under the active one — easy to miss
#      which one you're on, and the text contrast is poor. We give every tab
#      a boxed look with a clear border, brighter text, and make the active
#      tab pop with a coloured border + accent underline.
st.markdown(
    """
    <style>
    /* Hide Streamlit's floating top header (hamburger menu, share button,
       deploy button). It overlaps the tab strip when we shrink top padding,
       and a demo dashboard doesn't need those buttons anyway. */
    [data-testid="stHeader"] { display: none; }
    [data-testid="stToolbar"] { display: none; }

    /* Now that the header is gone we can pull the content up without it
       colliding. Keep a small top gap so the first tab isn't flush against
       the window edge. */
    .block-container { padding-top: 1rem; padding-bottom: 1rem; }
    h1 { font-size: 1.6rem; margin-bottom: 0.4rem; }
    h2 { font-size: 1.2rem; margin-top: 0.6rem; }
    [data-testid="stMetricValue"] { font-size: 1.4rem; }

    /* The tab strip itself: small gap between tabs + a bottom line below the
       whole strip so the content area visually "hangs" from it. */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 6px;
        border-bottom: 1px solid #333;
        padding-bottom: 4px;
        margin-bottom: 12px;
    }

    /* Each individual tab — boxed, bright text. */
    [data-testid="stTabs"] button[data-baseweb="tab"] {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid #3a3a3a;
        border-radius: 8px 8px 0 0;
        padding: 8px 18px;
        font-size: 1.05rem;
        font-weight: 500;
        color: #ccc;
        transition: background 0.12s, color 0.12s, border-color 0.12s;
    }
    [data-testid="stTabs"] button[data-baseweb="tab"]:hover {
        background: rgba(255, 255, 255, 0.08);
        color: #fff;
    }

    /* Active tab — bright coral accent (matches PCORE_COLOR from theme.py)
       so the choice is unmistakeable at a glance. */
    [data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] {
        background: rgba(255, 107, 107, 0.12);
        border-color: #ff6b6b;
        border-bottom: 2px solid #ff6b6b;
        color: #fff;
        font-weight: 600;
    }

    /* Kill Streamlit's own pink underline highlight under the active tab. */
    [data-testid="stTabs"] [data-baseweb="tab-highlight"] { display: none; }
    [data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Sidebar
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🐻 scx_teddy")
    st.caption("Collect data · train models · plot t-SNE.\n\n"
               "All three tabs run real scx_teddy / train.py "
               "(no fake data).")
    st.divider()
    st.caption("Demo machine · i5-13500\n6P (0–11) · 8E (12–19) · 32 GB")


# -----------------------------------------------------------------------------
# Tabs
# -----------------------------------------------------------------------------
# Optional: enable the mock live-dashboard tabs (Overall / Per-task / Cluster)
# while iterating on visuals locally. Requires _mock_stash/ to be present —
# it's gitignored, so a fresh clone won't have it.
MOCK_TABS_ENABLED = False

if MOCK_TABS_ENABLED:
    # Late, dynamic imports so a missing _mock_stash doesn't break the main
    # app and so static analysers don't warn about a path-injected module.
    # Note: Overall is the *real* tab below — the mock Overall is retired
    # now that the real one works. Only Per-task / Cluster still need mocks.
    import time
    import importlib
    mock_data = importlib.import_module("mock_data")
    per_task_page = importlib.import_module("_mock_stash.per_task_page")
    cluster_page = importlib.import_module("_mock_stash.cluster_page")

    if "mock" not in st.session_state:
        st.session_state.mock = mock_data.Mock(n_tasks=120)
        st.session_state.selected_tid = None
        st.session_state.last_tick = 0.0
    mock = st.session_state.mock
    now = time.time()
    if now - st.session_state.last_tick >= 1.0:
        mock.tick()
        st.session_state.last_tick = now

    (tab_collect, tab_train, tab_static, tab_classify,
     tab_overall, tab_per, tab_cluster) = st.tabs([
        "📥 Collect", "🧠 Train", "🗺 Static t-SNE", "🎯 Classify",
        "📊 Overall", "🔬 Per-task (mock)", "✨ Cluster (mock)",
    ])
    with tab_per:
        per_task_page.render(mock)
    with tab_cluster:
        cluster_page.render(mock)
else:
    (tab_collect, tab_train, tab_static,
     tab_classify, tab_overall) = st.tabs([
        "📥 Collect", "🧠 Train", "🗺 Static t-SNE", "🎯 Classify", "📊 Overall",
    ])

with tab_collect:
    page_collect.render()
with tab_train:
    page_train.render()
with tab_static:
    page_static_tsne.render()
with tab_classify:
    page_classify.render()
with tab_overall:
    page_overall.render()


# -----------------------------------------------------------------------------
# Background log-stream rerun nudger
# -----------------------------------------------------------------------------
# When Collect / Train have a subprocess running, its on_line callback writes
# log lines into session_state from a reader thread; Streamlit doesn't rerun
# automatically on that. We rerun every ~1.5 s while something is in flight
# so the log scrolls without the user clicking. Idle = no rerun, no spin.
import time as _time  # noqa: E402

def _any_proc_running() -> bool:
    for key in ("collect_handle", "train_handle", "classify_handle"):
        h = st.session_state.get(key)
        if h is not None and h.is_running():
            return True
    return False


# Overall no longer drives a global rerun: it self-refreshes via an
# @st.fragment(run_every="1s") in tabs/overall.py, scoped to that tab only, so
# typing in other tabs is never interrupted. The global rerun below remains
# only for the Collect/Train/Classify log streams (a background reader thread
# writes lines into session_state and Streamlit won't rerun on its own).
if _any_proc_running() or MOCK_TABS_ENABLED:
    _time.sleep(1.0 if MOCK_TABS_ENABLED else 1.5)
    st.rerun()
