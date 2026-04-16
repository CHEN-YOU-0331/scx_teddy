// fixed_mutex.c
#include <pthread.h>
#include <stdlib.h>
#include <sys/prctl.h>

pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
volatile int shared = 0;

void *worker(void *arg) {
    while (1) {
        pthread_mutex_lock(&lock);
        for (int i = 0; i < 1000; i++)
            shared++;
        pthread_mutex_unlock(&lock);
        sched_yield();
    }
    return NULL;
}

int main(int argc, char *argv[]) {
    int n = argc > 1 ? atoi(argv[1]) : 12;
    prctl(PR_SET_NAME, "fixed-mutex");
    
    pthread_t *threads = malloc(n * sizeof(pthread_t));
    for (int i = 0; i < n; i++)
        pthread_create(&threads[i], NULL, worker, NULL);
    
    for (int i = 0; i < n; i++)
        pthread_join(threads[i], NULL);
}
