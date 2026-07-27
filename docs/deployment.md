# Standing it up

What it takes to run deltaflow against a real repository, in the order that
isolates failures. The short version: get a public URL, create the App, bring
the service up in **dry-run** mode, and only then let it write to a pull
request.

The service is stateless apart from its database. There is no webhook receiver,
no queue, and no background worker — the pull request comment is upserted
synchronously on every submission, so a single process is a complete deployment.

## 1. A public HTTPS URL

Non-negotiable, because it is not only how GitHub reaches you: the URL **is**
the OIDC audience. A token minted for one audience cannot be replayed against
another, which is the point, but it also means the value has to be settled
before anything else is configured.

For local testing, any tunnel works:

```console
$ cloudflared tunnel --url http://localhost:8000
$ # or: ngrok http 8000
```

Take the URL it prints. It goes in three places, and they must agree exactly —
no trailing slash, `https` not `http`:

| Where | Why |
| --- | --- |
| `DELTAFLOW_AUDIENCE` | What the server requires in the `aud` claim |
| The workflow's `server:` input | Where results are POSTed |
| The workflow's `audience:` input | What CI asks GitHub to mint — defaults to `server`, so leave it unset |

A mismatch here surfaces as `401 token validation failed`, with nothing saying
which of the two sides is wrong. It is the single most common bring-up failure.

## 2. Create the GitHub App

Settings → Developer settings → **GitHub Apps** → New GitHub App.

- **Homepage URL** — anything; it is not used.
- **Webhook** — *uncheck Active*. Deltaflow subscribes to no events and has no
  receiver. Results arrive from CI, not from GitHub.
- **Where can this GitHub App be installed?** — Only on this account, unless you
  are running a service for others.

### Repository permissions

Only four, and two are read-only:

| Permission | Level | What needs it |
| --- | --- | --- |
| **Issues** | Read & write | Posting and editing the report comment. Pull request comments are issue comments — this is the one that surprises people. |
| **Pull requests** | Read-only | Fork attestation: confirming the PR is open and that the commit is its head. |
| **Actions** | Read-only | Fork attestation: confirming the run *and the named job* are executing right now. |
| **Metadata** | Read-only | Mandatory; GitHub selects it for you. |

No organisation permissions, no account permissions, no event subscriptions.

If you never intend to accept benchmark submissions from fork pull requests,
Issues alone is enough — the lease path simply refuses. Do not grant `contents:
write` or anything that can push; nothing in the code does.

### Collect three values

1. **App ID** — on the App's settings page.
2. **Private key** — *Generate a private key* at the bottom; the `.pem`
   downloads once and cannot be recovered. Convert it for the environment:
   ```console
   $ awk 'BEGIN{ORS="\\n"} {print}' your-app.private-key.pem
   ```
   The client repairs that escaped form on load (`normalise_private_key`),
   because a real multi-line value does not survive an env var or a compose
   file intact.
3. **Installation ID** — install the App on the target repository first
   (*Install App* in the sidebar). The number is the last path segment of the
   URL you land on: `.../settings/installations/<installation_id>`.

## 3. Configure and run

```console
$ cp .env.example .env      # fill in the three App values and the audience
$ docker compose up --build
```

That gives you the API on `:8000` with the source bind-mounted and hot reload
on, and Grafana on `:3000` with the JSON datasource already provisioned. Point
your tunnel at `:8000`.

Two things worth knowing about the local stack:

- If anything already listens on 8000, set `DF_HOST_PORT` in `.env`. Docker
  Desktop does not reliably report the collision — the container comes up
  healthy while the published port resolves to the *other* process, which is a
  genuinely confusing ten minutes.
- Reload works through the bind mount because `WATCHFILES_FORCE_POLLING` is
  set. Bind mounts deliver no inotify events on Docker Desktop's VM, so without
  it the reloader sees nothing and quietly serves stale code.

Grafana is anonymous-Editor and bound to localhost only. It is a viewer here;
every aggregation and uncertainty estimate is computed server-side, so panels
stay thin and improved statistics apply retroactively to all history.

Without Docker:

```console
$ uv sync
$ uv run deltaflow migrate
$ uv run deltaflow serve --host 0.0.0.0
```

