"""Turning stored measurements into the pull request comment.

The report is **descriptive**. It states what was measured, where that sits
relative to recent history, and how much the machine moved while measuring --
and then stops. It does not label anything a regression, and it never gates a
merge. The reader draws the conclusion.

The verdict machinery in `stats.py` is retained and tested but deliberately not
wired in here; it exists for when automated detection is wanted later, as
information rather than as a gate.
"""

from __future__ import annotations

import dataclasses
import statistics

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Lease, Measurement, Role
from .queries import (
    Bracket,
    baseline_points,
    brackets,
    head_series,
    machine_scatter,
)
from .stats import METHOD, Uncertainty, aggregate, estimate

# Above this much machine movement across a payload, the measurement is worth
# distrusting outright. A round number pending real data, not a derived figure.
UNSTABLE_PCT = 5.0

# How far the reference may sit from its own recent norm before the machine
# itself is the more likely explanation. Also a placeholder pending real data.
DRIFT_PCT = 10.0


@dataclasses.dataclass
class Line:
    """One measured series, described rather than judged."""

    metric: str
    unit: str
    labels: dict[str, str]
    value: float
    baseline: float | None
    delta_pct: float | None
    spread_pct: float | None
    n_baseline: int
    job: str
    # How much the machine moved *during* this measurement. Needs no history.
    instability_pct: float | None
    # How far the machine sits from its own recent norm. A sustained step here
    # is the machine changing -- a kernel or hypervisor update, a different
    # runner generation on GitHub's side -- not the repository.
    drift_pct: float | None = None
    n_drift: int = 0
    # One-sigma bar on `value`, combining repetition spread, instability and
    # machine scatter. This is the same quantity a dashboard draws as a band.
    uncertainty: Uncertainty | None = None


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


def build(session: Session, repo: str, head_sha: str) -> list[Line]:
    cfg = settings()
    machine = brackets(session, repo, head_sha)
    lines: list[Line] = []

    for s in head_series(session, repo, head_sha):
        # Exclude the commit under comparison from its own baseline. It matters
        # for mainline pushes, where the measurement being judged has already
        # landed in history and would otherwise pull the centre toward itself.
        history = baseline_points(
            session, s.series, cfg.baseline_window, before_sha=head_sha
        )
        value = aggregate(s.reps)

        centre = statistics.median(history) if history else None
        delta = None
        if centre:
            delta = (value - centre) / abs(centre) * 100.0

        # Historical spread, stated as a range rather than as a threshold --
        # context for the reader, not a line anything is judged against.
        spread = None
        if len(history) >= 4 and centre:
            lo, hi = min(history), max(history)
            spread = max(abs(hi - centre), abs(centre - lo)) / abs(centre) * 100.0

        bracket: Bracket | None = machine.get((s.job, s.group))

        # The reference's own history. A per-run aggregate of a reference
        # series is its bracket level, so this needs no special accounting.
        drift = None
        ref_history: list[float] = []
        scatter = 0.0
        if bracket is not None and bracket.series:
            ref_history = baseline_points(
                session,
                bracket.series,
                cfg.baseline_window,
                before_sha=head_sha,
                role=Role.REFERENCE,
            )
            if ref_history:
                norm = statistics.median(ref_history)
                if norm:
                    drift = (bracket.level - norm) / abs(norm) * 100.0
                scatter = machine_scatter(ref_history)

        # A deterministic metric is unaffected by how fast the machine ran, so
        # only its own repetition spread contributes -- which is normally zero.
        uncertainty = estimate(
            s.reps,
            instability_pct=(
                None if s.deterministic else (bracket.instability if bracket else None)
            ),
            machine_scatter=0.0 if s.deterministic else scatter,
            drift_pct=None if s.deterministic else drift,
        )

        lines.append(
            Line(
                metric=s.metric,
                unit=s.unit,
                labels=s.labels,
                value=value,
                baseline=centre,
                delta_pct=delta,
                spread_pct=spread,
                n_baseline=len(history),
                job=s.job,
                instability_pct=bracket.instability if bracket else None,
                drift_pct=drift,
                n_drift=len(ref_history),
                uncertainty=uncertainty,
            )
        )

    lines.sort(key=lambda ln: (ln.metric, sorted(ln.labels.items())))
    return lines


