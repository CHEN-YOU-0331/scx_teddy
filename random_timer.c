// random_timer.c — sleep with mean=100ms, stddev=100ms (log-normal)
#include <math.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <sys/prctl.h>

// Box-Muller transform: generate standard normal
static double randn(void) {
    double u1 = (double)rand() / RAND_MAX;
    double u2 = (double)rand() / RAND_MAX;
    if (u1 < 1e-15) u1 = 1e-15;
    return sqrt(-2.0 * log(u1)) * cos(2.0 * M_PI * u2);
}

int main() {
    prctl(PR_SET_NAME, "random-timer");
    srand(time(NULL) ^ getpid());

    // Log-normal parameters for mean=100ms, stddev=100ms
    // sigma^2 = ln(1 + (std/mean)^2) = ln(2)
    // mu = ln(mean) - sigma^2/2
    const double sigma = sqrt(log(2.0));
    const double mu = log(100000.0) - 0.5 * sigma * sigma;

    while (1) {
        double val = exp(mu + sigma * randn());
        useconds_t us = (useconds_t)(val > 1.0 ? val : 1.0);
        usleep(us);
        // 做一點小事
        volatile int x = 0;
        for (int i = 0; i < 1000; i++) x += i;
    }
}
