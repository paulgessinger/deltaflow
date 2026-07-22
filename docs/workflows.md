# Submitting from CI

Three paths exist. `deltaflow submit` picks between the first two automatically;
only the fork path needs anything extra in the workflow.

## The bundled actions

Three composite actions cover the whole job. Nothing stops you calling the CLI
directly — they exist so the ordering constraints below are not everyone's
problem to remember.

| Action | When |
| --- | --- |
| `claim-action` | First step. No-ops unless the job is a fork PR. |
| `reference-action` | Immediately before and immediately after the payload. |
| `submit-action` | Last step. Merges everything and posts it. |

```yaml
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  id-token: write        # ignored on fork PRs; the claim step covers those

jobs:
  bench:
    runs-on: ubuntu-24.04
    env:
      DELTAFLOW_SERVER: https://deltaflow.example.org
    steps:
      # First. On a fork PR this reserves the slot; otherwise it does nothing.
      - uses: acts-project/deltaflow/claim-action@v1
        with:
          server: ${{ env.DELTAFLOW_SERVER }}

      - uses: actions/checkout@v5

      - uses: acts-project/deltaflow/reference-action@reference-v1.0.0
        with:
          position: before
          runner: ubuntu-24.04

      - run: ./run-benchmarks.sh > payload.json

      - uses: acts-project/deltaflow/reference-action@reference-v1.0.0
        with:
          position: after
          runner: ubuntu-24.04

      - uses: acts-project/deltaflow/submit-action@v1
        with:
          server: ${{ env.DELTAFLOW_SERVER }}
          results: payload.json
```

Order is the load-bearing part:

- **Claim first, before checkout.** The window in which something else could
  take the slot is the time between the job starting and the claim landing.
  Checking out first widens it for no benefit.
- **The reference brackets the payload and nothing else.** Put checkout, build
  and cache restore *before* the `before` half. Work between the halves is work
  the instability signal attributes to the machine.
- **Submit once, at the end.** `submit-action` merges the payload with both
  bracket halves; splitting it into two submissions produces two runs and no
  bracket.

