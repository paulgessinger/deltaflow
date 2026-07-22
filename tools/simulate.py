#!/usr/bin/env python
"""Local simulation harness. Not shipped, not part of the CLI.

Drives the real API in-process against a real SQLite file, with GitHub stubbed
out, so the whole flow -- ingest, dedup, baselines, bracketing, reporting -- can
be exercised without CI or network.

Its purpose is not to prove anything about the software under test. It is to
generate realistic-looking data so the report can be judged by eye, and to check
that the derived machine quantities behave as intended: that within-run
instability tracks noise injected during a run, and that reference drift tracks
a hardware change injected between runs.

Usage:
    uv run python tools/simulate.py flow
    uv run python tools/simulate.py flow --noise 0.12 --seed 7
    uv run python tools/simulate.py drift --hardware-step 0.25
"""

from __future__ import annotations

import argparse
import hashlib
import math
import pathlib
import random
import statistics
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from deltaflow import api  # noqa: E402
from deltaflow.auth import new_secret  # noqa: E402
from deltaflow.config import Settings  # noqa: E402
from deltaflow.models import ApiToken, Base, Lease  # noqa: E402

REPO = "acts-project/acts"
REF_LABELS = {"benchmark": "reference-fixed", "runner": "ubuntu-latest"}
PAYLOAD_LABELS = {"benchmark": "seeding", "runner": "ubuntu-latest"}

# Ticks the machine's speed evolves through while one payload benchmark runs.
# The reference is sampled at the first and last tick, which is precisely what
# bracketing measures in reality.
TICKS_PER_RUN = 20


class Machine:
    """A machine whose speed wanders, in log space.

    AR(1) rather than white noise because real runners have bad afternoons:
    a noisy neighbour persists across several runs rather than resampling
    independently each time. `step` models a discontinuity -- a hypervisor
    upgrade, or GitHub rolling out a different runner generation.
    """

    def __init__(self, rng: random.Random, noise: float, phi: float = 0.7):
        self.rng = rng
        self.noise = noise
        self.phi = phi
        self.state = 0.0
        self.step = 0.0

    def tick(self) -> float:
        self.state = self.phi * self.state + self.rng.gauss(0, self.noise)
        return math.exp(self.state + self.step)

    def run(self) -> tuple[float, float, float]:
        """One benchmark run: (speed before, mean speed during, speed after)."""
        samples = [self.tick() for _ in range(TICKS_PER_RUN)]
        return samples[0], statistics.fmean(samples), samples[-1]


def measurements(
    payload_truth: float,
    ref_truth: float,
    before: float,
    during: float,
    after: float,
    rng: random.Random,
    reps: int,
    jitter: float,
) -> list[dict]:
    """A job's submission: reference, payload, reference."""

    def sample(true_value: float, speed: float) -> list[float]:
        return [
            round(true_value * speed * math.exp(rng.gauss(0, jitter)), 6)
            for _ in range(reps)
        ]

    return [
        {
            "metric": "runtime",
            "unit": "s",
            "labels": REF_LABELS,
            "role": "reference",
            "position": "before",
            "values": sample(ref_truth, before),
        },
        {
            "metric": "runtime",
            "unit": "s",
            "labels": PAYLOAD_LABELS,
            "values": sample(payload_truth, during),
        },
        {
            "metric": "allocations",
            "unit": "count",
            "labels": PAYLOAD_LABELS,
            # Unaffected by how fast the machine happens to run.
            "deterministic": True,
            "values": [round(42000 * payload_truth / 10.0)],
        },
        {
            "metric": "runtime",
            "unit": "s",
            "labels": REF_LABELS,
            "role": "reference",
            "position": "after",
            "values": sample(ref_truth, after),
        },
    ]


