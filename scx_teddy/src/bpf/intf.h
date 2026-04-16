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

#define CRITICAL_DSQ 200
#define INTERACTIVE_DSQ 201
#define NORMAL_DSQ 202
#define OTHER_DSQ 203
#define CRITICAL_WAKEUP_DSQ 204
#define INTERACTIVE_WAKEUP_DSQ 205

#define DSQ_NUM 6

#define DEFAULT_SLICE 100 * 1000

#define TIER_CRITICAL 0
#define TIER_INTERACTIVE 1
#define TIER_NORMAL 2
#define TIER_BATCH 3

typedef struct task_info {
    s32 prio; // 0, 1, 2, 3
    u64 slice; // ns
} sched_info_t;

typedef struct target_ctx {
    s32 prio; // 0, 1, 2, 3
    u64 slice; // ns
    u8 config;
    /* | 7 bits NOP | 1 bits ecore |*/
    u64 start_running;
    u64 sleep_start;
    u64 sleep_end;
    u64 runtime_ns;

    u32 event_cnt;
    u64 sleep_sum; // use 1e-6 sec
    u64 sleep_sq_sum;
    u64 runtime_sum;
    u64 runtime_sq_sum;
    u32 yield_cnt; // 1 ns add 1 still not overflow
    u32 sleep_cnt;
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
    u32 yield_cnt;
    u32 sleep_cnt;
    u32 in_iowait_cnt;
    u32 futex_wait_cnt;
} task_event_t;

#define CONFIG_STOP_RINGBUF 0

#define RUNTIME_MAX_TIME 100000000
