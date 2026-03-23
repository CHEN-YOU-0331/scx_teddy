#[derive(Debug, Clone, Default)]
pub struct TaskStats {
    // Runtime statistics
    runtime_sum: u64,
    runtime_sum_sq: f64,  // Sum of squares for variance calculation
    runtime_min: u64,
    runtime_max: u64,

    // Sleep statistics
    sleep_sum: u64,
    sleep_sum_sq: f64,
    sleep_min: u64,
    sleep_max: u64,
    sleep_count: u64,  // Number of events with sleep

    // Sleep interval statistics (time between sleeps)
    last_sleep_end: u64,
    sleep_interval_sum: u64,
    sleep_interval_sum_sq: f64,
    sleep_interval_min: u64,
    sleep_interval_max: u64,
    sleep_interval_count: u64,

    event_count: u64,
    parent: i32,
    pub exit: u8,
}

impl TaskStats {
    pub fn new(parent: i32) -> Self {
        Self {
            runtime_sum: 0,
            runtime_sum_sq: 0.0,
            runtime_min: u64::MAX,
            runtime_max: 0,

            sleep_sum: 0,
            sleep_sum_sq: 0.0,
            sleep_min: u64::MAX,
            sleep_max: 0,
            sleep_count: 0,

            last_sleep_end: 0,
            sleep_interval_sum: 0,
            sleep_interval_sum_sq: 0.0,
            sleep_interval_min: u64::MAX,
            sleep_interval_max: 0,
            sleep_interval_count: 0,

            event_count: 0,
            parent,
            exit: 0,
        }
    }

    pub fn update(&mut self, runtime_ns: u64, sleep_ns: u64, sleep_end: u64) {
        self.event_count += 1;

        // Update runtime statistics
        self.runtime_sum += runtime_ns;
        self.runtime_sum_sq += (runtime_ns as f64) * (runtime_ns as f64);
        self.runtime_min = self.runtime_min.min(runtime_ns);
        self.runtime_max = self.runtime_max.max(runtime_ns);

        // Update sleep statistics
        if sleep_ns > 0 {
            self.sleep_count += 1;
            self.sleep_sum += sleep_ns;
            self.sleep_sum_sq += (sleep_ns as f64) * (sleep_ns as f64);
            self.sleep_min = self.sleep_min.min(sleep_ns);
            self.sleep_max = self.sleep_max.max(sleep_ns);

            // Update sleep interval statistics
            if self.last_sleep_end > 0 && sleep_end > self.last_sleep_end {
                let interval = sleep_end - self.last_sleep_end;
                self.sleep_interval_count += 1;
                self.sleep_interval_sum += interval;
                self.sleep_interval_sum_sq += (interval as f64) * (interval as f64);
                self.sleep_interval_min = self.sleep_interval_min.min(interval);
                self.sleep_interval_max = self.sleep_interval_max.max(interval);
            }
            self.last_sleep_end = sleep_end;
        }
    }

    fn avg_runtime_ms(&self) -> f64 {
        if self.event_count > 0 {
            (self.runtime_sum as f64 / self.event_count as f64) / 1_000_000.0
        } else {
            0.0
        }
    }

    fn stddev_runtime_ms(&self) -> f64 {
        if self.event_count > 1 {
            let mean = self.runtime_sum as f64 / self.event_count as f64;
            let variance = (self.runtime_sum_sq / self.event_count as f64) - (mean * mean);
            (variance.max(0.0).sqrt()) / 1_000_000.0
        } else {
            0.0
        }
    }

    fn avg_sleep_ms(&self) -> f64 {
        if self.sleep_count > 0 {
            (self.sleep_sum as f64 / self.sleep_count as f64) / 1_000_000.0
        } else {
            0.0
        }
    }

    fn stddev_sleep_ms(&self) -> f64 {
        if self.sleep_count > 1 {
            let mean = self.sleep_sum as f64 / self.sleep_count as f64;
            let variance = (self.sleep_sum_sq / self.sleep_count as f64) - (mean * mean);
            (variance.max(0.0).sqrt()) / 1_000_000.0
        } else {
            0.0
        }
    }

    fn avg_sleep_interval_ms(&self) -> f64 {
        if self.sleep_interval_count > 0 {
            (self.sleep_interval_sum as f64 / self.sleep_interval_count as f64) / 1_000_000.0
        } else {
            0.0
        }
    }

    fn stddev_sleep_interval_ms(&self) -> f64 {
        if self.sleep_interval_count > 1 {
            let mean = self.sleep_interval_sum as f64 / self.sleep_interval_count as f64;
            let variance = (self.sleep_interval_sum_sq / self.sleep_interval_count as f64) - (mean * mean);
            (variance.max(0.0).sqrt()) / 1_000_000.0
        } else {
            0.0
        }
    }

    pub fn get_stats(&self) -> [f64; 15] {
        [
            self.event_count as f64,
            // Runtime statistics
            self.avg_runtime_ms(),
            self.stddev_runtime_ms(),
            self.runtime_min as f64 / 1_000_000.0,
            self.runtime_max as f64 / 1_000_000.0,
            // Sleep statistics
            self.sleep_count as f64,
            self.avg_sleep_ms(),
            self.stddev_sleep_ms(),
            self.sleep_min as f64 / 1_000_000.0,
            self.sleep_max as f64 / 1_000_000.0,
            // Sleep interval statistics
            self.sleep_interval_count as f64,
            self.avg_sleep_interval_ms(),
            self.stddev_sleep_interval_ms(),
            self.sleep_interval_min as f64 / 1_000_000.0,
            self.sleep_interval_max as f64 / 1_000_000.0,
        ]
    }
}
