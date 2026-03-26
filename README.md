# repo-health

Scan GitHub repositories and score their health across documentation, maintenance, and hygiene signals.

## Why This Repo Matters

This is a good example of product-minded developer tooling:

- small but useful CLI
- opinionated scoring model
- practical signal for repo hygiene
- easy to extend for teams, recruiters, or maintainers who want a quick repo audit

## Checks

- Has README (25 pts)
- Has LICENSE (20 pts)
- Has description (15 pts)
- Last commit within 90 days (20 pts)
- Open issues count (up to -10 pts)

Score range: 0-100. Green >= 70, Yellow >= 40, Red < 40.

## Example Use Cases

- audit your own GitHub account before job hunting
- compare public repos for maintenance quality
- spot weak READMEs, stale repos, or missing licenses quickly

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python health.py
```

Options:

```text
--user USER       GitHub username (default: ianalloway)
--token TOKEN     GitHub API token
--no-save         Skip saving markdown report
--stale-days N    Days before a repo is considered stale (default: 90)
--help            Show help
```

## Output

Prints a rich table to terminal. Saves a markdown report to `~/.repo-health/report-YYYY-MM-DD.md`.

## Future Improvements

- repository topics and homepage scoring
- branch/default-branch checks
- CI badge and workflow detection
- JSON output for automation
