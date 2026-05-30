#!/usr/bin/env bash
# Launch the Streamlit mockup dashboard.
#
# Run from anywhere; paths are resolved relative to this script. Streamlit
# opens the browser to a local URL (default http://localhost:8501) and
# auto-reloads when app.py changes — convenient while iterating on the
# visuals before any real backend is wired in.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# Same reasoning as gui.sh: streamlit/plotly are installed in the repo's
# venv (NOT in the system python). Lock the interpreter explicitly so the
# script works regardless of which python3 is on PATH.
if [[ -n "${SCX_TEDDY_PYTHON:-}" ]]; then
    PY="$SCX_TEDDY_PYTHON"
elif [[ -x "$ROOT/venv/bin/python3" ]]; then
    PY="$ROOT/venv/bin/python3"
else
    PY="python3"
fi

cd "$HERE"
# --server.headless silences the "would you like to share email" prompt;
# the dashboard opens in the default browser via streamlit's own logic.
exec "$PY" -m streamlit run app.py \
    --server.headless true \
    --browser.gatherUsageStats false \
    --theme.base dark
