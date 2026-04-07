gcc -o slow_timer slow_timer.c

# 跑 12 個
for i in $(seq 12); do ./slow_timer & done

# 搭配原本的 stress-ng
stress-ng \
  --cpu 12 --cpu-method fft \
  --hdd 12 --hdd-bytes 256M \
  --switch 12 \
  --timer 12 --timer-freq 500 \
  --pthread 12 \
  --metrics-brief
