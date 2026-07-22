# deltaflow

Continuous benchmark tracking for CI: collect performance metrics from GitHub
Actions, store them against commits, and report changes on pull requests.

Reports are **informational** and never gate a merge.

- [Design notes](docs/design.md) — architecture, trust model, statistics
- [Submitting from CI](docs/workflows.md) — workflow snippets and result format

## Quick start

```console
$ uv sync
$ uv run deltaflow initdb
$ uv run deltaflow serve --reload
```

Configuration is via `DELTAFLOW_*` environment variables or a `.env` file:

```bash
DELTAFLOW_DATABASE_URL=sqlite:///deltaflow.db
DELTAFLOW_AUDIENCE=https://deltaflow.example.org   # must match what CI requests
DELTAFLOW_ALLOWED_REPOS='["acts-project/acts"]'
DELTAFLOW_DEFAULT_BRANCH=main
DELTAFLOW_BASELINE_WINDOW=50

# Needed to post pull request comments and to verify fork submissions
DELTAFLOW_GITHUB_APP_ID=...
DELTAFLOW_GITHUB_PRIVATE_KEY=...
DELTAFLOW_GITHUB_INSTALLATION_ID=...
```

## How it fits together

Benchmark jobs POST raw repetitions to `/v1/submit`. Nothing is aggregated on
write — the database holds one row per repetition — so improved statistical
methods apply retroactively to all history.

Authentication has three tiers, because GitHub does not permit one:

| Path | Used by | May write baselines |
| --- | --- | --- |
| OIDC | Same-repo pushes and pull requests | Yes |
| Token | Machines outside GitHub Actions | Yes |
| Lease | Fork pull requests | No |

Fork pull requests cannot mint an OIDC token, so they claim a slot instead and
the server confirms with GitHub that the job is genuinely running. Fork authors
can still submit false numbers for their own pull request — that is accepted;
the report is informational and the blast radius is their own change. What is
prevented is anything untrusted reaching a mainline baseline.

The pull request comment is upserted on every submission, so results from any
number of independent workflows appear incrementally. There is no completion
detection and no webhook receiver.

## Status

Working end to end. The statistical model (`stats.py`) is deliberately a v1 —
median plus an IQR-derived band with a relative floor — and wants tuning against
real history before anyone trusts its verdicts.

## Development

```console
$ uv run pytest
```
