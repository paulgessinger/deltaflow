# Submitting from CI

Three paths exist. `deltaflow submit` picks between the first two automatically;
only the fork path needs anything extra in the workflow.

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
    "values": [42017]
  }
]
```

Send **every repetition**, not a mean. Within-job spread is the cheapest honest
uncertainty estimate available, and averaging at the client discards it for
good. A single value is fine for deterministic metrics.

Labels define series identity. Keep them stable: changing a label starts a new
series with no history, and there is no way to merge them afterwards.

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
