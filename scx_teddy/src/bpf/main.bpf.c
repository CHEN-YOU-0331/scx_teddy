// SPDX-License-Identifier: GPL-2.0
/* scx_teddy - A BPF scheduler based on task runtime characteristics */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>

#include <scx/common.bpf.h>
#include <scx/compat.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

/* CPU topology, filled by userspace (topology.rs) into rodata before load. */
const volatile u8 cpu_num;       // number of online logical CPUs in use
const volatile u8 cpu_kind_num;  // number of distinct freq kinds (>= 1)
/* CPUs sorted by speed. cpus_fast_to_slow[0] is the fastest CPU id;
 * cpus_slow_to_fast is the reverse. Only the first `cpu_num` entries are set. */
const volatile u8 cpus_fast_to_slow[MAX_CPU];
const volatile u8 cpus_slow_to_fast[MAX_CPU];
/* Indexed by logical CPU id. */
const volatile cpu_info_t cpu_info[MAX_CPU];

/* DSQ layout
 * ----------
 * Each priority owns a contiguous block of (1 + cpu_kind_num) DSQs starting at
 * DSQ_BASE, in priority order (prio 0 first). Within a priority block the slot
 * is the CPU kind:
 *   slot 0          = the shared DSQ — any CPU kind may pull from it.
 *   slot 1..kind_num = the kind-only DSQ for that CPU kind (kinds are 1-based,
 *                      kind 1 = fastest; see topology.rs CpuInfo::cpu_kind).
 * Total DSQ count is PRIORITY_NUM * (1 + cpu_kind_num).
 *
 * dispatch pulls slot 0 (shared, "runs anywhere") before the running CPU's own
 * kind slot within a priority, and walks priorities 0 -> PRIORITY_NUM-1.
 */

/* Number of DSQs in one priority block. */
static __always_inline u32 dsq_per_prio(void)
{
    return 1 + cpu_kind_num;
}

/* DSQ id for a priority and slot. slot 0 = shared, slot k = CPU kind k. */
static __always_inline u64 dsq_id(s32 prio, u32 kind)
{
    return DSQ_BASE + (u64)prio * dsq_per_prio() + kind;
}

#define MIN_SEND_INTERVAL 100000000

struct {
    __uint(type, BPF_MAP_TYPE_TASK_STORAGE);
    __uint(map_flags, BPF_F_NO_PREALLOC);
    __type(key, int);
    __type(value, target_ctx_t);
} task_ctx SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, u32);
    __type(value, u32);
} scheduler_config SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 256 * 1024);
    __type(key, s32);
    __type(value, sched_info_t);
} update_map SEC(".maps");

static void try_data_to_user(struct task_struct *p, target_ctx_t *target_ctx)
{
    u32 key = CONFIG_STOP_RINGBUF;
    u32 *pause_ringbuf = bpf_map_lookup_elem(&scheduler_config, &key);

    if (*pause_ringbuf) {
        return; // don't clear the data
    }
    
    u64 now = scx_bpf_now();

    if (now - target_ctx->last_send_time < MIN_SEND_INTERVAL)
        return;
    target_ctx->last_send_time = now;

    task_event_t *e = bpf_ringbuf_reserve(&events, sizeof(task_event_t), 0);
    if (!e) {
        bpf_printk("RINGBUF_FULL");
        return; // Ring buffer full, drop event
    }

    // Fill event data
    e->tid = p->pid;
    e->parent = p->real_parent->pid;
    e->event_cnt = target_ctx->event_cnt;
    e->sleep_sum = target_ctx->sleep_sum;
    e->sleep_sq_sum = target_ctx->sleep_sq_sum;
    e->runtime_sum = target_ctx->runtime_sum;
    e->runtime_sq_sum = target_ctx->runtime_sq_sum;
    e->sleep_cnt = target_ctx->sleep_cnt;
    e->in_iowait_cnt = target_ctx->in_iowait_cnt;
    e->futex_wait_cnt = target_ctx->futex_wait_cnt;

    // Submit to ring buffer
    bpf_ringbuf_submit(e, 0);

    target_ctx->event_cnt = 0;
    target_ctx->sleep_sum = 0;
    target_ctx->sleep_sq_sum = 0;
    target_ctx->runtime_sum = 0;
    target_ctx->runtime_sq_sum = 0;
    target_ctx->sleep_cnt = 0;
    target_ctx->in_iowait_cnt = 0;
    target_ctx->futex_wait_cnt = 0;
}

static target_ctx_t *get_target_storage(struct task_struct *p)
{
    target_ctx_t *target_ctx;
    target_ctx = bpf_task_storage_get(&task_ctx, p, 0, 0);

    if (unlikely(!target_ctx)) {
        target_ctx = bpf_task_storage_get(&task_ctx, p, 0,
                               BPF_LOCAL_STORAGE_GET_F_CREATE);
        if (unlikely(!target_ctx))
            return NULL;
        s32 key = p->pid;
        target_ctx->slice = DEFAULT_SLICE;
        target_ctx->prio = DEFAULT_PRIO;
        target_ctx->config = 1;
        target_ctx->kind = 0; // default: shared DSQ (no kind restriction)
        target_ctx->last_send_time = bpf_ktime_get_ns();

        target_ctx->start_running = target_ctx->sleep_start = target_ctx->sleep_end = target_ctx->runtime_ns = 0;
    }

    return target_ctx;
}

