#!/bin/bash

# Compile custom workloads
gcc -o slow_timer slow_timer.c
gcc -o random_timer random_timer.c -lm
gcc -o fix_mutex fix_mutex.c -lpthread

# Cleanup function: kill our background jobs on Ctrl+C / exit
cleanup() {
    echo ""
    echo "Cleaning up..."
    kill $(jobs -p) 2>/dev/null
    wait 2>/dev/null
    echo "All child processes terminated."
    exit 0
}
trap cleanup INT TERM

# Launch slow_timer x12
for i in $(seq 12); do ./slow_timer & done

# Launch random_timer x12
for i in $(seq 12); do ./random_timer & done

# Launch fix_mutex
./fix_mutex &

# stress-ng (runs in foreground, will be interrupted by Ctrl+C too)
stress-ng \
  --cpu 12 --cpu-method fft \
  --hdd 12 --hdd-bytes 256M \
  --switch 12 \
  --timer 12 --timer-freq 500 \
  --metrics-brief

# If stress-ng exits on its own (e.g. --timeout), clean up the rest
cleanup
