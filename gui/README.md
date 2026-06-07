# scx_teddy Dashboard · Streamlit

> 繁體中文版：[README.md.zh-TW.md](README.md.zh-TW.md)

A thin-shell GUI over scx_teddy and `train.py`: it only ever shells out the same
commands you could type by hand (everything flows through `scx_runner.py`), so
the GUI can die and a running scheduler keeps going. scx_teddy needs root for
BPF, so the run commands are wrapped in `sudo` — the GUI assumes you can sudo.

## Setup

Create the repo-root venv once and install the dependencies (`requirements.txt`
includes `streamlit`, `plotly`, etc.):

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`run.sh` locks onto this `venv/bin/python3` itself, so you don't have to
activate the venv manually before launching.

## Launch

```sh
cd gui
sudo ./run.sh
```

Locks the repo `venv/bin/python3`, dark theme, headless `streamlit run app.py`,
at <http://localhost:8501> by default.

## The five tabs

### 📥 Collect
Runs `sudo -E scx_teddy --mode collect`, writing each task's features to a CSV.
- Output defaults to `/tmp/scx_teddy_gui` (tmpfs); tick *Custom output dir* to
  choose another location. Filenames are always timestamped automatically.
- Streams the log / Stop sends SIGINT for a clean exit (flushes the CSV) / Clear
  `.csv` wipes them in one click.
- Lists saved CSVs below; multi-select to copy them to a chosen directory.
- **Specialization target**: optionally pick a target ppid (see *Target panel*
  below). In collect mode this only *marks* the family — the CSV's `ancestor`
  column converges to that ppid so the whole family is identifiable in analysis.

### 🧠 Train
Runs `train.py` to train KMeans, producing `model_<ts>.json` plus its sibling
`_result.json` (per-cluster membership).
- The Input CSV dropdown defaults to the most recent collect. Leave K blank for
  automatic elbow selection.

### 🗺 Static t-SNE
Runs t-SNE on any CSV, plotted with plotly (zoomable, hover shows tid/comm).
- Both the CSV and the cluster-result JSON can be picked from a /tmp dropdown or
  via *Custom path* for an arbitrary location.
- Three colour modes: single colour / by KMeans cluster / highlight
  `tgid=1234,5678` (the target group is coloured, the rest greyed).

### 🎯 Classify
Runs `sudo -E scx_teddy --mode classify --model M --config C` to classify tasks
live with a trained model and apply the scheduling policy.

- **Model + scheduling config editor** (the shared `tabs/_config_editor.py`):
  pick a model and the table below is auto-sized to its cluster count (extras
  dropped, missing ones filled with defaults). Each row edits **prio** (0 =
  highest, 11 = lowest), **slice_ns** (floored at 100000), **cpu_kind** (0 =
  shared, otherwise 1-based, 1 = fastest tier; labels are generated from the
  local topology as P-core / E-core / tier-N), and **cpu_prefer** (no preference
  / prefer fast / prefer slow).
  - Config-source radio: *Edit in GUI* starts from defaults; *Existing file*
    loads a config off disk as a seed (dropdown, or *Custom path*). Start always
    serializes the table to a fresh /tmp file and points `--config` at it (the
    original is never touched); *Existing file* mode has a guarded *Save back to
    file* button.
  - **Where the dropdowns look:** both the model picker and the *Existing file*
    config dropdown scan the tmpfs work dir **plus** the repo-root `model/` and
    `config/` directories. Drop a curated model into `model/` or a config into
    `config/` (any `*.json`) and it shows up here automatically; the dirs are
    optional (ignored if absent).
- **Target family model + config** (optional, stacked under the default editor):
  gives the specialization target its **own** model + config — it can use a
  different model than the default (the point of two SchedSets).
  - Scheduler **not yet running** → no buttons; whatever the editor shows is
    folded into `--target-model/--target-config` automatically when you press
    Start (writing the control files before launch would be wiped by scx_teddy's
    init, so there's nothing to "save" up front).
  - Scheduler **running** → *Apply target set* / *Clear target set* buttons
    appear: Apply writes `control_model`/`control_config` and scx_teddy
    hot-swaps it on its next poll (no restart); Clear reverts to the default set.
  - ⚠️ This set only takes effect once you **also pick a target ppid**.
- **Specialization target ppid** (Target panel, see below).
- Predict period `-c` defaults to 1s (scx_teddy's built-in 600s is too slow).
- Streams the log / Stop sends SIGINT to tear the scheduler down.

### 📊 Overall
An htop-style live dashboard, whole-machine + per-task, refreshing at 1 Hz via
`@st.fragment(run_every="1s")` (scoped to this tab, so typing in others isn't
interrupted).
- Top metrics: total CPU% / RAM / active task count / core-group makeup.
- Per-CPU bars: one bar per logical CPU, auto-coloured by cpufreq grouping (P/E
  not hardcoded).
- Task table: every task (virtualised scroll), sorted by CPU%, with comm / tgid
  / ppid filters. The whole-machine part is pure /proc — it never touches
  scx_teddy.
- **The classification columns (cluster / prio / cpu_kind / slice) read the
  classify snapshot**: once classify is running, scx_teddy atomically writes
  `/tmp/scx_teddy/snapshot.json` (tid → classification state) each cycle, and
  Overall joins it in by tid. These columns stay blank when classify isn't
  running.

## Target panel (`tabs/_target.py`, shared by Collect and Classify)

Picks which ppid family to specialize. Writes `/tmp/scx_teddy/control_ppid`
(root-owned, so the GUI writes it via `sudo tee`), which scx_teddy re-reads every
`--control-interval` seconds. A radio offers two modes:

- **Manual**: type a ppid, Set / Clear (0).
- **Scanner**: pick a scanner script from a dropdown of `target_finder_helper/`
  (currently one Steam example, `game_task_finder.py`; the dropdown scans the
  directory, so adding a scanner needs no code change). Start runs it as a
  subprocess that keeps writing control_ppid; Stop sends SIGINT (the scanner
  writes 0 on Ctrl-C to clear the target).

The current control_ppid is shown live at the top of the panel. For the full
protocol see `target_finder_helper/README.md`.
