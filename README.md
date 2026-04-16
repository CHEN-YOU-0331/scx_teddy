# scx_teddy

An eBPF-based experimental scheduler that collects task runtime behavior online, clusters tasks using ML models (K-means), and dynamically adjusts scheduling priorities and time slices per cluster.

## Requirements

- Linux kernel with sched_ext support
- Root privileges (required for eBPF operations)
- Rust toolchain
- libbpf
- Python 3 with `numpy`, `pandas`, `scikit-learn` (for training)

## Building

```bash
cargo build --release
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

## Usage

scx_teddy operates in two modes: **collect** (gather task data) and **classify** (apply a trained model to schedule tasks).

### Step 1: Collect Task Data

Run the scheduler in collect mode to record task behavior into a CSV file:

```bash
sudo ./target/release/scx_teddy -m collect -c 60 -o event.csv
```

**Options:**
- `-m, --mode <MODE>` - Operating mode: `collect` or `classify` (default: `collect`)
- `-c, --collect-duration <SECONDS>` - Data collection interval in seconds (default: 600)
- `-o, --output <PATH>` - Output CSV file path (default: `event.csv`)
- `--min-events <N>` - Minimum event count to include a task (default: 3)
- `-v, --verbose` - Enable verbose output

### Step 2: Train a K-means Model

Use the training script to cluster tasks based on their runtime characteristics:

```bash
python3 train.py event.csv -o model.json
```

By default, all tasks in the CSV are used. To restrict training to specific workloads, pass a `train_config.config` file containing one comm prefix per line:

```bash
python3 train.py event.csv -o model.json --train-config train_config.config
```

A sample `train_config.config` is provided that matches the workloads in `bench_mark.sh`:

```
# stress-ng workloads
stress-ng-cpu
stress-ng-hdd
stress-ng-switc
stress-ng-timer

# custom workloads
slow-timer
random-timer
fixed-mutex
```

Each line is matched by prefix against the task's `comm`. Lines starting with `#` and blank lines are ignored.

This will:
- Automatically select the number of clusters using the elbow method (or specify with `-k`)
- Export the model (centroids + scaler) to a JSON file
- Print per-cluster statistics and task membership (tid, tgid, ppid, command)
- Save classification results to `model_result.json`

**Options:**
- `-o, --output <PATH>` - Output model JSON path (default: `model.json`)
- `-k, --clusters <N>` - Number of clusters (auto-detect if not specified)
- `--train-config <PATH>` - Comm prefix filter list (default: use all tasks)
- `--filter-tid <TID...>` - Filter by tid(s)
- `--filter-tgid <TGID...>` - Filter by tgid(s)
- `--filter-cmd <CMD...>` - Filter by exact command name(s)

### Step 3: Configure Scheduling Policy

Create a `config.json` that maps each cluster to a priority and time slice policy:

```json
{
  "clusters": {
    "0": { "prio": 2, "slice_mode": "adaptive", "slice_sigma": 1.0 },
    "1": { "prio": 3, "slice_mode": "fixed", "slice_ns": 100000 },
    "4": { "prio": 0, "slice_mode": "adaptive", "slice_sigma": 2.0 }
  },
  "default": { "prio": 3, "slice_mode": "fixed", "slice_ns": 100000 }
}
```

- **prio**: Scheduling priority tier (0 = critical, 1 = interactive, 2 = normal, 3 = batch)
- **slice_mode**:
  - `adaptive`: time slice = avg_runtime + sigma * stddev_runtime (computed per task)
  - `fixed`: time slice = fixed value in nanoseconds

### Step 4: Run with Classification

Apply the trained model to dynamically classify tasks and update scheduling parameters:

```bash
sudo ./target/release/scx_teddy -m classify -c 60 --model model.json --config config.json
```

**Additional options for classify mode:**
- `--model <PATH>` - Path to trained model JSON (required)
- `--config <PATH>` - Path to scheduling config JSON (required)

---

[中文版說明文件](README.zh-TW.md)
