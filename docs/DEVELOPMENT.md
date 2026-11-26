# Developing Assay

## Repository layout

| Path | What |
|---|---|
| [`assay/`](../assay) | the engine and the CLI: driver, probes, sensors, checks, judge, goal agent, grading, selection, brief, MCP, the explore session ([`explore.py`](../assay/explore.py)) |
| [`assay/server.py`](../assay/server.py), [`assay/web/`](../assay/web) | the server: API for CI, accounts, the pages people and agents read |
| [`action.yml`](../action.yml) | the GitHub Action (`uses: RazinAleksandr/assay@v0`); uv builds the scanner from this repository |
| [`.claude-plugin/`](../.claude-plugin), [`skills/`](../skills), [`bin/`](../bin) | the Claude Code plugin and its marketplace: `/assay:connect`, `/assay:explore`, `/assay:bench`, `/assay:fix`, the `assay` skill and the MCP server |
| [`ci/`](ci), [`Dockerfile`](../Dockerfile) | the GitLab template and the scanner image for CI other than GitHub |
| [`deploy/`](deploy) | the server image and the scripts that install it on a machine |
| [`docs/`](.) | [the journey and profile format](AUTHORING.md), screenshots |
| [`examples/hello/`](../examples/hello) | the smallest complete project; this repository's CI runs it through the action |
| [`tests/`](../tests) | unit and integration tests |

## Build and test

```bash
uv sync                                   # Python 3.11+, installs the engine in .venv
uv run pytest -q                          # the test suite (about 30 s)
uv run assay browsers                     # Chromium for the driver, once per machine
uv run assay run examples/hello/.assay --scan            # a full local run against the example app
uv run assay server --data /tmp/assay-data --port 9000   # the server on http://127.0.0.1:9000
```

Try your change to the plugin without publishing it: `claude --plugin-dir .` in an app's repository.

**CI and releases.** [`ci.yml`](../.github/workflows/ci.yml) runs the tests, then the action from this checkout
(`uses: ./`) on a clean runner against `examples/hello`. After a green run on `main`,
[`deploy.yml`](../.github/workflows/deploy.yml) updates the hosted service and moves the tag `v0`, so the action users run,
the plugin they install (from `main`) and the server they upload to always come from the same commit. The tag `v1`
(the 1.0 release) is moved by hand.