static __always_inline s32 dispatch_sync_cold(struct task_struct *p, target_ctx_t *target_ctx, u64 wake_flags)
{
    u32 cpu = bpf_get_smp_processor_id();
    if (!bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
        return -1;

    scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (u64)cpu, target_ctx->slice, wake_flags);
    return (s32)cpu;
}

s32 BPF_STRUCT_OPS(teddy_select_cpu, struct task_struct *p, s32 prev_cpu,
                   u64 wake_flags)
{
    target_ctx_t *target_ctx = get_target_storage(p);
    if (!target_ctx) {
        scx_bpf_dsq_insert(p, dsq_id(DEFAULT_PRIO, 0), DEFAULT_SLICE, wake_flags);
        return prev_cpu;
    }

    if (target_ctx->prio >= CRITICAL_PRIO) {
        scx_bpf_dsq_insert(p, dsq_id(DEFAULT_PRIO, target_ctx->kind), DEFAULT_SLICE, wake_flags);
        return prev_cpu;
    }
        
    // p is woken by this cpu
    if (wake_flags & SCX_WAKE_SYNC) {
        s32 sync_cpu = dispatch_sync_cold(p, target_ctx, wake_flags);
        if (sync_cpu >= 0)
            return sync_cpu;
    }
    bool is_idle;
    s32 cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);

    if (is_idle) {
        scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (u64)cpu, target_ctx->slice, wake_flags);
        return cpu;
    }

    return prev_cpu;
}

void BPF_STRUCT_OPS(teddy_enqueue, struct task_struct *p, u64 enq_flags)
{
    target_ctx_t *target_ctx = get_target_storage(p);
    if (!target_ctx) {
        /* No ctx: fall back to the lowest-priority shared DSQ (batch). */
        scx_bpf_dsq_insert(p, dsq_id(DEFAULT_PRIO, 0), DEFAULT_SLICE, enq_flags);
        return;
    }

    scx_bpf_dsq_insert(p, dsq_id(target_ctx->prio, target_ctx->kind),
                       target_ctx->slice, enq_flags);
}

void BPF_STRUCT_OPS(teddy_dispatch, s32 cpu, struct task_struct *prev)
{
    /* cpu is the running CPU id, always in [0, cpu_num) — but the verifier
     * treats the s32 arg as possibly negative, so cpu_info[cpu] would read
     * out of bounds in its eyes. Clamp explicitly so the index is provably
     * within [0, MAX_CPU). */
    if (cpu < 0 || cpu >= MAX_CPU)
        return;
    /* This CPU's kind selects which kind-only DSQ this CPU may pull from. */
    u32 kind = cpu_info[cpu].cpu_kind;

    /* Walk priorities high -> low. Within each priority, pull the shared
     * (any-kind) DSQ before this CPU's own kind DSQ — "runs anywhere" wins. */
    for (s32 prio = 0; prio < PRIORITY_NUM; prio++) {
        if (scx_bpf_dsq_move_to_local(dsq_id(prio, 0)))
            return;
        if (scx_bpf_dsq_move_to_local(dsq_id(prio, kind)))
            return;
    }
}

void BPF_STRUCT_OPS(teddy_tick, struct task_struct *p)
{
}

/* Initialize the scheduler */
s32 BPF_STRUCT_OPS_SLEEPABLE(teddy_init)
{
    /* Dump the topology rodata filled by userspace (topology.rs), visible via
     * `cat /sys/kernel/debug/tracing/trace_pipe`. Loops are bounded by MAX_CPU
     * (compile-time) for the verifier and broken early at cpu_num. */
    bpf_printk("teddy topo: cpu_num=%u cpu_kind_num=%u", cpu_num, cpu_kind_num);
    for (u32 i = 0; i < MAX_CPU; i++) {
        if (i >= cpu_num)
            break;
        bpf_printk("teddy topo: cpu%u kind=%u freq=%u/%u",
                   i, cpu_info[i].cpu_kind, cpu_info[i].freq_n, cpu_info[i].freq_d);
    }
    for (u32 i = 0; i < MAX_CPU; i++) {
        if (i >= cpu_num)
            break;
        bpf_printk("teddy topo: order[%u] fast_to_slow=%u slow_to_fast=%u",
                   i, cpus_fast_to_slow[i], cpus_slow_to_fast[i]);
    }

    /* Create the DSQs now that the topology is known: PRIORITY_NUM priority
     * blocks of (1 + cpu_kind_num) DSQs each (slot 0 shared + one per kind).
     * The loop is bounded by the compile-time max (PRIORITY_NUM * (1 +
     * MAX_CPU_KIND)) for the verifier and broken early at the real count. */
    u32 dsq_total = (u32)PRIORITY_NUM * dsq_per_prio();
    for (u32 i = 0; i < PRIORITY_NUM * (1 + MAX_CPU_KIND); i++) {
        if (i >= dsq_total)
            break;
        s32 ret = scx_bpf_create_dsq(DSQ_BASE + i, -1);
        if (ret < 0)
            return ret;
    }

    return 0;
}

