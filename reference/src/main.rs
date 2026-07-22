//! Fixed-work reference workload for deltaflow reference bracketing.
//!
//! This binary is run immediately before and immediately after a payload
//! benchmark. The difference between the two runs is how much the machine
//! moved *during* the measurement; the level against history is how far the
//! machine sits from its own norm. Neither is used to normalise the payload.
//!
//! Everything here exists to hold constant everything except the machine:
//!
//! - **Fixed work, not fixed time.** Every constant below is a compile-time
//!   count. A faster machine finishes sooner; it does not do more.
//! - **No dependencies, no allocator pressure, no threads, no I/O, no clock
//!   reads inside a kernel.** The quietest process we can arrange.
//! - **Deterministic inputs.** A hand-rolled xorshift, not `rand`, so the same
//!   bytes are consumed on every machine for all time.
//! - **`black_box` on every kernel boundary.** The most common way a reference
//!   workload fails is that the optimiser deletes it: a loop with no observable
//!   output is dead code, and a reference that got eliminated still reports a
//!   plausible-looking small number. `checksum` in the output is the tripwire —
//!   it is bit-for-bit identical on every machine, and if it ever differs, the
//!   measurement is meaningless and must not be submitted.
//!
//! Three kernels, because a single one is blind by construction: a pure-ALU
//! reference cannot see a memory-bandwidth change on the host, which is exactly
//! the kind of thing a hypervisor upgrade does.
//!
//!   fp        dense matmul, working set in L2  -> core clock, turbo, thermal
//!   latency   dependent pointer chase, 64 MiB  -> memory latency, placement
//!   bandwidth STREAM triad, 48 MiB             -> bandwidth, noisy neighbours
//!
//! The submitted value is the *total* of the three, because the server keys a
//! bracket on (job, group) without a series component -- two reference series
//! under one group would pool into one nonsensical bracket. The per-kernel
//! split is reported for diagnosis, not for submission.

use std::hint::black_box;
use std::time::Instant;

/// Workload generation. This lands in the series label as
/// `reference-fixed@<WORKLOAD>`.
///
/// Bump it if and only if the measured work changes. A patch release that
/// touches argument parsing or machine-fact collection must NOT bump it: the
/// timings are still comparable, and splitting the series throws away the
/// history that the drift signal is computed from. Conversely, never change a
/// kernel constant without bumping it — that silently puts incomparable numbers
/// in one series, which reads as a hardware change that never happened.
const WORKLOAD: u32 = 1;

const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Matrix order for the FP kernel. 192x192xf64 is 288 KiB per matrix, so the
/// working set sits in L2 on every runner we care about and the kernel measures
/// arithmetic throughput rather than memory.
const N: usize = 192;
const FP_PASSES: usize = 400;

/// 16 Mi u32 = 64 MiB, comfortably past any last-level cache we will meet, so
/// every step is a main-memory round trip. The chase is serially dependent on
/// purpose: no prefetcher can hide it, so this measures latency, not bandwidth.
const CHASE_ENTRIES: usize = 16 << 20;
const CHASE_STEPS: usize = 4 << 20;

/// 2 Mi f64 = 16 MiB per array, three arrays. Streaming and cache-hostile.
const TRIAD_LEN: usize = 2 << 20;
const TRIAD_ITERS: usize = 384;

/// Deterministic PRNG. Not good randomness — identical randomness, forever.
struct Xorshift(u64);

impl Xorshift {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
}

/// Dense matmul, ikj order so the inner loop is unit-stride and vectorises.
///
/// Vectorisation happens over `j`, while each output element accumulates over
/// `k` in source order — so the summation order is fixed and the checksum is
/// bit-reproducible. Rust does not contract to FMA by default, which is what
/// makes that true across microarchitectures.
fn kernel_fp(a: &[f64], b: &[f64], c: &mut [f64]) -> f64 {
    let mut acc = 0.0f64;
    for _ in 0..FP_PASSES {
        c.fill(0.0);
        for i in 0..N {
            for k in 0..N {
                let aik = a[i * N + k];
                let brow = &b[k * N..k * N + N];
                let crow = &mut c[i * N..i * N + N];
                for j in 0..N {
                    crow[j] += aik * brow[j];
                }
            }
        }
        // Consume the result so the pass cannot be hoisted out of the loop.
        acc += black_box(&c)[N * N - 1];
    }
    acc
}

