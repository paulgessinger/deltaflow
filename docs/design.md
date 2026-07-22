# CI Performance Metrics Infrastructure – Design Notes

Status: design settled, implementation in progress.

## Goals

- Collect performance metrics continuously and store them historically.
- Associate every measurement with a commit SHA.
- Detect performance regressions over time.
- Report regressions on pull requests.
- Keep operational overhead as low as possible.

Explicit non-goal: **failing pull requests automatically.** Reports are
informational. Runtime measurements on shared CI runners are noisy enough that a
blocking gate would erode trust faster than it would catch regressions. A human
reads the comment and decides.

---

## Overall Architecture

A small Python service exposes an HTTP API.

CI jobs send arbitrary benchmark results to this API, for example:

- Runtime
- Peak memory
- Heap allocations
- Other custom metrics

The service stores all measurements in a database.

---

## Authentication

There is no single mechanism, because GitHub does not permit one. Three ingest
paths exist, each with a distinct trust level.

| Path | Used by | Mainline-eligible |
| --- | --- | --- |
| `oidc` | Same-repo pushes and pull requests | Yes |
| `token` | Submitters outside GitHub Actions (bare metal, cron) | Yes |
| `lease` | Fork pull requests | **Never** |

### OIDC (primary)

Each GitHub Actions job requests a short-lived OIDC token, naming this
deployment as the audience. The API validates it against GitHub's JWKS and
extracts attested metadata:

- Repository
- Ref and event name
- Workflow, job workflow ref
- Run ID and attempt

Authorization is derived entirely from claims; identity fields in the request
body are ignored. `event_name == push` with `ref == refs/heads/<default>` is the
only combination that yields a mainline write.

Two claim-level caveats drive the schema:

- The `sha` claim on a `pull_request` event is the **ephemeral merge commit**,
  not the branch head. It is stored as `merge_sha` and never used to anchor
  history; the real head SHA is supplied by the client.
- There is no pull request number claim. It is parsed from
  `refs/pull/<n>/merge`.

### Lease (fork pull requests)

`id-token: write` cannot be granted to `pull_request` runs originating from
forks on a public repository. Fork jobs therefore have no credential at all.

Rather than route fork results through an artifact and a trusted `workflow_run`
job — rejected as too much machinery — the fork job **claims a slot** as its
first action:

1. Job posts `{run_id, run_attempt, job, pr, head_sha}` to `/v1/claim`.
2. Server verifies against the GitHub API that the run exists in this
   repository, that the named job is currently `in_progress`, and that the
   pull request is open with a matching head SHA and head repository.
3. Server locks `(repo, run_id, run_attempt, job)` and returns a secret.
4. Results are submitted bearing that secret.

While a lease is held, nothing else can claim that slot.

### What this does and does not protect against

Deliberately **not** protected: a fork author can submit false numbers for their
own pull request. It is their pull request, the report is informational, and the
blast radius is a comment on their own change. Defending against this is what
forced the artifact machinery, and it is not worth the cost.

**Protected:** claiming a repository or pull request one has no connection to;
submitting against a closed pull request or a stale commit; writing anything at
all into a mainline baseline.

**Residual:** an attacker polling the Actions API could claim a slot in the
window between a job starting and its first HTTP call — roughly a second. This
is not eliminated, but it is *detectable*: the legitimate job's claim then
returns `409` and fails its step visibly, converting silent data corruption into
loud red CI. Given the report is non-blocking, that trade is acceptable.

Rate limiting per `(repo, run_id)` and a cap on series per pull request are
required, since the lease path is uncredentialed.

### Scoped tokens

Submitters that are not GitHub Actions jobs — a dedicated bare-metal reference
machine on a cron, most likely the source of the most trustworthy runtime
numbers — have no OIDC to offer. They authenticate with a scoped, hashed,
rotatable API token. Mainline-eligible.

---

## Data Model

Keep the schema generic. One row per **repetition**, never per aggregate.

Each measurement contains:

