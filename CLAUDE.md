# deltaflow

Continuous benchmark tracking for CI. GitHub Actions jobs POST benchmark results;
the service stores them against commits and reports changes on pull requests.

Full rationale in [docs/design.md](docs/design.md); CI-side usage in
[docs/workflows.md](docs/workflows.md). This file covers what you need to work in
the code without breaking something subtle.

## Commands

```console
$ uv sync
$ uv run pytest                                    # 79 tests, ~1s
$ uv run deltaflow serve --reload                  # API on :8000
$ uv run deltaflow migrate                         # apply schema migrations
$ uv run deltaflow db-status                       # exits 1 if behind
$ uv run deltaflow github check                    # verify App credentials
$ uv run deltaflow mint-token NAME --repo OWNER/REPO

# Local simulation (global flags precede the subcommand)
$ uv run python tools/simulate.py --noise 0.05 flow --effect 0.15
$ uv run python tools/simulate.py --noise 0.02 drift --hardware-step 0.25
```

`tools/simulate.py` drives the real API in-process against SQLite with GitHub
stubbed. Use it to eyeball report output and to check that derived machine
quantities behave — it is the fastest feedback loop in the repo.

## Layout

| Module | Responsibility |
| --- | --- |
| `models.py` | Schema. One row per *repetition*, never per aggregate. |
| `schemas.py` | Wire format. Identity fields are deliberately absent. |
| `auth.py` | OIDC validation; `RunAttestor` for the fork path. |
| `api.py` | Endpoints. Resolves any credential to one `Identity`. |
| `queries.py` | Read paths, reference brackets, per-run history. |
| `stats.py` | Aggregation, uncertainty, and unwired verdict machinery. |
| `reporting.py` | Builds and renders the pull request comment. |
| `github.py` | GitHub App client; comment upsert, retries. |
| `grafana.py` | Grafana JSON datasource endpoints. |
| `ratelimit.py` | Fixed-window counters for the uncredentialed claim path. |
| `deps.py` | Shared FastAPI dependencies. Tests override these by identity. |

## Invariants — do not break these

**Lease-derived measurements never reach mainline.** `Trust.LEASE` is barred
structurally, not by convention. Fork PRs can submit false numbers for their own
PR (accepted; blast radius is their own change), but a poisoned baseline corrupts
every future comparison. This is the one boundary that matters.

**Identity comes from claims, never from the request body.** Repository, run id,
and PR number are derived from the OIDC token or the lease. The body carries
measurements and `head_sha` only.

**`head_sha` must be the real head commit.** The OIDC `sha` claim on a
`pull_request` event is the ephemeral merge commit, which is regenerated when the
base moves. It is stored separately as `merge_sha` and never anchors history.

**Columns in the dedup constraint are never nullable.** SQL treats `NULL`s as
distinct, so a nullable column there silently stops rows deduplicating. `position`
uses `""`. This has already broken once.

**A commit is excluded from its own baseline** (`before_sha=`). Otherwise a landed
regression pulls the centre toward itself and shrinks the delta the report exists
to surface.

**Deterministic metrics carry no machine uncertainty.** Allocation counts do not
depend on CPU speed. Check `deterministic` before applying instability, drift, or
scatter.

**Repetition spread is not divided by √n.** Repetitions within one job share a
machine state; treating them as independent understates the bar.

**The report classifies nothing.** No verdicts, no "regression", nothing readable
as a gate. `stats.compare()` exists and is tested but is deliberately not wired
into `reporting.py`. Leave it that way unless asked.

## Reference bracketing

A short workload runs before *and* after the payload, submitted with
`role: "reference"` and `position: "before"|"after"`. It is **never** used to
normalise the payload — only to quantify variation. Two signals:

- **Instability** `|after − before| / level` — machine movement *during* the
  measurement. Needs no history, so it works from the first submission.
- **Drift** — bracket level against the reference series' own sliding window.
  Catches hardware changing underneath you.

`role` participates in series identity; `position` deliberately does not — the two
halves are two samples of one series. Both halves are required or no bracket forms.

## Testing

Tests use FastAPI dependency overrides (`api.session`, `api.config`, `api.github`,
`api.attestor`) — see `tests/conftest.py`. No network, no real GitHub.

Prefer tests that assert a *property* over ones that pin output strings. The
valuable ones here are negative: what must be refused, what must not reach a
baseline.

## Conventions

- Python 3.13, `uv` for everything. Typer CLI uses the `Annotated` pattern.
- All DB access through SQLAlchemy — no raw SQL, no SQLite-only functions, so the
  PostgreSQL escape hatch stays real.
- **No numpy/pandas/polars in the serving path.** Windows are ~250 floats, where
  stdlib beats numpy. If reads get slow the fix is pushing aggregation into SQL or
  materialising per-run aggregates, not swapping array libraries. A heavy
  dependency in `tools/` for offline calibration is fine.
- Comments explain *why*, especially where a choice looks arbitrary but is
  load-bearing.

## Schema changes

Migrations are Alembic, shipped inside the package so `deltaflow migrate` works
from an installed wheel. `render_as_batch` is on because SQLite cannot ALTER
most things in place.

After changing `models.py`:

```console
$ uv run alembic revision --autogenerate -m "what changed"
$ uv run deltaflow migrate
```

`tests/test_migrations.py` fails if migrations and models ever disagree, which
is the failure mode worth catching — a drifted migration breaks at query time,
far from the cause. `create_all()` still exists for tests and the simulator,
which build a database from nothing and discard it.

## Known gaps

- `UNSTABLE_PCT` (5%) and `DRIFT_PCT` (10%) in `reporting.py` are round numbers,
  not derived. They want calibrating against real runner noise.
- Nothing has run against a live GitHub App yet. `tests/fake_github.py` covers
  the protocol; `deltaflow github check` is the first thing to run against real
  credentials.
- Per-IP rate limiting is defence in depth only. A reverse proxy should be the
  primary control — see the note at the top of `ratelimit.py`.
