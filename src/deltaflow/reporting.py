"""Turning stored measurements into the pull request comment."""

from __future__ import annotations

import dataclasses

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Lease, Measurement
from .queries import baseline_points, head_series
from .stats import METHOD, Comparison, compare


@dataclasses.dataclass
class Progress:
    """How many benchmark jobs have claimed a slot versus actually reported.

    This is the payoff from the lease table beyond spoofing defence: it gives
    partial-completion display without a webhook receiver, so the comment can
    say what is still outstanding instead of silently looking finished.
    """

    claimed: int
    reported: int

    @property
    def complete(self) -> bool:
        return self.claimed == 0 or self.reported >= self.claimed


def job_progress(session: Session, repo: str, pr: int, head_sha: str) -> Progress:
    leases = session.scalars(
        select(Lease).where(
            Lease.repo == repo, Lease.pr == pr, Lease.head_sha == head_sha
        )
    ).all()
    reported = session.scalars(
        select(Measurement.job)
        .where(Measurement.repo == repo, Measurement.head_sha == head_sha)
        .distinct()
    ).all()
    return Progress(claimed=len(leases), reported=len({j for j in reported if j}))


def build(session: Session, repo: str, head_sha: str) -> list[Comparison]:
    cfg = settings()
    out: list[Comparison] = []
    for series in head_series(session, repo, head_sha):
        # Exclude the commit under comparison from its own baseline. It matters
        # for mainline pushes, where the measurement being judged has already
        # landed in history and would otherwise pull the centre toward itself,
        # shrinking exactly the delta the report exists to surface.
        baseline = baseline_points(
            session, series.series, cfg.baseline_window, before_sha=head_sha
        )
        out.append(
            compare(
                metric=series.metric,
                labels=series.labels,
                head_reps=series.reps,
                baseline_points=baseline,
                direction=series.direction,
            )
        )
    out.sort(key=lambda c: (not c.notable, c.metric, sorted(c.labels.items())))
    return out


def _fmt_delta(c: Comparison) -> str:
    if c.delta_pct is None:
        return "–"
    return f"{c.delta_pct:+.2f}%"


def _label_str(labels: dict[str, str]) -> str:
    skip = {"metric"}
    parts = [f"{k}={v}" for k, v in sorted(labels.items()) if k not in skip]
    return ", ".join(parts) or "–"


def render(
    comparisons: list[Comparison],
    head_sha: str,
    progress: Progress | None = None,
) -> str:
    cfg = settings()
    notable = [c for c in comparisons if c.notable]
    quiet = [c for c in comparisons if not c.notable]

    lines: list[str] = ["### Benchmark report", ""]

    if not comparisons:
        lines.append("No measurements submitted for this commit yet.")
        return "\n".join(lines)

    if notable:
        regressions = sum(1 for c in notable if c.verdict == "regressed")
        improvements = len(notable) - regressions
        summary = []
        if regressions:
            summary.append(f"**{regressions} regressed**")
        if improvements:
            summary.append(f"{improvements} improved")
        lines += [
            f"{' · '.join(summary)} out of {len(comparisons)} measurements.",
            "",
            "| Metric | Labels | Change | Noise band | Baseline n |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in notable:
            icon = "🔴" if c.verdict == "regressed" else "🟢"
            band = f"±{c.threshold_pct:.2f}%" if c.threshold_pct else "–"
            lines.append(
                f"| {icon} `{c.metric}` | {_label_str(c.labels)} | "
                f"{_fmt_delta(c)} | {band} | {c.n_baseline} |"
            )
    else:
        lines.append(
            f"No changes beyond the noise band across {len(comparisons)} measurements."
        )

    if quiet:
        lines += [
            "",
            f"<details><summary>{len(quiet)} unchanged or unbaselined</summary>",
            "",
            "| Metric | Labels | Change | Verdict | Baseline n |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in quiet:
            lines.append(
                f"| `{c.metric}` | {_label_str(c.labels)} | {_fmt_delta(c)} | "
                f"{c.verdict} | {c.n_baseline} |"
            )
        lines += ["", "</details>"]

    if progress is not None and not progress.complete:
        lines += [
            "",
            f"⏳ {progress.reported} of {progress.claimed} benchmark jobs have "
            "reported so far; this comment updates as the rest arrive.",
        ]

    lines += ["", "---", ""]
    if cfg.grafana_url:
        lines.append(f"[Dashboard]({cfg.grafana_url}) · ")
    lines.append(
        f"`{head_sha[:12]}` · method `{METHOD}` · "
        f"baseline: last {cfg.baseline_window} runs on `{cfg.default_branch}`"
    )
    lines += [
        "",
        "> This report is informational and does not gate merging. Runtime "
        "measurements on shared CI runners are noisy; treat a single flagged "
        "result as a prompt to look, not as proof.",
    ]
    return "\n".join(lines)
