#include <limits.h>
#include <stdbool.h>

/* Type defs for BPF/userspace compat - defined when vmlinux.h is not included */
#ifndef __VMLINUX_H__
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef unsigned long u64;

typedef signed char s8;
typedef signed short s16;
typedef signed int s32;
typedef signed long s64;
#endif

#define MODE_TID    0  
#define MODE_TGID   1

#define DSQ_BASE 200
#define PRIORITY_NUM 12
#define CRITICAL_PRIO 4 // prio < 4 is critical
#define DEFAULT_PRIO 11 // use lowest priority as default

#define DEFAULT_SLICE 100 * 1000

/* Upper bound on logical CPUs for the topology arrays below. */
#define MAX_CPU 255

/* Upper bound on distinct CPU kinds (freq tiers). A compile-time cap is needed
 * to bound the DSQ-creation loop for the verifier; real hardware has 1-3. */
#define MAX_CPU_KIND 8

/* Per-CPU topology */
typedef struct cpu_info {
    u8 cpu_kind;  // 0 = fastest kind
    u32 freq_n;   // numerator   = this CPU's max_freq (kHz)
    u32 freq_d;   // denominator = fastest CPU's max_freq (kHz)
} cpu_info_t;

typedef struct task_info {
    s32 prio; // 0, 1, 2, 3
    u8 kind;  // DSQ slot: 0 = shared (any kind), 1..cpu_kind_num = kind-only
    u64 slice; // ns
} sched_info_t;

typedef struct target_ctx {
    s32 prio; // 0, 1, 2, 3
    u64 slice; // ns
    u8 config;
    /* | 7 bits NOP | 1 bits ecore |*/
    u8 kind;  // DSQ slot: 0 = shared (any kind), 1..cpu_kind_num = kind-only
    u64 start_running;
    u64 sleep_start;
    u64 sleep_end;
    u64 runtime_ns;

    u64 last_send_time;

    u32 event_cnt;
    u64 sleep_sum; // use 1e-6 sec
    u64 sleep_sq_sum;
    u64 runtime_sum;
    u64 runtime_sq_sum;
    u32 sleep_cnt; // 1 ns add 1 still not overflow
    u32 in_iowait_cnt;
    u32 futex_wait_cnt;
} target_ctx_t;

typedef struct task_event {
    s32 tid;  // Thread ID (statistics are per-TID)
    s32 parent;
    u32 event_cnt;
    u64 sleep_sum; // use 1e-6 sec
    u64 sleep_sq_sum;
    u64 runtime_sum;
    u64 runtime_sq_sum;
    u32 sleep_cnt;
    u32 in_iowait_cnt;
    u32 futex_wait_cnt;
} task_event_t;

#define CONFIG_STOP_RINGBUF 0

#define RUNTIME_MAX_TIME 100000000
