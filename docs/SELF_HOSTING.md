# Self-hosting Assay

Everything the hosted service runs is in this repository. The server is a single Python process with SQLite and a
folder per analysis; it needs no browsers (those run in CI).

### 1. Start the server

```bash
git clone https://github.com/RazinAleksandr/assay && cd assay
docker build -t assay-server -f deploy/Dockerfile.server .
docker run -d --name assay-server --restart unless-stopped -p 8080:8080 -v assay-data:/data \
  assay-server --public-url https://assay.example.com --config /data/server.yaml
```

Or without Docker: `uvx --from git+https://github.com/RazinAleksandr/assay@v0 assay server --data ./assay-data
--host 0.0.0.0 --port 8080 --public-url https://assay.example.com`.

On first start the server writes an admin token to `<data>/token` (or takes `ASSAY_SERVER_TOKEN`). The token signs
you in at `/login` and lets CI systems other than GitHub upload.

`--public-url` is the address CI and browsers use. It turns on the GitHub Actions sign-in: a job asks GitHub for a
token whose audience is the action's `host-url`, so the two must be the same string.

### 2. Point CI at it

GitHub Actions, no secrets needed:

```yaml
- uses: RazinAleksandr/assay@v0
  with:
    host-url: https://assay.example.com
```

With the Claude Code plugin, tell Claude the server URL when you run `/assay:connect`, or add `host-url` to the
workflow it writes.

GitLab CI and anything else: give the job `ASSAY_HOST_URL` and `ASSAY_TOKEN` (the admin token) and run `assay ci`.
For GitLab there is a ready [template](../ci/assay.gitlab-ci.yml) built on the scanner image from the root
[`Dockerfile`](../Dockerfile) (Playwright browsers, Node 22, `assay`):

```bash
docker build -t assay .
docker run --rm -v "$PWD:/src" -w /src -e ASSAY_HOST_URL -e ASSAY_TOKEN assay ci
```

### 3. Configure it (optional): `server.yaml`

Keep it in the data folder, never in git.

```yaml
admin: {user: admin, password: "<hash>"}       # `assay hash-password` prints the hash; sign in at /login
admins: [you@example.com]                      # verified emails that see every project

auth:                                          # people sign up with email and password, Google or GitHub
  google: {client_id: "….apps.googleusercontent.com", client_secret: "…"}   # redirect URI: <public url>/auth/google/callback
  github: {client_id: "…", client_secret: "…"}                              # redirect URI: <public url>/auth/github/callback
  signup: true                                 # false: only Google and GitHub create accounts
smtp: {host: smtp.example.com, port: 587, user: "…", password: "…", from: "Assay <no-reply@example.com>"}

gate: {max_new: {blocker: 0, critical: 0}}     # a gate for every project …
projects: {gh/acme/shop: {gate: {min_rating: {reliability: B}}}}   # … or per project
public: [gh/acme/docs]                         # projects shown to everyone whatever their repository's visibility
trusted_proxy: true                            # behind your reverse proxy: take the client address from X-Forwarded-For
oidc_audiences: [https://assay.internal]       # more addresses CI may sign in to besides --public-url
```

Without `smtp`, accounts work but emails are not confirmed and a forgotten password cannot be reset by a link. Google
accepts OAuth redirects only on https with a domain name.

**Who sees what.** Projects of public repositories are public, like their code. A private repository's project opens
only through the link CI posts in its pull requests (the link carries a view key once; the server keeps it in a
cookie). A signed-in person also sees the projects of their own GitHub account.

### 4. Deploy and update

- `deploy/deploy.sh <ssh-host> <dir> <public-url>` ships the tracked files to a Linux box with Docker over SSH and runs
  `deploy/install.sh` there: build the image, replace the container, wait for `/api/health`. Data stays in `<dir>/data`.
- The hosted service deploys itself the same way: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) runs on
  a self-hosted runner on the server after every green CI run on `main` (no SSH keys in GitHub) and moves the tag `v0`.
  Copy it if you want the same for your fork.
- A status badge for your README: `<server>/api/projects/<key>/badge.svg?branch=main`.

## What leaves your CI

The app runs inside your own CI job. The server receives the run folder: verdicts, the steps of each journey,
screenshots of your app's screens, the data values the checks compared, the issues found, the app's log and, on a pull
request, the change's diff (Assay ranks the suspect hunks from it). The rest of your repository stays in the job. If
that is too much to send to the public instance, run your own server.
