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

Execution time depends on runner performance. A reference benchmark runs
alongside the actual benchmark.

The reference benchmark is **not** used to normalize runtimes. It estimates
current platform noise.

Note the assumption this rests on: that noise scales proportionally between the
reference and the real benchmark. That fails when their profiles differ — a
cache-thrashing benchmark and a tight ALU loop do not share a noise
distribution. Treat it as a hypothesis to validate against real data, not a
given. This is the one genuinely novel part of the design and no off-the-shelf
tool implements it.

---

## Statistical Uncertainty

Uncertainty is computed dynamically at read time. Because of that, improved
statistical methods automatically apply to all historical data.

v1 is deliberately crude — a robust location estimate (median) with an
IQR-derived spread and a relative floor. Two properties are non-negotiable even
in v1:

- **Robust to outliers.** One CI hiccup in the baseline window must not widen
  the band enough to hide a real regression.
- **No normality assumption for runtime.** Timing noise is right-skewed and
  one-sided; a slow run is always possible, a negatively-slow one is not.

Worth stealing from Bencher's taxonomy as the model matures: static, percentage,
z-score, t-test, log-normal, IQR, delta-IQR, each with min/max sample size and a
time window.

Because verdicts are computed at read time, a comparison rerun later will not
reproduce today's answer. Each posted report therefore snapshots its verdict and
a method version, keeping past decisions auditable without freezing the method.

---

## Pull Request Reporting

Each benchmark job submits results directly to the API. No artifact collection
or synchronization is required.

The comment is **upserted**, identified by a hidden marker, and rewritten on
every submission with everything known so far.

Desired content:

- Runtime change
- Whether the change exceeds statistical uncertainty
- Memory changes
- Heap allocation changes
- Link to Grafana dashboard

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

## Open Questions

- Statistical methodology beyond v1 (validate the reference-benchmark
  assumption first)
- Dashboard design
- PR comment format details
- Retention and downsampling policy

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