- Commit SHA (head; base and merge SHAs stored separately)
- Timestamp
- Metric name, unit, direction (is lower better?)
- Numeric value and repetition index
- Labels (JSON)
- Series key
- Context and trust level
- Optional metadata: workflow, job, runner, PR number, run ID, run attempt

This avoids schema migrations whenever new metrics are added.

Four details that are painful to retrofit and so are present from the start:

- **Series key.** A hash of `(repo, metric, canonical labels)`. Grouping "the
  same measurement over time" via a JSON column does not index; this does.
- **Context.** `mainline` or `pr`. Pull request measurements — including
  anything from a fork — are structurally barred from baselines.
- **Idempotency.** Unique on `(run_id, run_attempt, job, series, rep)`. CI
  reruns resubmit identical payloads; the second write must be a no-op.
- **Raw repetitions.** Within-job spread is the cheapest honest uncertainty
  estimate available, and averaging at the client discards it irrecoverably.

---

## Database

Initially SQLite, in WAL mode.

Advantages:

- Single file
- Minimal operational overhead
- WAL mode supports concurrent readers
- Easy migration to PostgreSQL later if needed

Everything goes through SQLAlchemy — no raw SQL, no SQLite-only functions — so
the PostgreSQL escape hatch stays real rather than aspirational.

---

## Visualization

Grafana talks to the Python application over HTTP (originally "Option B").

The application is the data source and performs aggregation, sliding windows,
uncertainty estimation, and regression detection. Grafana is purely a
visualization layer. Since all dashboards are maintained internally, the
abstraction remains manageable.

Concretely this means implementing the Grafana JSON datasource contract or
using the Infinity plugin.

---

## Types of Benchmarks

### Deterministic Metrics

Heap allocations, peak memory, object counts. No uncertainty estimate required —
but a relative floor is still applied, or every one-byte difference becomes a
regression.

### Runtime Benchmarks

Execution time depends on runner performance. A short reference workload (~60s)
runs **twice**, immediately before and immediately after the payload benchmark,
bracketing it.

The reference is **not** used to normalize runtimes. It quantifies variation.

Two independent signals come out of the bracket, and they answer different
questions:

| Signal | Definition | Needs history | Detects |
| --- | --- | --- | --- |
| Instability | `\|after − before\| / level` | **No** | The machine moving *during* the measurement |
| Drift | bracket level vs. the reference series' own recent median | Yes | The machine changing *between* runs |

Instability requiring no history is the valuable property: a variation estimate
exists from the very first submission, before any baseline has accumulated. On a
quiet machine it collapses toward zero on its own, with no code change.

Drift is what catches a hypervisor upgrade, a kernel change, or GitHub rolling
out a different runner generation — a step in the reference under an unchanged
repository. Since a per-run aggregate of the reference series *is* its bracket
level, this needs no special accounting: it is `baseline_points` over the
reference series.

Implementation notes:

- `role` (payload/reference) participates in series identity; `position`
  (before/after) deliberately does not — the two halves are two samples of one
  series, not two series.
- Both halves are required. A job whose after-run was skipped yields no
  bracket rather than a fabricated estimate.
- `group` ties a bracket to the payload it surrounds, defaulting to the job.
  Set it explicitly when a job runs several benchmarks with separate brackets.
- **Label references by runner class.** A single reference series spanning both
  GitHub-hosted runners and dedicated hardware makes the drift signal
  meaningless.

Note what this design does *not* assume: nothing about noise scaling
proportionally between the reference and the payload. The bracket describes the
machine and is reported as such, next to the payload rather than folded into it.

---

## Statistical Uncertainty

Uncertainty is computed dynamically at read time. Because of that, improved
statistical methods automatically apply to all historical data.

**There is no automated regression detection, and none is planned as a gate.**
The report is descriptive: it states the measured value, where that sits
relative to recent history, and how much the machine moved while measuring. It
classifies nothing. A human reads it and decides.

Detection may be added later, for information only. The machinery for it
(`stats.py` — median location, IQR-derived spread, relative floor) exists and is
tested but is deliberately not wired into the report. Two properties are
non-negotiable whenever it is turned on:

