# repo-health

Scan GitHub repositories and score their health across documentation, maintenance, and hygiene signals.

## Why This Repo Matters

This is a good example of product-minded developer tooling:

- small but useful CLI
- opinionated scoring model
- practical signal for repo hygiene
- easy to extend for teams, recruiters, or maintainers who want a quick repo audit

## Checks

Each repo starts at 100 and loses points for missing signals (weights are named constants at the top of `health.py`):

- No README: -25
- No LICENSE: -15
- No description: -15
- No GitHub Actions workflow: -10
- No topics: -5
- No push in the last 90 days (`--stale-days`): -15
- More than 10 open issues: -10 (1-10 open issues: -3)
- Bonus: +5 for 5+ stars, +3 for 2+ forks

Score range: 0-100. Green >= 70, Yellow >= 40, Red < 40.

## Example Use Cases

- audit your own GitHub account before job hunting
- compare public repos for maintenance quality
- spot weak READMEs, stale repos, or missing licenses quickly

## Install

```bash
pip install -r requirements.txt
```

## Authentication

Set a GitHub token in the `GITHUB_TOKEN` environment variable. Without it, GitHub allows only 60 requests per hour.

```bash
export GITHUB_TOKEN=...   # or: export GITHUB_TOKEN="$(gh auth token)"
```

The token is never accepted as a command-line argument, because arguments leak into shell history and `ps` output. Passing `--token` exits with an error.

## Run

```bash
python health.py
```

Options:

```text
--user USER        GitHub username (default: $GITHUB_USER or ianalloway)
--no-save          Skip saving report files
--stale-days N     Days before a repo is considered stale (default: 90)
--min-score N      Only show repos scoring below N
--json             Write one JSON array to stdout (progress goes to stderr)
--sort COLUMN      score | name | stars | last_push | open_issues (default: score)
--asc              Sort ascending
--parallel, -P N   Concurrent repo checks (default: 8)
--help             Show help
```

## API usage and rate limits

The scanner fetches the repository list in pages of 100 and stops at the first empty page. Each repository adds two requests, one for the root files and one for workflows. Older responses without topics require a third request. If metadata becomes unavailable mid-scan, the CLI exits with status 1 to keep an incomplete scorecard out of the saved history. Requests are spaced about 0.15 s apart across all threads.

On HTTP 429, or a 403 that GitHub marks as rate limited, the tool backs off and retries up to 5 times. It honours `Retry-After` and `X-RateLimit-Reset`, and gives up rather than hang if the reset is more than 60 s away.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && bandit -q -c pyproject.toml -r . && pytest
```

Tests mock every GitHub call, so they need no network access or token.

## Output

Prints a rich table to terminal. Saves a markdown report to `~/.repo-health/report-YYYY-MM-DD.md`.

## Future Improvements

- repository topics and homepage scoring
- branch/default-branch checks