class Harness:
    def __init__(self, db_path: pathlib.Path, window: int = 50):
        engine = create_engine(f"sqlite:///{db_path}", future=True)
        Base.metadata.create_all(engine)
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.comments: list[str] = []

        settings = Settings(
            database_url=f"sqlite:///{db_path}",
            allowed_repos=[REPO],
            default_branch="main",
            baseline_window=window,
        )

        harness = self

        class RecordingGitHub:
            """Stands in for the GitHub App: captures instead of posting."""

            def upsert_comment(self, _repo: str, _pr: int, body: str) -> int:
                harness.comments.append(body)
                return 1

        class AlwaysAttests:
            def attest(self, **_kw) -> None:
                pass

        def _session():
            with self.sessions() as s:
                yield s

        api.app.dependency_overrides[api.session] = _session
        api.app.dependency_overrides[api.config] = lambda: settings
        api.app.dependency_overrides[api.github] = lambda: RecordingGitHub()
        api.app.dependency_overrides[api.attestor] = lambda: AlwaysAttests()
        self.client = TestClient(api.app)

        secret, digest = new_secret()
        with self.sessions() as db:
            db.add(ApiToken(name="sim", repo=REPO, secret_hash=digest))
            db.commit()
        self.token = secret

    def submit_mainline(self, sha: str, body: list[dict], job: str = "bench") -> dict:
        resp = self.client.post(
            "/v1/submit",
            json={"run": {"head_sha": sha, "job": job}, "measurements": body},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        resp.raise_for_status()
        return resp.json()

    def submit_pr(
        self, sha: str, pr: int, body: list[dict], job: str = "bench", run_id: str = "9"
    ) -> dict:
        """Go through the real lease path, as a fork pull request would."""
        claim = self.client.post(
            "/v1/claim",
            params={"repo": REPO},
            json={
                "run_id": run_id,
                "run_attempt": 1,
                "job": job,
                "pr": pr,
                "head_sha": sha,
            },
        )
        claim.raise_for_status()
        resp = self.client.post(
            "/v1/submit",
            json={"run": {"head_sha": sha, "job": job}, "measurements": body},
            headers={"Authorization": f"Bearer {claim.json()['secret']}"},
        )
        resp.raise_for_status()
        return resp.json()

    def report(self, sha: str, pr: int | None = None) -> str:
        params = {"repo": REPO, "head_sha": sha}
        if pr is not None:
            params["pr"] = str(pr)
        resp = self.client.get("/v1/report", params=params)
        resp.raise_for_status()
        return resp.json()["markdown"]


def sha_for(i: int) -> str:
    return hashlib.sha1(f"commit-{i}".encode()).hexdigest()


def build_history(
    harness: Harness,
    machine: Machine,
    rng: random.Random,
    commits: int,
    payload_truth: float,
    reps: int,
    jitter: float,
    hardware_step_at: int | None = None,
    hardware_step: float = 0.0,
) -> None:
    for i in range(commits):
        if hardware_step_at is not None and i == hardware_step_at:
            machine.step = math.log(1 + hardware_step)
        before, during, after = machine.run()
        harness.submit_mainline(
            sha_for(i),
            measurements(
                payload_truth, 1.0, before, during, after, rng, reps, jitter
            ),
        )


def cmd_flow(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    machine = Machine(rng, noise=args.noise)
    db = pathlib.Path(args.db or tempfile.mkdtemp()) / "sim.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    harness = Harness(db)

    build_history(
        harness, machine, rng, args.commits, args.payload, args.reps, args.jitter
    )

    # A pull request whose payload genuinely changed by --effect.
    pr_sha = sha_for(9999)
    before, during, after = machine.run()
    harness.submit_pr(
        pr_sha,
        args.pr,
        measurements(
            args.payload * (1 + args.effect),
            1.0,
            before,
            during,
            after,
            rng,
            args.reps,
            args.jitter,
        ),
    )

    from deltaflow.models import series_key
    key = series_key(REPO, "runtime", PAYLOAD_LABELS)
    ts = harness.client.get(
        "/v1/timeseries", params={"repo": REPO, "series": key}
    ).json()
    print(f"timeseries points: {len(ts['points'])}  "
          f"machine_scatter={ts['machine_scatter']:.4f}")
    for pt in ts["points"][-3:]:
        print(f"  {pt['head_sha'][:8]}  {pt['value']:.3f} +- {pt['sigma']:.3f}  "
              f"[{pt['lower']:.3f}, {pt['upper']:.3f}]")
    print()
    print(f"database: {db}")
    print(f"injected payload change: {args.effect:+.1%}")
    print(f"machine noise: {args.noise}  seed: {args.seed}")
    print()
    print(harness.comments[-1] if harness.comments else harness.report(pr_sha, args.pr))


def cmd_drift(args: argparse.Namespace) -> None:
    """Does reference drift notice the machine changing under an unchanged repo?

    The payload truth is held perfectly constant throughout. Everything the
    report shows as a change is therefore an artefact of the machine, which is
    exactly the confusion the reference exists to expose.
    """
    rng = random.Random(args.seed)
    machine = Machine(rng, noise=args.noise)
    db = pathlib.Path(tempfile.mkdtemp()) / "sim.db"
    harness = Harness(db)

    build_history(harness, machine, rng, args.commits, args.payload, args.reps,
                  args.jitter)

    machine.step = math.log(1 + args.hardware_step)
    sha = sha_for(9999)
    before, during, after = machine.run()
    harness.submit_mainline(
        sha,
        measurements(args.payload, 1.0, before, during, after, rng, args.reps,
                     args.jitter),
    )

    print(f"injected hardware step: {args.hardware_step:+.1%}")
    print("payload truth: unchanged throughout")
    print()
    print(harness.report(sha))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--commits", type=int, default=40)
    parser.add_argument("--noise", type=float, default=0.03,
                        help="Machine speed noise, log-space sigma per tick.")
    parser.add_argument("--jitter", type=float, default=0.01,
                        help="Per-repetition measurement jitter.")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--payload", type=float, default=10.0)

    sub = parser.add_subparsers(dest="command", required=True)

    flow = sub.add_parser("flow", help="Mainline history, then a pull request.")
    flow.add_argument("--effect", type=float, default=0.15,
                      help="True payload change on the PR, e.g. 0.15 for +15%%.")
    flow.add_argument("--pr", type=int, default=4021)
    flow.add_argument("--db", default=None, help="Directory to keep the database in.")
    flow.set_defaults(func=cmd_flow)

    drift = sub.add_parser("drift", help="Hardware change under an unchanged repo.")
    drift.add_argument("--hardware-step", type=float, default=0.2)
    drift.set_defaults(func=cmd_drift)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
