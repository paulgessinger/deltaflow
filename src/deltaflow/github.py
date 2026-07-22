"""GitHub App client, used only for posting the pull request comment.

The comment is *upserted*, identified by a hidden marker. This is what lets the
design drop webhook-based completion detection entirely: results arrive from any
number of independent workflows, and each submission simply rewrites the comment
with everything known so far. There is no moment that must be recognised as
"done", because there is no final state to wait for.
"""

from __future__ import annotations

import datetime as dt
import time

import httpx
import jwt

API = "https://api.github.com"
MARKER = "<!-- deltaflow:report -->"


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, installation_id: str) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self._token: str | None = None
        self._expires: float = 0.0

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    def installation_token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        resp = httpx.post(
            f"{API}/app/installations/{self.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["token"]
        self._expires = dt.datetime.fromisoformat(data["expires_at"]).timestamp()
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def upsert_comment(self, repo: str, pr: int, body: str) -> int:
        """Create the report comment, or edit the existing one in place."""
        marked = f"{MARKER}\n{body}"

        resp = httpx.get(
            f"{API}/repos/{repo}/issues/{pr}/comments",
            headers=self._headers(),
            params={"per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        existing = next(
            (c for c in resp.json() if MARKER in (c.get("body") or "")), None
        )

        if existing:
            resp = httpx.patch(
                f"{API}/repos/{repo}/issues/comments/{existing['id']}",
                headers=self._headers(),
                json={"body": marked},
                timeout=15,
            )
        else:
            resp = httpx.post(
                f"{API}/repos/{repo}/issues/{pr}/comments",
                headers=self._headers(),
                json={"body": marked},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()["id"]
