<h1 align="center">Assay</h1>

<p align="center">
  <b>The quality gate for the running app.</b><br>
  SonarQube reads your code. Assay runs your product: it walks through the app like a person in a real browser<br>
  and checks what actually got saved. Every pull request gets a verdict with the evidence.
</p>

<p align="center">
  <a href="http://46.225.104.43:8080">Service</a> ·
  <a href="#connect-in-one-prompt">Connect</a> ·
  <a href="#what-a-pull-request-gets">What you get</a> ·
  <a href="#benchmarks-for-ai-features">Benchmarks</a> ·
  <a href="docs/AUTHORING.md">Journey format</a>
</p>

<p align="center">
  <img src="assets/pr-comment.png" width="760" alt="The Assay comment on a pull request: quality gate failed, one journey newly failing">
  <br><sub>A real pull request in a social app. The change cut every new comment to 20 characters.
  Assay read the database, found the short row and blocked the merge.</sub>
</p>

## Connect in one prompt

In your app's repository, in [Claude Code](https://claude.com/claude-code):

```
/plugin marketplace add RazinAleksandr/assay
/plugin install assay@assay
/assay:connect
```

Claude reads the app, writes 5-10 user journeys, proves each one on the running app, adds the GitHub Actions workflow
and opens a pull request. Its first CI run creates your account on the service. No sign-up, no tokens: GitHub vouches
for the job. About 30 minutes, without you.

From then on, ask Claude in plain words: *"add a journey for checkout"*, *"why did the Assay gate fail?"*,
*"benchmark the assistant on the cheaper model"*.

## What a pull request gets

```
❌ Don't ship: 1 journey broke
Mila comments on a post: `comments.body` was saved cut short in the database: 20 of 90 characters.
From you: nothing, your agent can fix it from the brief.  Evidence →  Brief for agents
Checked 12 of 140 journeys (touched by this change) · 3 min · compared with main @ 1b63234
```

- **A verdict in four lines:** ✅ Ship it, ❌ Don't ship, or ⚠️ Not sure (the check could not complete: never blamed on
  the product). One sentence built from what the app actually saved, what you need to do, and how much was checked.
- **Only what the change touches:** the journeys its files, API routes and tables can break, the critical ones and a
  small rotating sample. Only problems the change introduces block it.
- **The line that did it:** the brief ranks the hunks of the change by the data that went wrong (the hunk that writes
  `comments.body`, in the handler of the route the journey called) and says how to reproduce it.
- **Evidence:** the screens on `main` and in the change side by side, the saved values, every step.
- **A gate agents cannot game:** a change that removes or softens checks waits for a person to approve it.

## Fix it with your agent

In Claude Code, `/assay:fix <brief link>` reads the brief, opens the suspect code, reproduces the failure, fixes the
product and re-runs the journeys until the verdict is ✅. Other agents use the `assay` MCP server (`assay mcp`:
`assay_status`, `assay_brief`, `assay_run`, `assay_artifact`) or `assay brief <link>` for the same JSON.

## How a journey is checked

```yaml
- id: comment.add
  title: Mila comments on Noah's post
  as: mila
  steps:
    - goto: /p/1
    - fill: {label: Add a comment, value: "Lovely light in this one, where was it taken?"}
    - click: {role: button, name: Post}
  must:
    - state: {comments: {delta: 1, contains: {body: "Lovely light in this one, where was it taken?"}}}
    - allowed_changes: [comments, notifications]     # nothing else in the data may change
    - no_errors: true
```

Steps find elements the way people see them. The checks read the app's real state: a database query, an API, browser
storage. `allowed_changes` catches the side effects nobody wrote an assertion for.

## Benchmarks for AI features

A model answers differently every time, so one green run proves little. A second workflow repeats every journey and
compares arms: two models, two prompts, two builds. Each arm gets a pass rate with a 95% interval, the difference from
the baseline, and the journeys that flip. `/assay:connect` adds it when the app uses a model.

## Without Claude Code

```yaml
# .github/workflows/assay.yml
on: {push: {branches: [main]}, pull_request: {}}
jobs:
  assay:
    runs-on: ubuntu-latest
    permissions: {contents: read, pull-requests: write, id-token: write}
    steps:
      - uses: actions/checkout@v4
      - run: npm ci                    # install the app as your other jobs do
      - uses: RazinAleksandr/assay@v0
```

`.assay/` describes the app and its journeys. Start it with
`uvx --from https://github.com/RazinAleksandr/assay/raw/v0/dist/assay-1.0.0-py3-none-any.whl assay init`,
then follow [the format](docs/AUTHORING.md).

## What leaves your CI

The app runs inside your own CI job. The service receives the analysis: verdicts, the steps of each journey, screenshots
of your app's screens and the issues found. Your source code never leaves the job. Projects of private repositories are
private: they open from the link in the pull request comment.

## In this repository

| | |
|---|---|
| `action.yml` | the GitHub Action |
| `.claude-plugin/`, `.mcp.json`, `skills/`, `bin/` | the Claude Code plugin: `/assay:connect`, `/assay:fix`, the `assay` skill and MCP server |
| `dist/` | the scanner the action and the plugin run |
| `docs/AUTHORING.md` | the journey and profile format |