- **Robust to outliers.** One CI hiccup in the baseline window must not widen
  the band enough to hide a real change.
- **No normality assumption for runtime.** Timing noise is right-skewed and
  one-sided; a slow run is always possible, a negatively-slow one is not.

Worth stealing from Bencher's taxonomy if that day comes: static, percentage,
z-score, t-test, log-normal, IQR, delta-IQR, each with min/max sample size and a
time window.

Because figures are computed at read time, a report regenerated later will not
reproduce today's numbers. Each posted report therefore snapshots its content
and a method version, keeping past statements auditable without freezing the
method.

---

## Pull Request Reporting

Each benchmark job submits results directly to the API. No artifact collection
or synchronization is required.

The comment is **upserted**, identified by a hidden marker, and rewritten on
every submission with everything known so far.

Content:

- Each measured series: value, change against recent mainline, and the recent
  range for context
- Machine behaviour per job: instability during the run, drift against the
  machine's own norm
- Which jobs have claimed but not yet reported
- Link to Grafana dashboard

No verdicts, no pass/fail, no gating — see *Statistical Uncertainty*.

---

## Detecting Completion

**Not solved — dissolved.**

GitHub never reports which checks *will* run, only which have started, so there
is no complete-set signal to wait for. Benchmarks originating from multiple
independent workflows make this worse.

Because the comment is upserted on every submission, there is no moment that
must be recognised as "done" and no webhook receiver is needed. Results appear
incrementally as they arrive.

The lease table gives partial-progress display for free: the server knows which
jobs have claimed but not yet reported, so the comment can say "4 of 6 benchmark
jobs reported."

---

## Prior Art Considered

- **Bencher** — actively developed, runs on any runner, MIT/Apache core with a
  source-available `plus` tier. Rejected on fit, not cost: no OIDC ingest, no
  equivalent of the reference-benchmark noise model, its own frontend. Its
  threshold taxonomy is worth borrowing.
- **Conbench** — nearly this exact design, but effectively abandoned (last
  commit on main August 2024). Worth reading, not depending on.
- **github-action-benchmark** — maintenance mode; simpler than what is needed.
- **CodSpeed** — commercial SaaS, not self-hostable.

---

## Local Simulation

`tools/simulate.py` (local tooling, not shipped, not in the CLI) drives the real
API in-process against a real SQLite file with GitHub stubbed out, so the whole
flow can be exercised without CI or network.

The machine is modelled as an AR(1) process in log space — real runners have bad
afternoons, so noise persists across runs rather than resampling independently.
The reference is sampled at the first and last tick of a run, exactly as
bracketing samples it in reality.

Validated behaviour, injecting a +25% hardware step under an unchanged payload:

| Injected σ | Instability | Drift (true step +25%) | Payload delta (truth unchanged) |
| --- | --- | --- | --- |
| 0.005 | ±1.70% | +25.26% | +24.78% |
| 0.02 | ±4.94% | +24.26% | +26.01% |
| 0.05 | ±11.42% | +21.67% | +28.94% |
| 0.15 | ±32.75% | +13.23% | +38.03% |

Drift recovers the injected step while the machine is quiet and degrades
gracefully as noise swamps it; instability scales monotonically with σ. The last
column is the point of the exercise: the payload appears to regress by 25–38%
in every row while its true cost never changed.

---

## Open Questions

- The `UNSTABLE_PCT` (5%) and `DRIFT_PCT` (10%) thresholds are round numbers,
  not derived. They want calibrating against real runner noise.
- Dashboard design
- Retention and downsampling policy
- Whether one bracket per job suffices, or benchmarks need individual brackets

---

## Current Architecture

- Python API (FastAPI)
- SQLite in WAL mode (PostgreSQL later if necessary)
- Three-tier auth: OIDC, lease, scoped token
- Grafana frontend over HTTP
- Generic metric storage, one row per repetition
- Read-time uncertainty estimation, snapshotted verdicts
- Incrementally upserted PR comment, no completion detection
- Informational only; never blocks a merge