def _labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) or "–"


def _num(value: float, unit: str) -> str:
    formatted = f"{value:,.4g}"
    return f"{formatted} {unit}".strip()


def _measured(line: Line) -> str:
    """Value with its one-sigma bar, in the metric's own units."""
    base = _num(line.value, line.unit)
    unc = line.uncertainty
    if unc is None or not unc.known:
        return base
    # Match the bar's precision to its own magnitude rather than the value's,
    # so a 0.004 s bar does not render as "0 s".
    return f"{base} ± {unc.absolute:,.3g} {line.unit}".strip()


def render(lines: list[Line], head_sha: str, progress: Progress | None = None) -> str:
    cfg = settings()
    out: list[str] = ["### Benchmark report", ""]

    if not lines:
        out.append("No measurements submitted for this commit yet.")
        return "\n".join(out)

    out += [
        "| Metric | Labels | This commit | vs. recent `"
        + cfg.default_branch
        + "` | Recent range | n |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for ln in lines:
        delta = f"{ln.delta_pct:+.2f}%" if ln.delta_pct is not None else "–"
        baseline = _num(ln.baseline, ln.unit) if ln.baseline is not None else "–"
        spread = f"±{ln.spread_pct:.2f}%" if ln.spread_pct is not None else "–"
        out.append(
            f"| `{ln.metric}` | {_labels(ln.labels)} | {_measured(ln)} | "
            f"{delta} | {baseline} {spread} | {ln.n_baseline} |"
        )

    measured = [ln for ln in lines if ln.instability_pct is not None]
    if measured:
        out += ["", "**Machine behaviour**", ""]
        seen: set[str] = set()
        rows: list[str] = [
            "| Job | Instability during run | vs. machine's recent norm | n |",
            "| --- | --- | --- | --- |",
        ]
        for ln in measured:
            if ln.job in seen:
                continue
            seen.add(ln.job)
            unstable = " ⚠️" if ln.instability_pct >= UNSTABLE_PCT else ""
            if ln.drift_pct is None:
                drift = "–"
            else:
                drift = f"{ln.drift_pct:+.2f}%"
                if abs(ln.drift_pct) >= DRIFT_PCT:
                    drift += " ⚠️"
            rows.append(
                f"| `{ln.job}` | ±{ln.instability_pct:.2f}%{unstable} | "
                f"{drift} | {ln.n_drift} |"
            )
        out += rows

        if any(ln.instability_pct >= UNSTABLE_PCT for ln in measured):
            out += [
                "",
                "⚠️ The reference workload moved by more than "
                f"{UNSTABLE_PCT:.0f}% between its runs either side of the "
                "payload, so the machine was not stable while measuring. Treat "
                "changes from that job as unquantified.",
            ]
        if any(
            ln.drift_pct is not None and abs(ln.drift_pct) >= DRIFT_PCT
            for ln in measured
        ):
            out += [
                "",
                "⚠️ The reference workload is running well away from its own "
                "recent norm. If that persists across commits it is the "
                "machine that changed — a hypervisor or kernel update, or a "
                "different runner generation — and comparisons across the step "
                "are not meaningful.",
            ]
    else:
        out += [
            "",
            "_No reference bracket submitted, so machine variation during this "
            "run is unknown._",
        ]

    if progress is not None and not progress.complete:
        out += [
            "",
            f"⏳ {progress.reported} of {progress.claimed} benchmark jobs have "
            "reported so far; this comment updates as the rest arrive.",
        ]

    out += ["", "---", ""]
    trailer = (
        f"`{head_sha[:12]}` · aggregation `{METHOD}` · "
        f"recent range over the last {cfg.baseline_window} runs on "
        f"`{cfg.default_branch}`"
    )
    if cfg.grafana_url:
        trailer = f"[Dashboard]({cfg.grafana_url}) · " + trailer
    out.append(trailer)
    out += [
        "",
        "> Informational only — nothing here blocks merging, and no change is "
        "classified as a regression. The numbers are reported so a human can "
        "judge them.",
    ]
    return "\n".join(out)
