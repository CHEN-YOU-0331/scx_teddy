# scx_teddy Debugging Guide (when the scheduler hangs)

When the scheduler appears stuck — the system is unresponsive, some tasks never
get to run, the terminal freezes — this guide shows how to **dump the
scheduler's internal state**, how to **reproduce the hang reliably**, and one
**known case where a "hang" is actually expected behavior**. When filing an
issue, attach the dump report together with how you triggered it.

---

## 1. Getting a dump (the `teddy_dump_task` output)

`teddy_dump_task` (`scx_teddy/src/bpf/main.bpf.c`) is the sched_ext `dump_task`
callback. It **does not run periodically** — the kernel calls it once per task
only when it decides to produce a sched_ext dump. That typically happens on:
scheduler error / exit, or when the **watchdog detects a task that hasn't been
scheduled for a long time (a stall)**.

It prints two kinds of line:
- a task that ran for a long time:
  `=== <comm> tid: N run for ... ns prio: P, cpu: K, slice: S ===`
- a stalled task:
  `<comm> tid: N stall for ... ns prio: P, cpu: K, slice: S`

(The `cpu:` field actually prints `kind` — the DSQ cpu_kind slot, not a CPU id.)

### How to get it (works on this machine)

```bash
# 1. Enable the sched_ext dump tracepoint
echo 1 | sudo tee /sys/kernel/tracing/events/sched_ext/sched_ext_dump/enable

# 2. Make it hang once (see section 3 for how to trigger reliably).
#    Once the kernel detects a stall it produces a dump automatically.

# 3. Read it
sudo cat /sys/kernel/tracing/trace_pipe
```

`trace_pipe` streams (it blocks waiting for new data; Ctrl+C to stop). Once you
see the dump you can stop it.

### Quieter variant (recommended)

`trace_pipe` blocks and occasionally spews a big burst at once, which is noisy.
Prefer `trace` (a snapshot — reads what's there and stops) and grep down to just
the `teddy_dump_task` lines:

```bash
echo 1 | sudo tee /sys/kernel/tracing/events/sched_ext/sched_ext_dump/enable
echo | sudo tee /sys/kernel/tracing/trace            # clear the buffer, start clean
# ...trigger the stall (see section 3)...
sudo cat /sys/kernel/tracing/trace | grep -E 'tid:|stall for'
```

### ⚠️ Other kernels / distros may have different methods

How you obtain a dump depends on the kernel version. The above is what works on
this machine (Fedora, sched_ext interface under `/sys/kernel/sched_ext/`). Other
environments may differ:

- Some newer kernels expose an **on-demand dump file** under debugfs; a single
  `cat` forces a full dump without waiting for a stall:
  ```bash
  sudo cat /sys/kernel/debug/sched_ext/dump      # if it exists
  ```
- The sched_ext interface may live under `/sys/kernel/debug/sched_ext/` instead
  of `/sys/kernel/sched_ext/`, depending on the debugfs mount and kernel.
- **This machine has no on-demand dump file** (`/sys/kernel/sched_ext/root/`
  only has `events` / `ops` stats), so the only path here is "let it stall →
  the kernel dumps automatically."

When filing a report, note your kernel version (`uname -r`) and the exact
command you used to get the dump.

---

## 2. Inspecting current scheduling decisions on demand (without a dump)

If what you want is not a stall report but "what prio / kind / slice is each task
currently assigned," **do not use dump_task** (it's passive and kernel-triggered).
Classify mode atomically writes a snapshot every cycle:

```bash
jq . /tmp/scx_teddy/snapshot.json
```

It's `tid -> {cluster, prio, slice_ns, cpu_kind, cpu_prefer}` — pure userspace
output, no trace noise, readable any time. The GUI's Overall tab reads this too.

---

## 3. How to trigger a hang reliably (for reproduction)

To make the watchdog produce a dump, the most direct way is to create a workload
that **starves others via high priority**, so a low-priority task goes
unscheduled long enough for the kernel's stall detector to fire a dump. See
section 4 for such a workload.

When filing a bug, attach **how you triggered it** along with the dump:
1. which workload (command, priority settings);
2. the model / config used;
3. the dump content obtained above.

---

## 4. ⚠️ Known "expected behavior": a high-priority non-sleeping task hangs others

scx_teddy's dispatch is **strict priority** (`teddy_dispatch` pulls from
highest → lowest priority, and within a priority pulls the shared DSQ before
this CPU's own kind DSQ), with **no anti-starvation / aging mechanism whatsoever**.

Therefore: **if you give a non-sleeping CPU-bound task (e.g. `stress-ng --cpu N`)
a higher priority than other tasks, it will inevitably starve the lower-priority
tasks on the same core (or all cores), and the scheduler will look "hung."**

**This is expected behavior, not a bug.** A task that never sleeps and sits at
the front of a strict-priority queue means everyone behind it never gets a turn,
by design.

When investigating a stall, rule this out first:
- Check whether the stalled task in the dump is simply **lower priority than some
  busy, non-sleeping high-priority task**;
- If so, that's the config / classification placing a non-sleeping task too far
  forward. Fix the config (lower its prio, or don't put the CPU-bound cluster at
  high priority) — don't report it as a scheduler defect.

A stall worth reporting is one where there is **no** such high-priority hog yet a
task still goes unscheduled for a long time.

---

## Quick reference

| I want to see… | How |
|---|---|
| Stall report when the scheduler hangs | enable `sched_ext_dump` → trigger a stall → read `trace` / `trace_pipe`, grep `tid:` |
| Current per-task prio/kind/slice | `jq . /tmp/scx_teddy/snapshot.json` |
| Rule out an "expected" hang | confirm no high-priority non-sleeping task (`stress-ng --cpu`) is hogging the CPU |