/// Serially dependent chase around a single Hamiltonian cycle.
///
/// Sattolo's algorithm gives one cycle covering every entry, so the chase can
/// never fall into a short loop that fits in cache.
fn kernel_latency(ring: &[u32]) -> u64 {
    let mut idx = 0usize;
    let mut sum = 0u64;
    for _ in 0..CHASE_STEPS {
        idx = ring[idx] as usize;
        sum = sum.wrapping_add(idx as u64);
    }
    sum
}

/// STREAM triad: a = b + s*c.
///
/// `s` varies per iteration so that the iterations are not provably identical
/// and cannot be collapsed into one.
fn kernel_bandwidth(a: &mut [f64], b: &[f64], c: &[f64]) -> f64 {
    let mut acc = 0.0f64;
    for it in 0..TRIAD_ITERS {
        let s = 3.0 + (it as f64) * 1e-9;
        for i in 0..TRIAD_LEN {
            a[i] = b[i] + s * c[i];
        }
        acc += black_box(&a)[TRIAD_LEN - 1];
    }
    acc
}

struct Timings {
    fp: Vec<f64>,
    latency: Vec<f64>,
    bandwidth: Vec<f64>,
    total: Vec<f64>,
}

fn run(repeat: usize) -> (Timings, u64) {
    // Setup is outside every timed region. Allocation and page faults are not
    // part of the measured work.
    let mut rng = Xorshift(0x9e37_79b9_7f4a_7c15);

    let a: Vec<f64> = (0..N * N)
        .map(|_| (rng.next() >> 11) as f64 * (1.0 / (1u64 << 53) as f64))
        .collect();
    let b: Vec<f64> = (0..N * N)
        .map(|_| (rng.next() >> 11) as f64 * (1.0 / (1u64 << 53) as f64))
        .collect();
    let mut c = vec![0.0f64; N * N];

    let mut ring: Vec<u32> = (0..CHASE_ENTRIES as u32).collect();
    // Sattolo: i from len-1 down to 1, swap with j uniform in [0, i).
    for i in (1..CHASE_ENTRIES).rev() {
        let j = (rng.next() % i as u64) as usize;
        ring.swap(i, j);
    }

    let tb: Vec<f64> = (0..TRIAD_LEN).map(|i| 1.0 + (i % 17) as f64).collect();
    let tc: Vec<f64> = (0..TRIAD_LEN).map(|i| 2.0 + (i % 13) as f64).collect();
    let mut ta = vec![0.0f64; TRIAD_LEN];

    // Touch every page once so that first-touch faults land here rather than in
    // the first repetition, which would otherwise read as an outlier.
    let mut touch = 0u64;
    for chunk in ring.chunks(1024) {
        touch = touch.wrapping_add(chunk[0] as u64);
    }
    black_box(touch);
    ta.fill(0.0);
    black_box(&mut ta);

    let mut t = Timings {
        fp: Vec::with_capacity(repeat),
        latency: Vec::with_capacity(repeat),
        bandwidth: Vec::with_capacity(repeat),
        total: Vec::with_capacity(repeat),
    };
    let mut checksum: u64 = 0;

    for _ in 0..repeat {
        let t0 = Instant::now();
        let r_fp = kernel_fp(black_box(&a), black_box(&b), black_box(&mut c));
        let d_fp = t0.elapsed().as_secs_f64();

        let t1 = Instant::now();
        let r_lat = kernel_latency(black_box(&ring));
        let d_lat = t1.elapsed().as_secs_f64();

        let t2 = Instant::now();
        let r_bw = kernel_bandwidth(black_box(&mut ta), black_box(&tb), black_box(&tc));
        let d_bw = t2.elapsed().as_secs_f64();

        // Fold every kernel's result in. Identical on every machine, every run,
        // and independent of --repeat: each repetition recomputes the same
        // answer, so the checksum is a pure function of the workload constants
        // and can be pinned in `reference/CHECKSUM`.
        let mut rep = r_fp.to_bits();
        rep = rep.rotate_left(17) ^ r_lat;
        rep = rep.rotate_left(17) ^ r_bw.to_bits();

        if t.total.is_empty() {
            checksum = rep;
        } else if rep != checksum {
            // Repetitions differ, so the machine did not compute the same
            // answer twice. That is a hardware or miscompilation problem, not a
            // slow runner, and no timing from this process means anything.
            eprintln!("checksum diverged between repetitions: {checksum:016x} != {rep:016x}");
            std::process::exit(1);
        }

        t.fp.push(d_fp);
        t.latency.push(d_lat);
        t.bandwidth.push(d_bw);
        t.total.push(d_fp + d_lat + d_bw);
    }

    (t, checksum)
}