The payload step is yours: `results` takes whatever files your benchmark binary
produced, as paths or globs, in the [result format](#result-format).

## Result format

```json
[
  {
    "metric": "runtime",
    "unit": "s",
    "direction": "lower_better",
    "labels": {"benchmark": "seeding", "build": "release"},
    "values": [1.204, 1.198, 1.211, 1.207]
  },
  {
    "metric": "allocations",
    "unit": "count",
    "labels": {"benchmark": "seeding"},
    "deterministic": true,
    "values": [42017]
  }
]
```

Send **every repetition**, not a mean. Within-job spread is the cheapest honest
uncertainty estimate available, and averaging at the client discards it for
good. A single value is fine for deterministic metrics.

Labels define series identity. Keep them stable: changing a label starts a new
series with no history, and there is no way to merge them afterwards.

Mark metrics that do not depend on machine speed with `"deterministic": true` —
allocation counts, object counts, instruction counts. They then carry no machine
uncertainty, because none applies: a slow runner does not change how many bytes
the code allocates.

## Reference bracketing

Run a short fixed workload immediately before and immediately after the payload.
The supported way is the bundled action, which downloads a published binary,
verifies its checksum, runs it, and writes a measurement file:

```yaml
      - uses: acts-project/deltaflow/reference-action@reference-v1.0.0
        with:
          position: before
          runner: ubuntu-24.04

      - run: ./run-benchmarks.sh > payload.json

      - uses: acts-project/deltaflow/reference-action@reference-v1.0.0
        with:
          position: after
          runner: ubuntu-24.04

      - run: jq -s add deltaflow-reference-*.json payload.json > results.json
```

Pin the action to an exact `reference-vX.Y.Z`. The binary is built once and
published as a release asset precisely so that every run measures the same work;
a floating tag can move under you and put incomparable timings in one series.
See [reference/README.md](../reference/README.md) for why it is not built at
benchmark time.

The measurements it emits look like this — nothing stops you producing them
yourself, but then the stability of the workload is on you:

```json
[
  {"metric": "runtime", "unit": "s", "role": "reference", "position": "before",
   "labels": {"benchmark": "reference-fixed@1", "runner": "ubuntu-24.04"},
   "values": [3.61, 3.58, 3.62, 3.59, 3.60]},

  {"metric": "runtime", "unit": "s",
   "labels": {"benchmark": "seeding", "runner": "ubuntu-24.04"},
   "values": [1.204, 1.198, 1.211]},

  {"metric": "runtime", "unit": "s", "role": "reference", "position": "after",
   "labels": {"benchmark": "reference-fixed@1", "runner": "ubuntu-24.04"},
   "values": [3.84, 3.91, 3.86, 3.88, 3.85]}
]
```

The `@1` in the benchmark label is the workload generation. It is there so that
changing the reference starts a new series instead of putting a step into the old
one, which would read as a hardware change that never happened.

This yields two things the payload alone cannot tell you:

- **Instability** — how much the machine moved *during* your measurement. Needs
  no history, so it works on the very first run.
- **Drift** — how far the machine sits from its own recent norm, which is what
  catches a hypervisor upgrade or a new runner generation.

Both feed the error bar on every measurement (`11.7 s ± 0.494 s` in the comment,
`value`/`sigma`/`lower`/`upper` from `/v1/timeseries` for dashboard bands).
Without a bracket, only repetition spread contributes and the bar understates.

Both halves must be submitted or no bracket is formed. **Label the reference
with the runner class**: one reference series spanning both GitHub-hosted
runners and dedicated hardware makes drift meaningless.

If a job runs several benchmarks each with their own bracket, set `group` to tie
each bracket to its payload. It defaults to the job name.

## Same-repo pushes and pull requests (OIDC)

```yaml
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  id-token: write        # required, and unavailable to fork pull requests

jobs:
  bench:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - run: ./run-benchmarks.sh > results.json
      - run: |
          uvx deltaflow submit results.json \
            --head-sha "${{ github.event.pull_request.head.sha || github.sha }}" \
            --base-sha "${{ github.event.pull_request.base.sha }}"
        env:
          DELTAFLOW_SERVER: https://deltaflow.example.org
          DELTAFLOW_AUDIENCE: https://deltaflow.example.org
```

Note `github.event.pull_request.head.sha`. On a `pull_request` event
`github.sha` is the throwaway merge commit, which is regenerated whenever the
base branch moves — anchoring history to it produces rows that orphan
themselves.

## Fork pull requests (lease)

Fork jobs cannot be granted `id-token: write`, so they hold no credential. They
instead claim their submission slot, and the server verifies with GitHub that
the job really is running before granting it.

**Claim as the job's first step.** That is the entire point: it shrinks the
window in which anything else could take the slot from the benchmark's whole
duration down to a moment.

`claim-action` does this and decides for itself whether a claim is needed — it
checks whether the job was granted an OIDC token, which is the actual capability
the choice turns on, rather than reading `head.repo.fork`. The raw equivalent:

```yaml
jobs:
  bench:
    runs-on: ubuntu-latest
    env:
      DELTAFLOW_SERVER: https://deltaflow.example.org
    steps:
      - name: Claim benchmark slot
        if: github.event.pull_request.head.repo.fork
        run: |
          echo "DELTAFLOW_LEASE=$(uvx deltaflow claim \
            --pr ${{ github.event.pull_request.number }} \
            --head-sha ${{ github.event.pull_request.head.sha }})" >> "$GITHUB_ENV"

      - uses: actions/checkout@v5
      - run: ./run-benchmarks.sh > results.json
      - run: uvx deltaflow submit results.json --head-sha "${{ github.event.pull_request.head.sha }}"
```

If the claim returns `409`, the slot was already taken. If that job did not take
it, someone is submitting under its identity — the step fails loudly rather than
letting bad data through quietly.

Submission failures do **not** fail the build by default; pass `--fail-on-error`
to change that. Benchmark reporting is informational and should never be able to
break CI.

## Machines outside GitHub Actions (token)

A bare-metal reference machine on a cron has no OIDC to offer. Issue it a scoped
token:

```console
$ deltaflow mint-token bare-metal-01 --repo acts-project/acts
xK7f...   # shown once; only the hash is stored
```

```console
$ DELTAFLOW_TOKEN=xK7f... deltaflow submit results.json \
    --head-sha "$(git rev-parse HEAD)"
```

These submissions are mainline-eligible, so the token is worth protecting: it is
the one credential whose compromise corrupts baselines rather than a single
comment. Revoke by setting `revoked` on the row.
