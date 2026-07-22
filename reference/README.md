# deltaflow reference workload

A fixed-work binary that runs immediately before and immediately after a payload
benchmark, so the report can say what the *machine* was doing while the payload
was measured. It is never used to normalise a payload — see
[docs/design.md](../docs/design.md).

```console
$ cargo build --release
$ ./target/release/deltaflow-reference --repeat 5
```

## Why it is built this way

The reference has to hold constant everything except the machine. Four
consequences, each of which is load-bearing:

**Fixed work, never fixed time.** Every constant in `src/main.rs` is a
compile-time count. `stress-ng` and `sysbench cpu` are time-boxed by default —
they do more work on a faster machine and report the same duration, which is
precisely backwards for this.

**Built once, shipped as bytes.** The binary is compiled in
`.github/workflows/reference-release.yml` and published to a `reference-v*`
release. It is never built at benchmark time: a locally compiled reference
drifts with the compiler, libc and container image, so the drift signal would
fire on toolchain bumps — the one class of change it exists to distinguish from
hardware. Static musl linkage removes the glibc coupling too.

**Zero dependencies.** This file gets touched about once a year. Every crate in
the tree is a chance that the next generation fails to build.

**The checksum is the tripwire.** A benchmark loop with no observable output is
dead code, and the usual failure is not a crash — it is the optimiser deleting
the work while the binary still prints plausible seconds. `black_box` guards
every kernel boundary and the checksum is pinned in `CHECKSUM`, verified in CI
and again at release. It is bit-reproducible across machines because Rust does
not contract to FMA and the reduction orders are fixed.

## Kernels

One kernel is blind by construction — a pure-ALU reference cannot see a
memory-bandwidth change on the host, which is exactly what a hypervisor upgrade
looks like.

| Kernel | Shape | Sensitive to |
| --- | --- | --- |
| `fp` | 192² f64 matmul, ikj, working set in L2 | core clock, turbo, thermal throttling |
| `latency` | dependent pointer chase, 64 MiB single cycle | memory latency, NUMA placement |
| `bandwidth` | STREAM triad, 48 MiB across three arrays | bandwidth, noisy neighbours |

Constants are sized so the three cost roughly the same. On an M4 Pro a
repetition is ~1.1 s; on a GitHub-hosted runner expect 3–4 s, so the default
`--repeat 5` costs ~20 s per half and ~40 s per bracketed job.

**The submitted value is the total, not the three separately.** `queries.brackets`
keys a bracket on `(job, group)` with no series component, so two reference
series under one group would pool into one meaningless bracket. The per-kernel
split is emitted for diagnosis. Splitting the axes properly needs the bracket key
to grow a series component first.

## Releasing

1. Change the workload → bump `WORKLOAD` in `src/main.rs` **and** regenerate
   `CHECKSUM`. They move together or the series silently mixes incomparable
   timings. Changes that do not touch a kernel (argument parsing, machine facts)
   must *not* bump `WORKLOAD`: splitting the series throws away the history the
   drift signal is computed from.
2. Bump `version` in `Cargo.toml`; the release workflow refuses a mismatched tag.
3. Tag `reference-vX.Y.Z` and push.
4. Update the `version` default in `../reference-action/action.yml`.

**Assets on a published release are immutable.** Everyone who pinned that
version verified the bytes already there, and the action's `SHA256SUMS` check is
only as good as that promise. The release job refuses to publish over an
existing tag rather than leaving it to discipline.

## Not yet built

- Only `x86_64-unknown-linux-musl` is published. `aarch64` needs a cross
  toolchain in the release job; the action fails with a clear message until then.
- No CPU affinity pinning — that would need the `libc` crate, and the zero-dependency
  property is worth more than pinning until measurements say otherwise.
