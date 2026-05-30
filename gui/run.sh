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
# Streamlit flags:
#   --server.headless true     don't auto-launch a browser tab; user keeps
#                              their already-open one and just hits reload.
#                              (Was opening Chrome on every restart before.)
#   --browser.serverAddress    avoid the "where is the server?" prompt.
#   --browser.gatherUsageStats false   no telemetry.
#   --theme.base dark          force dark theme.
exec "$PY" -m streamlit run app.py \
    --server.headless true \
    --browser.serverAddress localhost \
    --browser.gatherUsageStats false \
    --theme.base dark
