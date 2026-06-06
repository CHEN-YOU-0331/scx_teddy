# target_finder_helper — external scanners for scx_teddy

scx_teddy can **specialize** a task family: a chosen process and all its
descendants get treated specially by the scheduler (via the Union-Find ancestor
climb in `main.rs`). Which family to specialize is decided *outside* scx_teddy,
by a **scanner** — any program, in any language, that writes a target ppid into
a control file. scx_teddy does no scanning itself; it just re-reads that file.

This directory holds one example scanner (`game_task_finder.py`); the protocol
below is all you need to write your own.

## The control protocol

scx_teddy watches three single-value files under `/tmp/scx_teddy/`, each
re-read every `--control-interval` seconds (default 5):

```
control_ppid     ← target ppid: a single integer (0 = none)
control_model    ← path to the target family's model JSON  (empty = use default)
control_config   ← path to the target family's config JSON (empty = use default)
```

One value per file so each is trivially `echo`-able from any language. They are
read independently and a change in any one is acted on; this isn't a hot path.

- scx_teddy **creates** all three before loading BPF and **removes** them on
  shutdown. `control_ppid` is seeded to `0`; `control_model`/`control_config`
  are seeded with the startup `--target-model`/`--target-config` paths (empty if
  not given).
- **`control_ppid`** — a well-formed non-negative integer becomes the new
  target; `0` clears it. Anything else (empty, garbage, a torn write, negative)
  is ignored, keeping the current target. So a scanner that crashes mid-write
  does no harm. scx_teddy also clears the target on its own the moment the
  target ppid dies (it sees the exit event), without waiting for the next poll.
- **`control_model` + `control_config`** — when both are non-empty the target
  family uses that model + config instead of the defaults; if either is empty
  the target family falls back to the default set. A reload that fails to load
  or validate keeps the currently-loaded set (the running scheduler is never
  disturbed by a bad path).

### Who writes what

A **scanner** writes only `control_ppid` — its one job is to point at a family.
Choosing the target family's model/config (`control_model`, `control_config`) is
left to the GUI or a manual `echo`; the example scanner here does not touch
them. The simplest scanner is one line:

```sh
echo 1234 > /tmp/scx_teddy/control_ppid
```

> **Needs root.** scx_teddy runs under `sudo` (it needs root for BPF), so it
> creates the control files owned by root — a normal-user process can't write
> them. Run your scanner with `sudo` too (e.g. `sudo ./game_task_finder.py`,
> `echo 1234 | sudo tee /tmp/scx_teddy/control_ppid`).

## Tools

### `game_task_finder.py` — Steam game scanner

Detects a running Steam game and publishes its family ppid. Steam injects
`SteamGameId=<appid>` (older: `STEAM_GAME=`) into every process it launches for
a game, so reading `/proc/<pid>/environ` finds the whole family.

Picking the ppid is the subtle part. Under Proton the tree looks like:

```
pv-adverb ─┬─ armoredcore6.exe ...
           ├─ explorer.exe ...
           ├─ services.exe ...
           ├─ python3 ─── steam.exe ...   ← also carries SteamGameId
           └─ ...
```

A naive "first hit wins" could pick the tiny `python3 → steam.exe` branch.
Instead the scanner buckets every hit by its parent pid and picks the parent
with the **most** members — the real game family is by far the largest group
under one parent, so the count breaks the tie correctly. (Known Steam/Proton
plumbing — `reaper`, `srt-bwrap`, `pv-adverb`, `steam.exe` — is excluded so it
doesn't skew the counts.)

```sh
./game_task_finder.py        # scan every 5s
./game_task_finder.py 2      # scan every 2s
```

Writes the game's ppid to `control_ppid` when one is running, `0` when none is,
and `0` again on Ctrl-C so a stale target doesn't linger.

## Writing your own

Any of these is a valid scanner — the detection is up to you, the output is
always "a ppid in `control_ppid`":

- match on `comm` / `cmdline`
- a process in a particular cgroup
- the focused window's pid (from your compositor)
- a pid you picked by hand

```python
import os
def publish(ppid):
    tmp = "/tmp/scx_teddy/control_ppid.tmp"
    with open(tmp, "w") as f:
        f.write(str(ppid))
    os.replace(tmp, "/tmp/scx_teddy/control_ppid")  # atomic
```
