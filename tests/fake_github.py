"""An in-process stand-in for the GitHub REST API.

Deliberately an httpx transport rather than a set of method stubs, so the code
under test does real request construction, real header handling, real status
handling, and real Link-header pagination. The only thing not exercised is the
network and GitHub's own behaviour.

Everything short of live credentials can be tested against this.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import re
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

RUN_RE = re.compile(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)$")
JOBS_RE = re.compile(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)/attempts/(\d+)/jobs$")
PULL_RE = re.compile(r"/repos/([^/]+/[^/]+)/pulls/(\d+)$")
ISSUE_COMMENTS_RE = re.compile(r"/repos/([^/]+/[^/]+)/issues/(\d+)/comments$")
COMMENT_RE = re.compile(r"/repos/([^/]+/[^/]+)/issues/comments/(\d+)$")
TOKEN_RE = re.compile(r"/app/installations/([^/]+)/access_tokens$")


def generate_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class FakeGitHub:
    """Mutable fake. Adjust attributes to drive a scenario."""

    def __init__(self) -> None:
        self.comments: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(1000)
        self.token_calls = 0
        self.requests: list[tuple[str, str]] = []

        # Failures injected before serving normally, as (status, headers).
        # These apply to API calls only; token minting has its own knob so a
        # test can exercise one without disturbing the other.
        self.fail_next: list[tuple[int, dict[str, str]]] = []
        self.fail_token_next: list[tuple[int, dict[str, str]]] = []
        # When set, runs/jobs/pulls all report 404.
        self.missing = False

        self.run = {
            "status": "in_progress",
            "run_attempt": 1,
            "head_sha": "c" * 40,
            "head_repository": {"full_name": "contributor/acts"},
        }
        self.jobs = {"jobs": [{"name": "bench", "status": "in_progress"}]}
        self.pull = {
            "state": "open",
            "head": {"sha": "c" * 40, "repo": {"full_name": "contributor/acts"}},
        }

    # --- helpers for tests ------------------------------------------------

    def add_comment(self, pr: int, body: str, author: str = "someone") -> int:
        cid = next(self._ids)
        self.comments[cid] = {"id": cid, "body": body, "pr": pr, "user": author}
        return cid

    def comment_bodies(self, pr: int) -> list[str]:
        return [c["body"] for c in self.comments.values() if c["pr"] == pr]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- the fake ---------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))

        if TOKEN_RE.search(path) and self.fail_token_next:
            status, headers = self.fail_token_next.pop(0)
            return httpx.Response(status, headers=headers, json={"message": "nope"})

        if not TOKEN_RE.search(path) and self.fail_next:
            status, headers = self.fail_next.pop(0)
            return httpx.Response(status, headers=headers, json={"message": "nope"})

        if m := TOKEN_RE.search(path):
            self.token_calls += 1
            return httpx.Response(
                201,
                json={
                    "token": f"ghs_fake_{m.group(1)}",
                    "expires_at": (
                        dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
                    ).isoformat(),
                },
            )

        if path == "/app":
            return httpx.Response(200, json={"slug": "deltaflow-test"})

        if self.missing and (
            JOBS_RE.search(path) or RUN_RE.search(path) or PULL_RE.search(path)
        ):
            return httpx.Response(404, json={"message": "Not Found"})

        if JOBS_RE.search(path):
            return httpx.Response(200, json=self.jobs)
        if RUN_RE.search(path):
            return httpx.Response(200, json=self.run)
        if PULL_RE.search(path):
            return httpx.Response(200, json=self.pull)

        if m := ISSUE_COMMENTS_RE.search(path):
            pr = int(m.group(2))
            if request.method == "POST":
                body = json.loads(request.content)["body"]
                cid = self.add_comment(pr, body, author="deltaflow")
                return httpx.Response(201, json={"id": cid})
            return self._list_comments(request, pr)

        if m := COMMENT_RE.search(path):
            cid = int(m.group(2))
            if cid not in self.comments:
                return httpx.Response(404, json={"message": "Not Found"})
            if request.method == "PATCH":
                self.comments[cid]["body"] = json.loads(request.content)["body"]
            return httpx.Response(200, json={"id": cid})

        return httpx.Response(404, json={"message": f"unhandled: {path}"})

    def _list_comments(self, request: httpx.Request, pr: int) -> httpx.Response:
        """Paginate exactly as GitHub does, Link header and all."""
        per_page = int(request.url.params.get("per_page", 30))
        page = int(request.url.params.get("page", 1))

        rows = [c for c in self.comments.values() if c["pr"] == pr]
        start = (page - 1) * per_page
        chunk = rows[start : start + per_page]

        headers = {}
        if start + per_page < len(rows):
            nxt = request.url.copy_set_param("page", page + 1)
            headers["Link"] = f'<{nxt}>; rel="next"'

        return httpx.Response(
            200,
            headers=headers,
            json=[{"id": c["id"], "body": c["body"]} for c in chunk],
        )
