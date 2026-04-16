#!/bin/bash

# Compile custom workloads
gcc -o slow_timer slow_timer.c
gcc -o random_timer random_timer.c -lm
gcc -o fix_mutex fix_mutex.c -lpthread

# All spawned PIDs (tracked manually since stress-ng workers are not shell jobs)
ALL_PIDS=()

cleanup() {
    echo ""
    echo "Cleaning up..."
    for pid in "${ALL_PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    # Also kill any remaining shell background jobs
    kill $(jobs -p) 2>/dev/null
    wait 2>/dev/null
    echo "All child processes terminated."
    exit 0
}
trap cleanup INT TERM

# ---------------------------------------------------------------------------
# Task pool: each entry is "type:remaining_count"
# Types and their total target counts (matching bench_mark.sh)
# stress-ng workers are spawned one at a time via --xxx 1
# ---------------------------------------------------------------------------
declare -A REMAINING
REMAINING[slow_timer]=12
REMAINING[random_timer]=12
REMAINING[fix_mutex]=1
REMAINING[cpu]=12
REMAINING[hdd]=12
REMAINING[switch]=12
REMAINING[timer]=12

TYPES=(slow_timer random_timer fix_mutex cpu hdd switch timer)

spawn_one() {
    local t="$1"
    local pid
    case "$t" in
        slow_timer)
            ./slow_timer &
            pid=$!
            ;;
        random_timer)
            ./random_timer &
            pid=$!
            ;;
        fix_mutex)
            ./fix_mutex &
            pid=$!
            ;;
        cpu)
            stress-ng --cpu 1 --cpu-method fft &
            pid=$!
            ;;
        hdd)
            stress-ng --hdd 1 --hdd-bytes 256M &
            pid=$!
            ;;
        switch)
            stress-ng --switch 1 &
            pid=$!
            ;;
        timer)
            stress-ng --timer 1 --timer-freq 500 &
            pid=$!
            ;;
    esac
    ALL_PIDS+=("$pid")
}

# ---------------------------------------------------------------------------
# Gradual spawn loop: every 1 second, pick random types that still have
# remaining quota and spawn up to 2 of each chosen type.
# ---------------------------------------------------------------------------
total_remaining() {
    local s=0
    for t in "${TYPES[@]}"; do
        s=$(( s + REMAINING[$t] ))
    done
    echo "$s"
}

echo "Starting gradual workload spawn..."

while [ "$(total_remaining)" -gt 0 ]; do
    # Collect types that still have quota
    available=()
    for t in "${TYPES[@]}"; do
        [ "${REMAINING[$t]}" -gt 0 ] && available+=("$t")
    done

    # Shuffle and pick a random subset (at least 1, at most all available)
    n_avail=${#available[@]}
    # Fisher-Yates shuffle on available array
    for (( i = n_avail - 1; i > 0; i-- )); do
        j=$(( RANDOM % (i + 1) ))
        tmp="${available[$i]}"
        available[$i]="${available[$j]}"
        available[$j]="$tmp"
    done
    # Pick between 1 and n_avail types this round
    n_pick=$(( (RANDOM % n_avail) + 1 ))

    for (( k = 0; k < n_pick; k++ )); do
        t="${available[$k]}"
        # Spawn up to 2 of this type (but not more than remaining)
        cap=$(( REMAINING[$t] < 2 ? REMAINING[$t] : 2 ))
        count=$(( (RANDOM % cap) + 1 ))
        for (( c = 0; c < count; c++ )); do
            [ "${REMAINING[$t]}" -le 0 ] && break
            spawn_one "$t"
            REMAINING[$t]=$(( REMAINING[$t] - 1 ))
            echo "  spawned $t (remaining: ${REMAINING[$t]})"
        done
    done

    sleep 1
done

echo "All tasks spawned. Press Ctrl+C to stop."

# Wait indefinitely until Ctrl+C
wait
cleanup