void BPF_STRUCT_OPS(teddy_runnable, struct task_struct *p, u64 enq_flags)
{
    target_ctx_t *target_ctx = get_target_storage(p);
    if (!target_ctx)
        return;
    if (enq_flags & SCX_ENQ_WAKEUP)
        target_ctx->sleep_end = scx_bpf_now();
}

void BPF_STRUCT_OPS(teddy_running, struct task_struct *p)
{
    target_ctx_t *target_ctx = get_target_storage(p);
    if (!target_ctx)
        return;
    target_ctx->start_running = scx_bpf_now();
}

static void update_event_data(target_ctx_t *target_ctx)
{
    target_ctx->event_cnt++;
    u64 sleep_mus;
    if (target_ctx->sleep_end > target_ctx->sleep_start) 
        sleep_mus = (target_ctx->sleep_end - target_ctx->sleep_start) >> 10;
    else
        sleep_mus = 0;
    if (sleep_mus > (1ULL << 31)) 
        sleep_mus = 1ULL << 31;
    target_ctx->sleep_sum += sleep_mus;
    target_ctx->sleep_sq_sum += sleep_mus * sleep_mus;

    // runtime have upper bound RUNTIME_MAX_TIME, won't overflow directly
    target_ctx->runtime_sum += target_ctx->runtime_ns;
    target_ctx->runtime_sq_sum += target_ctx->runtime_ns * target_ctx->runtime_ns;

    target_ctx->runtime_ns = 0;
    target_ctx->sleep_end = 0;
}

void BPF_STRUCT_OPS(teddy_stopping, struct task_struct *p, bool runnable)
{
    u64 now = scx_bpf_now();
    target_ctx_t *target_ctx = get_target_storage(p);
    if (!target_ctx)
        return;
    
    u64 now_runtime = now - target_ctx->start_running;
    /* Normalize CPU frequency differences between big and little cores 
     * to avoid overestimating task load when tasks are executed on little 
     * cores.*/
    {
        u32 cpu = bpf_get_smp_processor_id();
        u64 freq_n = cpu_info[cpu].freq_n;
        u64 freq_d = cpu_info[cpu].freq_d;
        if (freq_n != freq_d) {
            now_runtime *= freq_n;
            now_runtime /= freq_d;
        }
    }

    target_ctx->runtime_ns += now_runtime;
    target_ctx->in_iowait_cnt += BPF_CORE_READ_BITFIELD_PROBED(p, in_iowait);

    if (!runnable) {
        target_ctx->sleep_cnt++;
        if (target_ctx->sleep_start != 0) {
            update_event_data(target_ctx);
            try_data_to_user(p, target_ctx);
        }
        target_ctx->sleep_start = now;
    } else {
        if (target_ctx->runtime_ns >= RUNTIME_MAX_TIME) {
            update_event_data(target_ctx);
            try_data_to_user(p, target_ctx);
        }
    }

    s32 key = p->pid;
    sched_info_t *update_info = bpf_map_lookup_elem(&update_map, &key);
    if (unlikely(update_info)) {
        target_ctx->prio = update_info->prio;
        target_ctx->slice = update_info->slice;
        target_ctx->kind = update_info->kind;
        bpf_map_delete_elem(&update_map, &key);
    }

}

void BPF_STRUCT_OPS(teddy_exit_task, struct task_struct *p, struct scx_exit_task_args *args)
{
    u32 key = CONFIG_STOP_RINGBUF;
    u32 *pause_ringbuf = bpf_map_lookup_elem(&scheduler_config, &key);

    if (*pause_ringbuf)
        goto clear_tracing_data;

    task_event_t *e = bpf_ringbuf_reserve(&events, sizeof(task_event_t), 0);
    if (!e)
        return;

    e->tid = p->pid;
    e->parent = -1;

    bpf_ringbuf_submit(e, 0);
clear_tracing_data:
}

/* Scheduler exit - record exit info */
void BPF_STRUCT_OPS(teddy_exit, struct scx_exit_info *ei)
{
    UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(teddy_ops,
               .select_cpu     = (void *)teddy_select_cpu,
               .enqueue        = (void *)teddy_enqueue,
               .dispatch       = (void *)teddy_dispatch,
               .tick           = (void *)teddy_tick,
               .runnable       = (void *)teddy_runnable,
               .running        = (void *)teddy_running,
               .stopping       = (void *)teddy_stopping,
               .exit_task      = (void *)teddy_exit_task,
               .init           = (void *)teddy_init,
               .exit           = (void *)teddy_exit,
               .flags          = SCX_OPS_KEEP_BUILTIN_IDLE,
               .name           = "teddy");

SEC("kprobe/futex_wait")
int BPF_KPROBE(trace_futex_wait)
{
    struct task_struct *p = bpf_get_current_task_btf();
    target_ctx_t *target_ctx = get_target_storage(p);
    if (target_ctx)
        target_ctx->futex_wait_cnt++;
    return 0;
}
