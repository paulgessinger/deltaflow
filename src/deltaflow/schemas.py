"""Wire format for benchmark submission.

Submitters send raw repetitions. They do *not* send a mean, because the spread
within a job is the cheapest honest uncertainty estimate available and averaging
it away at the client throws it out irrecoverably.

Fields that identity depends on -- repository, run id, pull request number --
are deliberately absent: they come from the OIDC claims, never from the body.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from .models import Direction, Position, Role

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class MeasurementIn(BaseModel):
    metric: Annotated[str, Field(min_length=1, max_length=255)]
    values: Annotated[list[float], Field(min_length=1, max_length=1000)]
    unit: Annotated[str, Field(max_length=32)] = ""
    direction: Direction = Direction.LOWER_BETTER
    labels: dict[str, str] = Field(default_factory=dict)

    # Set for metrics that do not depend on how fast the machine is running --
    # allocation counts, object counts, instruction counts. Machine variation
    # is then excluded from their uncertainty, because it does not apply: a
    # slow runner does not change how many bytes the code allocates.
    deterministic: bool = False

    # A reference is a short fixed workload submitted twice, before and after
    # the payload, to quantify how much the machine moved during measurement.
    role: Role = Role.PAYLOAD
    position: Position | None = None
    # Which payload this brackets. Defaults to the job.
    group: Annotated[str, Field(max_length=255)] = ""

    @field_validator("values")
    @classmethod
    def finite(cls, v: list[float]) -> list[float]:
        import math

        if not all(math.isfinite(x) for x in v):
            raise ValueError("values must be finite")
        return v

    @field_validator("labels")
    @classmethod
    def bounded_labels(cls, v: dict[str, str]) -> dict[str, str]:
        # Unbounded label cardinality is how a metrics store dies. A fork PR
        # could otherwise mint a fresh series per push and never be compared.
        if len(v) > 16:
            raise ValueError("at most 16 labels")
        return v


class RunIn(BaseModel):
    """Client-asserted context that OIDC cannot supply.

    `head_sha` matters: the token's `sha` claim on a pull_request event is the
    throwaway merge commit, so it cannot anchor history.
    """

    head_sha: Sha
    base_sha: Sha | None = None
    runner: Annotated[str, Field(max_length=255)] = ""
    job: Annotated[str, Field(max_length=255)] = ""


class SubmissionIn(BaseModel):
    run: RunIn
    measurements: Annotated[list[MeasurementIn], Field(min_length=1, max_length=5000)]


class SubmissionOut(BaseModel):
    accepted: int
    duplicates: int
    series: int
    context: str
    trust: str


class ClaimIn(BaseModel):
    """A fork job announcing itself before it has anything to report.

    Every field is checked against GitHub before the slot is granted, so none
    of it is taken on faith.
    """

    run_id: Annotated[str, Field(pattern=r"^\d{1,32}$")]
    run_attempt: Annotated[int, Field(ge=1, le=100)] = 1
    job: Annotated[str, Field(min_length=1, max_length=255)]
    pr: Annotated[int, Field(ge=1)]
    head_sha: Sha


class ClaimOut(BaseModel):
    secret: str
    expires_at: str
    pr: int
    head_sha: str
