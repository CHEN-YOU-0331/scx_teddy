"""Subprocess wrappers around scx_teddy and train.py.

Design constraints (from demo.md):
  - No IPC layer. Control flows to scx_teddy purely by writing lines to its
    stdin; data flows back by reading files under /tmp. The GUI can die and
    scx_teddy keeps running; scx_teddy runs fine with no GUI at all.
  - Everything here just shells out — the GUI stays a thin shell over the
    same commands a user could type by hand.

scx_teddy needs root for BPF, so the collect command is wrapped in `sudo`.
The caller is responsible for the machine being able to sudo (cached creds
or a terminal askpass); we surface failures rather than prompt.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

# Repo root = parent of this gui/ directory.
REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIN_PY = REPO_ROOT / "train.py"

# Built binary location (cargo workspace target). Allow override via env so a
# release build or a custom path can be pointed at without code changes.
DEFAULT_BIN = REPO_ROOT / "target" / "release" / "scx_teddy"
SCX_TEDDY_BIN = Path(os.environ.get("SCX_TEDDY_BIN", DEFAULT_BIN))

# Default scratch location for collected data, per demo.md ("/tmp"). /tmp is
# tmpfs (RAM) here, so this is disk-free. gui.sh creates and exports this; the
# fallback keeps a bare `python3 gui/app.py` working too.
DEFAULT_DATA_DIR = Path(os.environ.get("SCX_TEDDY_DATA_DIR", "/tmp/scx_teddy_gui"))


class ProcessHandle:
    """A running child process whose stdout/stderr are pumped to a callback.

    `on_line(text)` is invoked from a reader thread for every output line
    (stdout and stderr merged). `on_exit(returncode)` fires once when the
    process ends. Both callbacks run off the Tk main thread, so the GUI must
    marshal back via `widget.after(...)`.
    """

    def __init__(self, argv, on_line, on_exit, label="proc"):
        self.label = label
        self._on_exit = on_exit
        # Line-buffered text pipes; merge stderr into stdout so ordering is
        # preserved in the log view. stdin kept open as a pipe so control
        # lines can be written later (the stdin command protocol on the
        # scx_teddy side is still TODO — see demo.md).
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._pump, args=(on_line,), daemon=True
        )
        self._reader.start()

    def _pump(self, on_line):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            on_line(line.rstrip("\n"))
        rc = self.proc.wait()
        self._on_exit(rc)

    def send_line(self, text: str) -> bool:
        """Write one control line to the child's stdin.

        Returns False if the pipe is already closed/dead. This is the GUI's
        only control channel into scx_teddy; the line format is whatever the
        scx_teddy stdin protocol ends up being (currently it treats a line as
        a game name).
        """
        if self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError):
            return False

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def stop(self):
        """Ask the process to shut down (SIGINT, mirroring a terminal Ctrl-C).

        scx_teddy's Ctrl-C handler flushes the CSV and tears down the
        scheduler, so SIGINT (not SIGKILL) is the clean stop.
        """
        if self.is_running():
            self.proc.send_signal(signal.SIGINT)


def build_collect_argv(
    output: Path,
    duration: int | None = None,
    max_runtime: int | None = None,
    checkpoint: bool = False,
    use_sudo: bool = True,
) -> list[str]:
    """Build the argv for a `scx_teddy --mode collect` run.

    scx_teddy refuses to overwrite an existing --output file, so the caller
    must pick a fresh path (or delete the old one) before starting.
    """
    argv: list[str] = []
    if use_sudo:
        # -E preserves env (e.g. SCX_TEDDY_BIN) for the child; harmless if unset.
        argv += ["sudo", "-E"]
    argv += [str(SCX_TEDDY_BIN), "--mode", "collect", "--output", str(output)]
    if duration is not None:
        argv += ["--collect-duration", str(duration)]
    if max_runtime is not None and max_runtime > 0:
        argv += ["--max-runtime", str(max_runtime)]
    if checkpoint:
        argv += ["--csv-checkpoint"]
    return argv


def build_classify_argv(
    model: Path,
    config: Path,
    duration: int | None = None,
    use_sudo: bool = True,
) -> list[str]:
    """Build the argv for a `scx_teddy --mode classify` run.

    classify loads a trained model + scheduling config and runs until SIGINT
    (no max-runtime — that deadline is collect-only). Like collect it needs
    root for BPF, so it is wrapped in `sudo -E`.

    `duration` is `-c/--collect-duration`: in classify mode it is the predict
    *period* — the main loop re-classifies every `duration` seconds. scx_teddy's
    built-in default (600s) is far too slow to feel interactive, so the GUI
    passes an explicit value (defaulting to 1s).
    """
    argv: list[str] = []
    if use_sudo:
        argv += ["sudo", "-E"]
    argv += [str(SCX_TEDDY_BIN), "--mode", "classify",
             "--model", str(model), "--config", str(config)]
    if duration is not None:
        argv += ["--collect-duration", str(duration)]
    return argv


def build_train_argv(
    csv: Path,
    model_out: Path,
    k: int | None = None,
    train_config: Path | None = None,
) -> list[str]:
    """Build the argv for a `train.py` run. Produces model_out and, alongside
    it, <model_out stem>_result.json with per-task cluster membership."""
    argv = [sys.executable, str(TRAIN_PY), str(csv), "-o", str(model_out)]
    if k is not None:
        argv += ["-k", str(k)]
    if train_config is not None:
        argv += ["--train-config", str(train_config)]
    return argv


def model_n_clusters(model: Path) -> int | None:
    """Read `n_clusters` out of a trained model JSON, or None if unreadable.

    The Classify config editor uses this to show exactly one row per cluster
    (cluster ids 0..n-1) plus the `default` row. Best-effort: a malformed or
    missing file just yields None and the caller falls back to a manual count.
    """
    try:
        import json
        with open(model) as f:
            n = json.load(f).get("n_clusters")
        return int(n) if n is not None else None
    except (OSError, ValueError, TypeError):
        return None


# Defaults for a freshly-built cluster scheduling entry (see ClusterSchedConfig
# in main.rs). prio 11 = lowest priority, a 100us fixed slice, cpu_kind 0 =
# shared DSQ (runnable on any CPU kind). The editor seeds every row with these.
CONFIG_DEFAULT_PRIO = 11
CONFIG_DEFAULT_SLICE_NS = 100_000
CONFIG_DEFAULT_CPU_KIND = 0


def make_cluster_entry(prio: int = CONFIG_DEFAULT_PRIO,
                       slice_ns: int = CONFIG_DEFAULT_SLICE_NS,
                       cpu_kind: int = CONFIG_DEFAULT_CPU_KIND) -> dict:
    """One cluster's scheduling entry in the fixed-slice shape scx_teddy reads:
    {"prio", "slice_mode":"fixed", "slice_ns", "cpu_kind"}.

    slice_ns is floored at CONFIG_DEFAULT_SLICE_NS (100us): anything smaller is
    not what the scheduler actually grants in practice, so a smaller value would
    silently mislead. We clamp at the single point every saved config flows
    through rather than only in the editor widget."""
    return {"prio": int(prio), "slice_mode": "fixed",
            "slice_ns": max(int(slice_ns), CONFIG_DEFAULT_SLICE_NS),
            "cpu_kind": int(cpu_kind)}


def read_config(path: Path) -> tuple[dict, dict] | None:
    """Parse an existing scheduling config JSON into ({clusters}, default).

    Best-effort: returns None if the file is missing or malformed. Used by the
    Classify editor's "Existing file" mode to seed the table from a config on
    disk, which the user can then re-shape (clusters added/dropped) before it
    is re-serialized to /tmp.
    """
    try:
        import json
        with open(path) as f:
            obj = json.load(f)
        clusters = obj.get("clusters", {})
        default = obj.get("default", {})
        if not isinstance(clusters, dict) or not isinstance(default, dict):
            return None
        return clusters, default
    except (OSError, ValueError):
        return None


def _config_json(clusters: dict[str, dict], default: dict) -> str:
    import json
    return json.dumps({"clusters": clusters, "default": default}, indent=2)


def write_config(clusters: dict[str, dict], default: dict,
                 directory: Path | None = None) -> Path:
    """Serialize a {clusters, default} scheduling config to a fresh timestamped
    JSON under the data dir (tmpfs by default) and return its path.

    Writing to /tmp keeps the GUI-built config out of the repo and means the
    "edit in the GUI" path is just "write a file, then `--config` it" — no new
    channel, same thin-shell model as everything else here.
    """
    out = timestamped_path("config", "json", directory=directory)
    out.write_text(_config_json(clusters, default))
    return out


def save_config_to(path: Path, clusters: dict[str, dict], default: dict) -> Path:
    """Write a {clusters, default} config to an explicit path, overwriting it.

    This is the deliberate "save back to the original file" action (the Classify
    editor's Save button) — distinct from `write_config`, which always picks a
    fresh /tmp path. Kept separate so an overwrite only ever happens on an
    explicit user click, never as a side effect of starting a run.
    """
    path.write_text(_config_json(clusters, default))
    return path


def ensure_data_dir() -> Path:
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_DATA_DIR


def timestamped_path(prefix: str, ext: str, directory: Path | None = None) -> Path:
    """A fresh path named by wall-clock time so it never collides with a
    previous run. Used for both collected CSVs (scx_teddy refuses to overwrite
    an existing --output) and trained models (so a new train run doesn't
    silently clobber the last model). `ext` without dot.

    `directory` defaults to the tmpfs data dir; pass a user-chosen directory
    to write straight to persistent storage (SSD) instead of /tmp — the
    filename is still auto-generated, only the location changes.
    """
    if directory is None:
        directory = ensure_data_dir()
    else:
        directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return directory / f"{prefix}_{stamp}.{ext}"


# Path of the most recently produced output, regardless of where it landed
# (/tmp or a custom SSD dir). The tabs default their dropdown to this so a
# just-collected CSV / just-trained model is immediately the selection for the
# next step — the intuitive "I made a CSV, now I train/visualize it" flow.
# None until something is produced this session (then the dropdown has no
# default, which is fine: nothing exists to act on yet).
last_csv: Path | None = None
last_model: Path | None = None


def note_csv(path: Path) -> None:
    global last_csv
    last_csv = path


def note_model(path: Path) -> None:
    global last_model
    last_model = path


def list_files(pattern: str, directory: Path | None = None) -> list[Path]:
    """List files matching `pattern` under the data dir (newest first by
    mtime). This is the tmpfs /tmp menu of items that exist this boot — used
    to populate dropdowns so the user can also reach back to earlier files.
    The *default* selection, however, is driven by last_csv/last_model, which
    may point outside this dir (a custom SSD output)."""
    directory = directory or ensure_data_dir()
    if not directory.is_dir():
        return []
    files = [p for p in directory.glob(pattern) if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def list_models(directory: Path | None = None) -> list[Path]:
    """List model JSONs (model_*.json), excluding their *_result.json
    siblings so a model appears once even though train.py writes a pair."""
    return [
        p for p in list_files("model_*.json", directory)
        if not p.name.endswith("_result.json")
    ]


def copy_to(src: Path, dest_dir: Path, with_result: bool = False) -> list[Path]:
    """Copy a file out of tmpfs onto persistent storage (SSD). Returns the
    list of destination paths actually written.

    Uses copy (not move): the source in /tmp is often root-owned (collect runs
    under sudo) but world-readable, so a copy by the normal-user GUI works,
    whereas deleting the root-owned source would need privileges. /tmp clears
    on reboot anyway, so leaving the original is harmless.

    `with_result=True` (for models): if a sibling `<stem>_result.json` exists,
    copy it too, so the model and its cluster-membership file stay together.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = [Path(shutil.copy2(src, dest_dir / src.name))]
    if with_result:
        sibling = result_path_for(src)
        if sibling.exists():
            written.append(Path(shutil.copy2(sibling, dest_dir / sibling.name)))
    return written


# Back-compat aliases (CSV-specific names used by the Collect tab).
def timestamped_csv(prefix: str = "event", directory: Path | None = None) -> Path:
    return timestamped_path(prefix, "csv", directory)


def list_csvs(directory: Path | None = None) -> list[Path]:
    return list_files("*.csv", directory)


def clear_data(pattern: str) -> int:
    """Delete files matching `pattern` from the tmpfs data dir ONLY (not any
    SSD copy target — those are deliberate user backups). Returns how many
    were removed.

    No sudo needed: the data dir is owned by the normal user, and Unix
    deletion is governed by the directory's write permission, not the files'
    owner (they are root-owned because collect runs under sudo). /tmp is RAM
    anyway, so this just reclaims what would vanish on reboot.
    """
    removed = 0
    for p in DEFAULT_DATA_DIR.glob(pattern):
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def result_path_for(model_out: Path) -> Path:
    """train.py writes <model>_result.json next to the model (it does
    output_path.replace('.json', '_result.json'))."""
    return model_out.with_name(model_out.stem + "_result.json")


def pretty_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
