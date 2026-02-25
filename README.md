# repo-health

Scans all GitHub repos for a user and scores their health. Outputs a ranked table.

## Checks

- Has README (25 pts)
- Has LICENSE (20 pts)
- Has description (15 pts)
- Last commit within 90 days (20 pts)
- Open issues count (up to -10 pts)

Score range: 0-100. Green >= 70, Yellow >= 40, Red < 40.

## Install

```bash
pip3 install --break-system-packages rich requests
```

## Run

```bash
python3 health.py
```

Options:

```
--user USER       GitHub username (default: ianalloway)
--token TOKEN     GitHub API token
--no-save         Skip saving markdown report
--stale-days N    Days before a repo is considered stale (default: 90)
--help            Show help
```

## Output

Prints a rich table to terminal. Saves markdown report to `~/.repo-health/report-YYYY-MM-DD.md`.