// --- machine facts ---------------------------------------------------------
//
// Not metrics. When drift fires, these turn "the machine changed" into "the
// machine changed from Skylake to Sapphire Rapids", which is the difference
// between a mystery and an explanation.

fn cpu_model() -> String {
    if let Ok(s) = std::fs::read_to_string("/proc/cpuinfo") {
        for line in s.lines() {
            if let Some((key, value)) = line.split_once(':') {
                let key = key.trim();
                if key == "model name" || key == "Model" || key == "cpu model" {
                    return value.trim().to_string();
                }
            }
        }
    }
    if cfg!(target_os = "macos") {
        if let Ok(out) = std::process::Command::new("sysctl")
            .args(["-n", "machdep.cpu.brand_string"])
            .output()
        {
            let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !s.is_empty() {
                return s;
            }
        }
    }
    String::new()
}

/// Container CPU budget. A runner given 2 of 64 cores behaves nothing like the
/// bare machine, and cgroup v2 is where GitHub-hosted runners express that.
fn cgroup_quota() -> String {
    std::fs::read_to_string("/sys/fs/cgroup/cpu.max")
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

// --- output ----------------------------------------------------------------

fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn nums(v: &[f64]) -> String {
    v.iter()
        .map(|x| format!("{x:.6}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn usage() -> ! {
    eprintln!(
        "deltaflow-reference {VERSION} (workload {WORKLOAD})\n\
         \n\
         usage: deltaflow-reference [--repeat N]\n\
         \n\
         Runs a fixed amount of work N times and writes one JSON object to\n\
         stdout. Values are seconds. Work is fixed, so a faster machine reports\n\
         smaller numbers -- that is the entire point.\n\
         \n\
           --repeat N   repetitions, 1..=64 (default 5)\n\
           --version    print version and exit\n"
    );
    std::process::exit(2)
}

fn main() {
    let mut repeat = 5usize;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--repeat" | "-n" => {
                i += 1;
                repeat = args
                    .get(i)
                    .and_then(|s| s.parse().ok())
                    .unwrap_or_else(|| usage());
            }
            "--version" | "-V" => {
                println!("deltaflow-reference {VERSION} workload {WORKLOAD}");
                return;
            }
            _ => usage(),
        }
        i += 1;
    }
    if repeat == 0 || repeat > 64 {
        usage();
    }

    let (t, checksum) = run(repeat);

    let cpus = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(0);

    println!(
        r#"{{
  "reference": "reference-fixed@{workload}",
  "workload": {workload},
  "version": "{version}",
  "unit": "s",
  "repeat": {repeat},
  "checksum": "{checksum:016x}",
  "total": [{total}],
  "kernels": {{
    "fp": [{fp}],
    "latency": [{latency}],
    "bandwidth": [{bandwidth}]
  }},
  "machine": {{
    "os": "{os}",
    "arch": "{arch}",
    "cpus": {cpus},
    "cpu": "{cpu}",
    "cgroup_cpu_max": "{quota}"
  }}
}}"#,
        workload = WORKLOAD,
        version = VERSION,
        repeat = repeat,
        checksum = checksum,
        total = nums(&t.total),
        fp = nums(&t.fp),
        latency = nums(&t.latency),
        bandwidth = nums(&t.bandwidth),
        os = std::env::consts::OS,
        arch = std::env::consts::ARCH,
        cpus = cpus,
        cpu = esc(&cpu_model()),
        quota = esc(&cgroup_quota()),
    );
}
