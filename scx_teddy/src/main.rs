// SPDX-License-Identifier: GPL-2.0
//! scx_teddy - A BPF scheduler based on task runtime characteristics

use std::io::Write;
use std::mem::MaybeUninit;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::Parser;
use serde::{Deserialize, Serialize};
use plain::Plain;

use libbpf_rs::skel::OpenSkel;
use libbpf_rs::skel::SkelBuilder;
use libbpf_rs::MapCore;
use libbpf_rs::MapFlags;

mod task_stats;

use task_stats::TaskStats;

mod bpf_skel {
    include!(concat!(env!("OUT_DIR"), "/bpf_skel.rs"));
}

mod bpf_intf {
    include!(concat!(env!("OUT_DIR"), "/intf.rs"));
}

#[allow(clippy::wildcard_imports)]
use bpf_skel::*;

#[derive(Debug, Deserialize, Serialize)]
struct TaskConfig {
    tid: i32,
    prio: i32,
    slice: u64,
    on_ecore: u8,
}

#[derive(Debug, Deserialize, Serialize)]
struct Config {
    target_mode: i32,
    tgid: Option<i32>,
    tasks: Vec<TaskConfig>,
}

#[derive(Parser, Debug)]
#[command(name = "scx_teddy")]
#[command(about = "scx_teddy - A BPF scheduler based on task runtime characteristics", long_about = None)]
struct Args {
    /// Verbose output
    #[arg(short, long, default_value_t = false)]
    verbose: bool,

    /// Data collection interval in seconds
    #[arg(short, long, default_value_t = 600)]
    collect_duration: u64,

    /// Mode: "collect" to generate event.csv, "classify" to use a trained model
    #[arg(short, long, default_value = "collect")]
    mode: String,

    /// Minimum event count to include a task in event.csv (filter inactive tasks)
    #[arg(long, default_value_t = 3)]
    min_events: u64,

    /// Output CSV file path (collect mode)
    #[arg(short, long, default_value = "event.csv")]
    output: String,
}

#[repr(C)]
struct TaskEvent {
    tid: i32,
    parent: i32,
    sleep_start: u64,
    sleep_end: u64,
    runtime_ns: u64
}

unsafe impl Plain for TaskEvent {}

// Process event received from ring buffer
fn process_event(data: &[u8], stats: &Arc<Mutex<std::collections::HashMap<i32, TaskStats>>>) -> i32 {
    let event = plain::from_bytes::<TaskEvent>(data).unwrap();

    let sleep_duration = if event.sleep_end > event.sleep_start {
        event.sleep_end - event.sleep_start
    } else {
        0
    };

    // Update statistics
    let mut stats = stats.lock().unwrap();

    if event.parent > 0 {
        let task_stats = stats.entry(event.tid).or_insert(TaskStats::new(event.parent));
        task_stats.update(event.runtime_ns, sleep_duration, event.sleep_end);
    } else if event.parent == -1 {
        if let Some(task_stats) = stats.get_mut(&event.tid) {
            task_stats.exit = 1;
        }
    }

    0
}

const CSV_HEADER: &str = "tid,event_count,avg_runtime_ms,stddev_runtime_ms,runtime_min_ms,runtime_max_ms,sleep_count,avg_sleep_ms,stddev_sleep_ms,sleep_min_ms,sleep_max_ms,sleep_interval_count,avg_sleep_interval_ms,stddev_sleep_interval_ms,sleep_interval_min_ms,sleep_interval_max_ms";

fn main() -> Result<()> {
    let args = Args::parse();

    match args.mode.as_str() {
        "collect" | "classify" => {}
        _ => anyhow::bail!("Invalid mode '{}'. Use 'collect' or 'classify'.", args.mode),
    }

    if args.mode == "classify" {
        println!("Classify mode is not yet implemented.");
        return Ok(());
    }
    println!("scx_teddy scheduler starting...");

    // Build and load eBPF skeleton
    let skel_builder = BpfSkelBuilder::default();
    let mut open_object = MaybeUninit::uninit();
    let mut open_skel = skel_builder.open(&mut open_object).context("Failed to open BPF object")?;

    // Initialize SCX enums from kernel BTF (SCX_DSQ_LOCAL_ON, etc.)
    scx_utils::import_enums!(open_skel);

    let mut skel = open_skel.load().context("Failed to load BPF object")?;

    // Load and attach the scheduler struct_ops
    let _struct_ops = skel
        .maps
        .teddy_ops
        .attach_struct_ops()
        .context("Failed to attach struct_ops")?;

    // Statistics storage
    let stats: Arc<Mutex<std::collections::HashMap<i32, TaskStats>>> =
        Arc::new(Mutex::new(std::collections::HashMap::new()));
    let stats_clone = Arc::clone(&stats);

    let mut builder = libbpf_rs::RingBufferBuilder::new();
    builder
        .add(&skel.maps.events, move |data| process_event(data, &stats_clone))
        .context("Failed to add ringbuf")?;
    let ringbuf = builder.build().context("Failed to build ringbuf")?;

    let scheduler_config = &skel.maps.scheduler_config;

    println!("scx_teddy scheduler loaded successfully!");
    println!("Press Ctrl+C to exit...\n");

    // Setup Ctrl+C handler
    let running = Arc::new(Mutex::new(true));
    let running_clone = Arc::clone(&running);
    ctrlc::set_handler(move || {
        println!("\nReceived Ctrl+C, shutting down...");
        *running_clone.lock().unwrap() = false;
    })
    .expect("Error setting Ctrl+C handler");

    let mut start_time = Instant::now();
    let duration = Duration::from_secs(args.collect_duration);

    // Main loop - keep scheduler running
    while *running.lock().unwrap() {
        if start_time.elapsed() >= duration {
            let key = 0u32.to_ne_bytes();
            let mut val = 1u32.to_ne_bytes();
            scheduler_config.update(&key, &val, MapFlags::ANY)?;
            let mut stats_map = stats.lock().unwrap();

            // Write collected stats to CSV
            let file_exists = std::path::Path::new(&args.output).exists();
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&args.output)
                .context("Failed to open output CSV")?;

            if !file_exists {
                writeln!(file, "{}", CSV_HEADER)
                    .context("Failed to write CSV header")?;
            }

            let mut count = 0u64;
            for (&tid, task_stats) in stats_map.iter() {
                if task_stats.exit != 0 {
                    continue;
                }
                let stats_arr = task_stats.get_stats();
                // stats_arr[0] is event_count
                if (stats_arr[0] as u64) < args.min_events {
                    println!("{} too few data", tid);
                    continue;
                }
                let values: Vec<String> = stats_arr.iter().map(|v| format!("{}", v)).collect();
                writeln!(file, "{},{}", tid, values.join(","))
                    .context("Failed to write CSV row")?;
                count += 1;
            }
            println!("Wrote {} tasks to {}", count, args.output);

            stats_map.clear();
            start_time = Instant::now();
            val = 0u32.to_ne_bytes();
            scheduler_config.update(&key, &val, MapFlags::ANY)?;
        }
        ringbuf.poll(Duration::from_millis(1000))?;
    }

    println!("scx_teddy scheduler exiting...");

    Ok(())
}
