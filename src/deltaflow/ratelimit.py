"""Rate limiting for the uncredentialed claim path.

`/v1/claim` cannot require a credential -- fork pull requests have none to
offer -- so it is the one endpoint the open internet can reach unauthenticated.
Every claim costs two GitHub API calls and a database write, and a successful
one occupies a lease slot, so it needs limiting on all three axes.

Fixed windows rather than a token bucket or sliding log: the counters live in
the database so they survive restarts and work across processes, and a fixed
window is one row and one update. The known cost is burstiness at the boundary
-- a caller can spend a full window's budget at the end of one window and again
at the start of the next. At these limits that is not worth a more elaborate
scheme.

Why not slowapi or limits, which are the obvious off-the-shelf answer:

* `limits` (which slowapi wraps) supports memory, Redis, Memcached, MongoDB and
  Valkey -- there is no SQL backend. Redis means standing up infrastructure for
  one endpoint on a service that runs off a single SQLite file, which is
  directly at odds with the project's first goal. Memory means an attacker's
  budget resets on every deploy.
* Two of the three limits here are keyed on the *request body* -- repository and
  pull request number -- which decorator-based limiters handle awkwardly, since
  their key functions run before the body is parsed.
* The remaining caps in `api.py` (lease slots per run, series per pull request,
  submissions per lease) are domain invariants rather than rate limits, and no
  library covers them.

Per-address limiting is the one axis a library or, better, a reverse proxy
would do well. `nginx limit_req`, Caddy, or Cloudflare enforce it before a
request reaches Python and belong to the layer that actually sees the network.
Treat the IP counter here as defence in depth, not as the primary control.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import RateBucket


@dataclasses.dataclass(frozen=True)
class Limit:
    """A budget of `count` events per `window`."""

    count: int
    window: dt.timedelta

    @property
    def description(self) -> str:
        seconds = int(self.window.total_seconds())
        unit = (
            "minute" if seconds == 60 else "hour" if seconds == 3600 else f"{seconds}s"
        )
        return f"{self.count} per {unit}"


class RateLimited(Exception):
    """Budget exhausted. `retry_after` is seconds until the window rolls."""

    def __init__(self, scope: str, limit: Limit, retry_after: int):
        super().__init__(f"rate limit exceeded for {scope} ({limit.description})")
        self.scope = scope
        self.limit = limit
        self.retry_after = retry_after


def consume(
    session: Session, key: str, limit: Limit, now: dt.datetime | None = None
) -> None:
    """Charge one event against `key`, or raise RateLimited.

    Windows are aligned to wall-clock boundaries rather than to first use, so
    two processes counting the same key agree on which window they are in
    without coordinating.
    """
    now = now or dt.datetime.now(dt.UTC)
    seconds = int(limit.window.total_seconds())
    epoch = int(now.timestamp())
    start = dt.datetime.fromtimestamp(epoch - (epoch % seconds), dt.UTC)

    bucket = session.scalars(
        select(RateBucket).where(RateBucket.key == key)
    ).one_or_none()

    if bucket is None:
        bucket = RateBucket(key=key, window_start=start, count=0)
        session.add(bucket)
        try:
            with session.begin_nested():
                session.flush()
        except IntegrityError:
            # Another process created it between the select and the insert.
            session.rollback()
            bucket = session.scalars(
                select(RateBucket).where(RateBucket.key == key)
            ).one()

    stored = bucket.window_start
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=dt.UTC)

    if stored < start:
        bucket.window_start = start
        bucket.count = 0

    if bucket.count >= limit.count:
        retry_after = max(1, int((start + limit.window - now).total_seconds()))
        raise RateLimited(key.split(":", 1)[0], limit, retry_after)

    bucket.count += 1
    session.flush()


def purge(session: Session, older_than: dt.timedelta = dt.timedelta(days=1)) -> int:
    """Drop buckets whose window closed long ago, so the table stays small."""
    cutoff = dt.datetime.now(dt.UTC) - older_than
    stale = session.scalars(
        select(RateBucket).where(RateBucket.window_start < cutoff)
    ).all()
    for bucket in stale:
        session.delete(bucket)
    return len(stale)