Schema migration runs automatically at startup (`DELTAFLOW_AUTO_MIGRATE`). Turn
it off and run `deltaflow migrate` as a deploy step if you will ever run more
than one replica, so they cannot race each other.

## 4. Verify the credentials before trusting them

```console
$ docker compose exec deltaflow deltaflow github check
```

This exists to separate two failures that otherwise both surface as a confusing
404 much later, in the middle of a benchmark run:

```
app:          deltaflow
installation: 12345678
  paulgessinger/deltaflow: reachable
```

`NOT reachable` means the credentials are fine but the App is not installed on
that repository. A failure before any of that means the App ID, installation
ID, or private key is wrong.

Note that `github check` short-circuits while `DELTAFLOW_GITHUB_DRY_RUN=true` —
unset it for the check, then decide whether to put it back.

## 5. First real run, in dry-run mode

Leave `DELTAFLOW_GITHUB_DRY_RUN=true` and open a pull request that runs the
benchmark workflow. Every path executes for real — OIDC verification, ingest,
statistics, report rendering — and the comment is logged rather than posted:

```
dry run: would post 1843 chars to paulgessinger/deltaflow#4
```

The rendered markdown is also stored on the `Report` row, and
`GET /v1/report?repo=…&pr=…` returns it, so you can read exactly what would have
appeared before anyone else does.

One caveat: the fork lease path returns **503** in dry-run mode. It has no
GitHub client to attest against, and accepting unverified claims would be
strictly worse than refusing. Test forks after you flip dry-run off.

When the output looks right, set `DELTAFLOW_GITHUB_DRY_RUN=false` and restart.

## Where the trust boundaries sit

Worth knowing before pointing this at a repository that matters:

- Anything on the **lease** path (fork pull requests) is barred from writing a
  mainline baseline structurally, not by convention. Fork authors can submit
  false numbers about their own pull request; that is accepted, and the blast
  radius is their own comment.
- The **OIDC** path writes a baseline only for `push` events on the default
  branch. A pull request cannot forge `ref`, so it cannot reach that branch.
- The **token** path is mainline-eligible and unconditional. See
  [Scoped tokens](#scoped-tokens) below — issue them narrowly.

## Scoped tokens

For submitters that have no OIDC to offer: a bare-metal machine on a cron, a
lab runner outside GitHub Actions. Likely the *most* trustworthy runtime
numbers you have, which is why this path is mainline-eligible.

```console
$ docker compose exec deltaflow deltaflow mint-token bare-metal-01 --repo owner/repo
```

The secret is printed once and never recoverable — only its SHA-256 is stored.
The submitter sets it as `DELTAFLOW_TOKEN` and `deltaflow submit` prefers it
over everything else.

A token is scoped to exactly one repository and carries no PR context, so every
submission under it lands as mainline history for that repo. Treat one as
equivalent to write access to the baseline: mint one per machine, never share
one between machines, and revoke by setting `revoked` on the row (there is no
CLI for that yet).

## Rate limiting

`ratelimit.py` covers the uncredentialed claim path only, and is defence in
depth. A reverse proxy should be the primary control. If you do put one in
front, set `DELTAFLOW_TRUST_FORWARDED_FOR=true` — but *only* if that proxy
overwrites `X-Forwarded-For`, since trusting the header otherwise lets any
caller forge their address and bypass the per-address limit entirely. A
development tunnel does not qualify.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `401 token validation failed` | Audience mismatch between `DELTAFLOW_AUDIENCE` and the workflow, or a clock more than 60s out. |
| `401 repository not permitted` | The repo is not matched by `DELTAFLOW_ALLOWED_REPOS`. It is JSON: `["owner/*"]`. |
| `503 lease path unavailable` | Dry-run mode, or no App configured. Expected on forks until step 5 is done. |
| `403 run is not currently executing` | The claim arrived after the job finished — or a replay. Claim in the job's *first* step. |
| Comment posted twice | Two `Report` rows, meaning two PR numbers resolved for one pull request. Check `head-sha` is the real head, not the merge commit. |
| No comment, no error | Dry-run still on. |
