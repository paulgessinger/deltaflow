"""GitHub App client.

Used for two things: posting the pull request comment, and -- on the fork path
-- asking GitHub whether a claimed benchmark job is genuinely running. Ingest
itself needs no credential; reporting and fork verification unavoidably do.

The comment is *upserted*. That is what lets the design drop webhook-based
completion detection entirely: results arrive from any number of independent
workflows, and each submission rewrites the comment with everything known so
far. There is no moment that must be recognised as "done".
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import time

import httpx
import jwt

API = "https://api.github.com"
MARKER = "<!-- deltaflow:report -->"

log = logging.getLogger("deltaflow.github")

# Retried once the server has told us to back off, or when it has failed in a
# way that is plausibly transient. Never on 4xx other than 403/429, since a
# malformed request will stay malformed.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4


def normalise_private_key(key: str) -> str:
    """Accept a PEM however the environment mangled it.

    Private keys passed through environment variables, CI secrets, or docker
    compose files routinely arrive with literal backslash-n instead of real
    newlines. PyJWT rejects those with an unhelpful error a long way from the
    cause.
    """
    key = key.strip().strip('"').strip("'")
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    return key


class GitHubError(Exception):
    """A GitHub call failed after exhausting retries."""


class GitHubClient:
    """Authenticated HTTP against the GitHub API, with backoff."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        api_base: str = API,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = normalise_private_key(private_key)
        self.installation_id = installation_id
        self.api = api_base
        self._token: str | None = None
        self._expires: float = 0.0
        # One client, so connections are reused rather than reopened per call.
        self._http = httpx.Client(timeout=15, transport=transport)

    # --- authentication --------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        # Backdated to tolerate clock skew; GitHub rejects anything over 10
        # minutes in the future, so 9 is the practical ceiling.
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    def installation_token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        # Retried like any other call, since a transient 5xx here would
        # otherwise fail a report that was about to succeed. A 401 or 404 is a
        # configuration error and is never retried -- it will not fix itself.
        resp = None
        for attempt in range(MAX_ATTEMPTS):
            resp = self._http.post(
                f"{self.api}/app/installations/{self.installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                },
            )
            if resp.status_code < 400 or resp.status_code not in RETRY_STATUSES:
                break
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(self._retry_delay(resp, attempt))

        assert resp is not None
        if resp.status_code >= 400:
            raise GitHubError(
                f"could not mint an installation token [{resp.status_code}]: "
                "check the app id, installation id, and private key"
            )
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

    # --- requests --------------------------------------------------------

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call the API, retrying transient failures and honouring Retry-After.

        GitHub signals secondary rate limits with a 403 and a Retry-After
        header rather than a 429, so both are treated the same way.
        """
        url = path if path.startswith("http") else f"{self.api}{path}"
        last: httpx.Response | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._http.request(
                    method, url, headers=self._headers(), **kwargs
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise GitHubError(f"{method} {path} failed: {exc}") from exc
                time.sleep(self._backoff(attempt))
                continue

            if resp.status_code not in RETRY_STATUSES:
                self._note_budget(resp)
                return resp

            last = resp
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(self._retry_delay(resp, attempt))

        assert last is not None
        raise GitHubError(
            f"{method} {path} failed after {MAX_ATTEMPTS} attempts "
            f"[{last.status_code}]"
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        # Jittered, so several jobs finishing together do not retry in lockstep.
        return min(2**attempt, 8) * (0.5 + random.random() / 2)

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        # A primary rate limit reports when the window resets rather than a
        # delay; without this the retries are simply wasted.
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(0.0, min(int(reset) - time.time(), 60.0))
        return self._backoff(attempt)

    @staticmethod
    def _note_budget(resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining and remaining.isdigit() and int(remaining) < 100:
            log.warning("GitHub API budget low: %s calls remaining", remaining)

    def get(self, path: str, **params) -> dict:
        resp = self.request("GET", path, params=params or None)
        if resp.status_code == 404:
            raise GitHubError(f"not found: {path}")
        resp.raise_for_status()
        return resp.json()

    def paginate(self, path: str, **params) -> list[dict]:
        """Follow Link headers to the end.

        GitHub caps a page at 100 items, and the naive single-page version of
        this was a real bug: on a pull request with a long discussion the
        report comment fell off the first page, was never found, and a fresh
        duplicate was posted on every submission.
        """
        out: list[dict] = []
        url: str | None = path
        merged = {"per_page": 100, **params}

        while url:
            resp = self.request("GET", url, params=merged if out == [] else None)
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list):
                break
            out.extend(page)
            url = resp.links.get("next", {}).get("url")

        return out

    def close(self) -> None:
        self._http.close()


class GitHubApp(GitHubClient):
    def upsert_comment(
        self, repo: str, pr: int, body: str, comment_id: int | None = None
    ) -> int:
        """Create the report comment, or edit the existing one in place.

        A known `comment_id` is used directly, which is both far cheaper than
        scanning and immune to the pagination problem. Scanning is the fallback
        for a comment posted before this record existed, or one edited away.
        """
        marked = f"{MARKER}\n{body}"

        if comment_id is not None:
            resp = self.request(
                "PATCH",
                f"/repos/{repo}/issues/comments/{comment_id}",
                json={"body": marked},
            )
            if resp.status_code < 400:
                return resp.json()["id"]
            if resp.status_code != 404:
                resp.raise_for_status()
            # 404 means someone deleted it; fall through and make a new one.
            log.info("report comment %s is gone, recreating", comment_id)

        existing = self.find_comment(repo, pr)
        if existing is not None:
            resp = self.request(
                "PATCH",
                f"/repos/{repo}/issues/comments/{existing}",
                json={"body": marked},
            )
        else:
            resp = self.request(
                "POST", f"/repos/{repo}/issues/{pr}/comments", json={"body": marked}
            )
        resp.raise_for_status()
        return resp.json()["id"]

    def find_comment(self, repo: str, pr: int) -> int | None:
        comments = self.paginate(f"/repos/{repo}/issues/{pr}/comments")
        for comment in comments:
            if MARKER in (comment.get("body") or ""):
                return comment["id"]
        return None

    def check(self) -> dict:
        """Verify credentials and permissions. Used by `deltaflow github check`."""
        app = self.request("GET", "/app")
        app.raise_for_status()
        token = self.installation_token()
        return {
            "app": app.json().get("slug") or app.json().get("name"),
            "installation": self.installation_id,
            "token_acquired": bool(token),
        }


class NullGitHubApp:
    """Records what would have been posted, and posts nothing.

    Lets the service run against a real repository -- real OIDC, real
    attestation, real measurements -- without writing to anyone's pull request.
    Set DELTAFLOW_GITHUB_DRY_RUN=true.
    """

    def __init__(self) -> None:
        self.posted: list[tuple[str, int, str]] = []

    def upsert_comment(
        self, repo: str, pr: int, body: str, comment_id: int | None = None
    ) -> int:
        self.posted.append((repo, pr, body))
        log.info("dry run: would post %d chars to %s#%s", len(body), repo, pr)
        return comment_id or 0

    def installation_token(self) -> str:
        raise GitHubError("dry run: no GitHub credentials configured")
