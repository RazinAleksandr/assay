# Command line

```bash
assay init [--ci github|gitlab]          # add .assay/ and the CI step to an app repository
assay browsers [--with-deps] [chromium webkit]   # download the browsers the scanner drives (once per machine)
assay doctor .assay                      # preflight: start command, accounts, probes, judge, browsers
assay run .assay [--scan] [--confirm] [-k 'post.*' | -k tag:smoke | -k group:feed | -k is:critical]   # same gate as CI
assay scan --url http://localhost:3000   # no config at all: crawl any URL and scan every screen
assay grade .assay .assay/runs/<id>      # re-grade recorded evidence without re-running the app
assay brief [<analysis URL> | .assay/runs/<id>] [--case ID --full]   # what an agent needs to fix the gate (JSON)
assay mcp                                # the same for agents over MCP (stdio), plus the explore browser
assay bench .assay [--attempts 3] [--arm name=env:K=V | name=<folder> | name=<url>]   # repeat and compare (default: bench: in assay.yaml)
assay ci                                 # the CI step: run, compare with the target branch, upload, comment, gate
assay upload .assay/runs/<id> --branch main   # upload an existing run as an analysis
assay report <run> --embed -o report.html     # one self-contained HTML file to share
assay portfolio <project>... -o portfolio.html   # one page over several apps
assay server --data ./assay-data [--public-url URL] [--config server.yaml]
assay hash-password                      # the admin password hash for server.yaml
```

Exit codes: `0` passed · `1` failed · `2` inconclusive (the environment: retry, don't blame the change) · `3`
configuration error.

| Environment variable | Used by | Meaning |
|---|---|---|
| `ASSAY_HOST_URL` | `ci`, `bench` | the server to upload to; unset: a local analysis, reports stay in the job's artifacts |
| `ASSAY_TOKEN` | `ci`, `bench`, `upload` | the server's admin token (CI other than GitHub Actions) |
| `ASSAY_PROJECT_KEY` | `ci`, `bench` | the project key outside GitHub (on GitHub it is `gh/<owner>/<repo>`) |
| `ANTHROPIC_API_KEY` | judge, goal agent | Claude through the API; locally a logged-in Claude Code CLI works without a key |
| `ASSAY_JUDGE` | judge | override the profile's judge backend: `claude-cli`, `anthropic` or `none` |
| `GITHUB_TOKEN` / `ASSAY_GITLAB_TOKEN` | `ci` | to post the pull request / merge request comment |
| `ASSAY_SERVER_TOKEN` | `server` | the admin token (default: generated into `<data>/token`) |
