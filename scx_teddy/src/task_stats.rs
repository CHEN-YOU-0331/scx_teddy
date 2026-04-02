#[derive(Debug, Clone, Default)]
pub struct TaskStats {
    // Runtime statistics
    runtime_sum: u64,
    runtime_sum_sq: f64,  // Sum of squares for variance calculation

    // Sleep statistics
    sleep_sum: u64,
    sleep_sum_sq: f64,
    sleep_count: u64,  // Number of events with sleep (used internally for avg/cv)

    event_count: u64,
    parent: i32,
    pub exit: u8,
}

impl TaskStats {
    pub fn new(parent: i32) -> Self {
        Self {
            runtime_sum: 0,
            runtime_sum_sq: 0.0,

            sleep_sum: 0,
            sleep_sum_sq: 0.0,
            sleep_count: 0,

            event_count: 0,
            parent,
            exit: 0,
        }
    }

    pub fn update(&mut self, runtime_ns: u64, sleep_ns: u64, _sleep_end: u64) {
        self.event_count += 1;

        // Update runtime statistics
        self.runtime_sum += runtime_ns;
        self.runtime_sum_sq += (runtime_ns as f64) * (runtime_ns as f64);

        // Update sleep statistics
        if sleep_ns > 0 {
            self.sleep_count += 1;
            self.sleep_sum += sleep_ns;
            self.sleep_sum_sq += (sleep_ns as f64) * (sleep_ns as f64);
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

    /// Coefficient of variation for runtime (stddev / avg). 0 if avg is 0.
    fn runtime_cv(&self) -> f64 {
        let avg = self.avg_runtime_ms();
        if avg > 0.0 { self.stddev_runtime_ms() / avg } else { 0.0 }
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

    /// Coefficient of variation for sleep (stddev / avg). 0 if avg is 0.
    fn sleep_cv(&self) -> f64 {
        let avg = self.avg_sleep_ms();
        if avg > 0.0 { self.stddev_sleep_ms() / avg } else { 0.0 }
    }

    /// Returns (name, value) pairs for all features.
    /// The order here defines the CSV column order and feature vector order.
    pub fn get_named_stats(&self) -> Vec<(&'static str, f64)> {
        vec![
            ("event_count", self.event_count as f64),
            ("avg_runtime_ms", self.avg_runtime_ms()),
            ("runtime_cv", self.runtime_cv()),
            ("avg_sleep_ms", self.avg_sleep_ms()),
            ("sleep_cv", self.sleep_cv()),
        ]
    }

    /// Returns feature values as a Vec (order matches get_named_stats).
    pub fn get_stats(&self) -> Vec<f64> {
        self.get_named_stats().into_iter().map(|(_, v)| v).collect()
    }

    /// Returns feature names (order matches get_stats).
    pub fn get_feature_names() -> Vec<&'static str> {
        Self::default().get_named_stats().into_iter().map(|(n, _)| n).collect()
    }
}
