// slow_timer.c
#include <unistd.h>
#include <sys/prctl.h>

int main() {
    prctl(PR_SET_NAME, "slow-timer");
    while (1) {
        usleep(100000);  // 10Hz
        // 做一點小事
        volatile int x = 0;
        for (int i = 0; i < 1000; i++) x += i;
    }
}
