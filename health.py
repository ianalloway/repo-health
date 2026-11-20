#!/usr/bin/env python3
"""
repo-health: Scan all GitHub repos for ianalloway and score their health.
Outputs a ranked rich table and saves a markdown report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "ianalloway")
OUTPUT_DIR = Path.home() / ".repo-health"
STALE_DAYS = 90

API_BASE = "https://api.github.com"

# Valid columns for --sort
SORT_COLUMNS = ("score", "name", "stars", "last_push", "open_issues")


# ---------- Scoring weights ----------
MISSING_README_PENALTY = 25
MISSING_LICENSE_PENALTY = 15
MISSING_DESCRIPTION_PENALTY = 15
MISSING_CI_PENALTY = 10
MISSING_TOPICS_PENALTY = 5
STALE_PENALTY = 15
NO_PUSH_DATE_PENALTY = 10
MANY_ISSUES_THRESHOLD = 10
MANY_ISSUES_PENALTY = 10
SOME_ISSUES_PENALTY = 3
STARS_BONUS_THRESHOLD = 5
STARS_BONUS = 5
FORKS_BONUS_THRESHOLD = 2
FORKS_BONUS = 3

README_NAMES = {"readme", "readme.md", "readme.rst", "readme.txt"}
LICENSE_NAMES = {"license", "license.md", "license.txt", "licence", "licence.md", "copying"}


# ---------- Rate limiter ----------
# GitHub allows 60 req/hr unauthenticated and 5,000 req/hr with a token.
# Each run makes 1 request per 100 repos to list them, plus 2 per repo
# (root contents and workflows). Fetch topics separately for legacy responses.
_rate_lock = threading.Lock()
_RATE_INTERVAL = 0.15  # seconds between requests (~6.7 req/s; hourly quotas still apply)
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 60.0


def _retry_delay(resp, attempt: int) -> float | None:
    """Seconds to wait before retrying ``resp``, or None if it should not be retried."""
    if resp.status_code not in (403, 429):
        return None
    headers = resp.headers or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    if headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset")
        if reset is not None:
            try:
                wait = float(reset) - time.time()
            except ValueError:
                wait = 0.0
            if wait > MAX_BACKOFF_SECONDS:
                return None  # primary limit exhausted for a long time; don't hang
            return max(wait, 1.0)
    if resp.status_code == 429:
        return min(2.0 ** attempt, MAX_BACKOFF_SECONDS)
    # Plain 403 (e.g. permissions) is not a rate limit: don't retry.
    return None


def _rate_limited_get(url, **kwargs):
    """requests.get wrapper with request spacing and backoff for rate-limit responses.

    Retries 429s with exponential backoff (honouring Retry-After) and 403s that
    GitHub marks as rate limited (X-RateLimit-Remaining: 0 / Retry-After).
    """
    timeout = kwargs.pop("timeout", 15)
    resp = None
    for attempt in range(MAX_RETRIES):
        with _rate_lock:
            time.sleep(_RATE_INTERVAL)
        resp = requests.get(url, timeout=timeout, **kwargs)
        delay = _retry_delay(resp, attempt)
        if delay is None or attempt == MAX_RETRIES - 1:
            return resp
        time.sleep(delay)
    return resp  # pragma: no cover - loop always returns


def github_headers() -> dict:
    """Request headers. The token is only ever read from the GITHUB_TOKEN env var."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def fetch_repos(user: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/users/{user}/repos?per_page=100&page={page}&type=owner"
        resp = _rate_limited_get(url, headers=github_headers(), timeout=15)
        if resp.status_code == 401:
            print("[error] GitHub API: bad credentials. Set GITHUB_TOKEN env var.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code != 200:
            print(f"[error] GitHub API returned {resp.status_code}: {resp.text}", file=sys.stderr)
            sys.exit(1)
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def _repo_field(user: str, repo: str, path: str, expected: type):
    resp = _rate_limited_get(f"{API_BASE}/repos/{user}/{repo}/{path}", headers=github_headers(), timeout=10)
    if resp.status_code == 404:
        return expected()
    if resp.status_code != 200:
        raise ValueError(f"{path} returned HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, expected):
        raise ValueError(f"{path} returned an unexpected JSON shape")
    return payload


def list_root_files(user: str, repo: str) -> set[str]:
    """Lower-cased names of the files in the repo root, from one API call."""
    entries = _repo_field(user, repo, "contents/", list)
    return {str(e.get("name", "")).lower() for e in entries if isinstance(e, dict)}


def check_ci_exists(user: str, repo: str) -> bool:
    """Check for a YAML workflow file in the GitHub Actions directory."""
    return any(
        isinstance(f, dict) and f.get("type", "file") == "file"
        and str(f.get("name", "")).endswith((".yml", ".yaml"))
        for f in _repo_field(user, repo, "contents/.github/workflows", list)
    )


def check_topics(user: str, repo: str) -> list[str]:
    """Fetch topics for an older listing payload without them."""
    payload = _repo_field(user, repo, "topics", dict)
    names = payload.get("names", [])
    if not isinstance(names, list):
        raise ValueError("topics returned an unexpected names field")
    return names


def check_repo_health(user: str, repo: dict) -> dict:
    """
    Run all health checks for a single repo and return its result dict.

    Each worker fetches the root and workflows, reusing topics from the listing.
    """
    name = repo["name"]
    root_files = list_root_files(user, name)
    has_readme = bool(root_files & README_NAMES)
    has_license = bool(root_files & LICENSE_NAMES)
    has_ci = check_ci_exists(user, name)
    topics = repo.get("topics")
    if not isinstance(topics, list):
        topics = check_topics(user, name)
    score, flags = score_repo(repo, has_readme, has_license, has_ci, topics)

    pushed_at = repo.get("pushed_at") or "N/A"
    if pushed_at != "N/A":
        pushed_at = pushed_at[:10]

    return {
        "name": name,
        "score": score,
        "flags": flags,
        "last_push": pushed_at,
        "open_issues": repo.get("open_issues_count", 0),
        "stars": repo.get("stargazers_count", 0),
        "url": repo.get("html_url", ""),
        "has_ci": has_ci,
        "topics": topics,
    }


def score_repo(
    repo: dict,
    has_readme: bool,
    has_license: bool,
    has_ci: bool,
    topics: list[str],
) -> tuple[int, list[str]]:
    flags = []
    score = 100

    if not has_readme:
        score -= MISSING_README_PENALTY
        flags.append("no README")
    if not has_license:
        score -= MISSING_LICENSE_PENALTY
        flags.append("no LICENSE")
    if not repo.get("description"):
        score -= MISSING_DESCRIPTION_PENALTY
        flags.append("no description")
    if not has_ci:
        score -= MISSING_CI_PENALTY
        flags.append("no CI")
    if not topics:
        score -= MISSING_TOPICS_PENALTY
        flags.append("no topics")

    # Stale check
    pushed_at = repo.get("pushed_at") or repo.get("updated_at")
    if pushed_at:
        last = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - last).days
        if age_days > STALE_DAYS:
            score -= STALE_PENALTY
            flags.append(f"stale ({age_days}d)")
    else:
        flags.append("no push date")
        score -= NO_PUSH_DATE_PENALTY

    open_issues = repo.get("open_issues_count", 0)
    if open_issues > MANY_ISSUES_THRESHOLD:
        score -= MANY_ISSUES_PENALTY
        flags.append(f"{open_issues} open issues")
    elif open_issues > 0:
        score -= SOME_ISSUES_PENALTY

    # Bonus: starred or forked repos get a small credibility bump
    if repo.get("stargazers_count", 0) >= STARS_BONUS_THRESHOLD:
        score += STARS_BONUS
    if repo.get("forks_count", 0) >= FORKS_BONUS_THRESHOLD:
        score += FORKS_BONUS

    return max(0, min(100, score)), flags


def load_previous_report() -> dict[str, int]:
    """Load the most recent saved report to compute score deltas."""
    if not OUTPUT_DIR.exists():
        return {}
    reports = sorted(OUTPUT_DIR.glob("report-*.json"), reverse=True)
    if not reports:
        return {}
    try:
        data = json.loads(reports[0].read_text())
        return {r["name"]: r["score"] for r in data}
    except (ValueError, KeyError):
        return {}


def build_report_markdown(results: list[dict]) -> str:
    lines = [
        "# Repo Health Report",
        f"Generated: {date.today().isoformat()}",
        f"User: {GITHUB_USER}",
        f"Total repos: {len(results)}",
        f"Average score: {sum(r['score'] for r in results) / len(results):.1f}/100" if results else "",
        "",
        "| Repo | Score | CI | Topics | Flags | Last Push | Stars |",
        "|------|-------|----|--------|-------|-----------|-------|",
    ]
    for r in results:
        flags = ", ".join(r["flags"]) if r["flags"] else "clean"
        ci_str = "✓" if r.get("has_ci") else "✗"
        topics_str = ", ".join(r.get("topics", [])[:3]) or "—"
        lines.append(
            f"| [{r['name']}]({r['url']}) | {r['score']} | {ci_str} | {topics_str} | "
            f"{flags} | {r['last_push']} | {r.get('stars', 0)} |"
        )
    return "\n".join(lines)


def sort_results(results: list[dict], sort_by: str, descending: bool = True) -> list[dict]:
    """Sort results by the given column name."""
    reverse = descending

    if sort_by == "name":
        # Names sort ascending by default (alphabetical)
        reverse = not descending
        return sorted(results, key=lambda x: x["name"].lower(), reverse=reverse)
    elif sort_by == "last_push":
        # ISO date strings sort correctly as strings; treat "N/A" as oldest
        return sorted(results, key=lambda x: x["last_push"] if x["last_push"] != "N/A" else "0000-00-00", reverse=reverse)
    else:
        return sorted(results, key=lambda x: x.get(sort_by, 0), reverse=reverse)


REMOVED_CLI_FLAG_MSG = (
    "--token is no longer supported: tokens passed on the command line leak into "
    "shell history and process listings. Set the GITHUB_TOKEN environment variable instead."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan GitHub repos for health metrics.",
        epilog="Authentication: set the GITHUB_TOKEN environment variable (never pass tokens as arguments).",
    )
    parser.add_argument("--user", default=GITHUB_USER, help="GitHub username")
    parser.add_argument("--no-save", action="store_true", help="Skip saving report files")
    parser.add_argument("--stale-days", type=int, default=STALE_DAYS, help="Days before a repo is stale")
    parser.add_argument("--min-score", type=int, default=0, help="Only show repos with score below this value")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument(
        "--sort",
        choices=SORT_COLUMNS,
        default="score",
        help=f"Column to sort results by. Choices: {', '.join(SORT_COLUMNS)}. Default: score",
    )
    parser.add_argument(
        "--asc",
        action="store_true",
        help="Sort ascending instead of descending (default is descending)",
    )
    parser.add_argument(
        "--parallel", "-P",
        type=int,
        default=8,
        metavar="N",
        help="Max concurrent threads for repo health checks (default: 8). Use 1 to disable.",
    )
    # Hidden: kept only so old invocations fail loudly instead of silently
    # ignoring the value. The value is never used or echoed.
    parser.add_argument("--token", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    global GITHUB_USER, STALE_DAYS

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.token is not None:
        parser.error(REMOVED_CLI_FLAG_MSG)

    GITHUB_USER = args.user
    STALE_DAYS = args.stale_days

    console = Console(stderr=args.json)

    if not GITHUB_TOKEN:
        console.print("[yellow]Warning: No GITHUB_TOKEN set. Rate limits will apply (60 req/hr).[/yellow]")

    console.print(f"[cyan]Fetching repos for {GITHUB_USER}...[/cyan]")

    repos = fetch_repos(GITHUB_USER)
    if not repos:
        console.print("[yellow]No repos found.[/yellow]")
        if args.json:
            print("[]")
        return

    workers = max(1, min(args.parallel, len(repos)))
    console.print(
        f"[green]Found {len(repos)} repos. Checking health "
        f"({'parallel ×' + str(workers) if workers > 1 else 'sequential'})...[/green]\n"
    )

    previous_scores = load_previous_report()
    results = []

    # Each worker checks one repository at a time.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_repo = {
            pool.submit(check_repo_health, GITHUB_USER, repo): repo
            for repo in repos
        }
        for future in as_completed(future_to_repo):
            repo = future_to_repo[future]
            try:
                result = future.result()
            except Exception as exc:
                console.print(f"[red]Error checking {repo['name']}: {exc}[/red]")
                continue

            prev_score = previous_scores.get(result["name"])
            result["delta"] = (result["score"] - prev_score) if prev_score is not None else None
            results.append(result)
            console.print(f"  ✓ [cyan]{result['name']}[/cyan] — {result['score']}/100", end="\r")

    console.print()  # clear the \r line
    if len(results) != len(repos):
        console.print("[red]Incomplete scan. No report was written.[/red]")
        sys.exit(1)

    # Sort
    results = sort_results(results, args.sort, descending=not args.asc)

    # Filter if --min-score flag used (show only repos below threshold)
    display_results = results
    if args.min_score > 0:
        display_results = [r for r in results if r["score"] < args.min_score]

    if args.json:
        print(json.dumps(display_results, indent=2))
        return

    sort_indicator = f"{args.sort} {'↑' if args.asc else '↓'}"

    # Rich table
    table = Table(
        title=f"Repo Health: {GITHUB_USER}  [dim](sorted by {sort_indicator})[/dim]",
        box=box.ROUNDED,
        show_header=True,
        expand=True,
    )
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right", width=10)
    table.add_column("CI", justify="center", width=4)
    table.add_column("Topics", width=20)
    table.add_column("Flags", style="yellow")
    table.add_column("Last Push", width=12)
    table.add_column("Stars", justify="right", width=6)

    for r in display_results:
        score_color = "green" if r["score"] >= 70 else ("yellow" if r["score"] >= 40 else "red")
        flags_str = ", ".join(r["flags"]) if r["flags"] else "[dim]clean[/dim]"
        ci_str = "[green]✓[/green]" if r["has_ci"] else "[red]✗[/red]"
        topics_str = ", ".join(r["topics"][:2]) if r["topics"] else "[dim]—[/dim]"

        # Score with delta indicator
        delta = r.get("delta")
        if delta is not None and delta != 0:
            arrow = "↑" if delta > 0 else "↓"
            delta_color = "green" if delta > 0 else "red"
            score_cell = f"[{score_color}]{r['score']}[/{score_color}] [{delta_color}]{arrow}{abs(delta)}[/{delta_color}]"
        else:
            score_cell = f"[{score_color}]{r['score']}[/{score_color}]"

        table.add_row(
            r["name"],
            score_cell,
            ci_str,
            topics_str,
            flags_str,
            r["last_push"],
            str(r["stars"]) if r["stars"] else "—",
        )

    console.print()
    console.print(table)

    avg_score = sum(r["score"] for r in results) / len(results) if results else 0
    clean_count = sum(1 for r in results if not r["flags"])
    ci_count = sum(1 for r in results if r["has_ci"])
    no_desc_count = sum(1 for r in results if "no description" in r["flags"])
    no_topics_count = sum(1 for r in results if "no topics" in r["flags"])

    summary = Text()
    summary.append("\nAverage score:    ", style="bold")
    summary.append(f"{avg_score:.1f}/100\n")
    summary.append("Clean repos:      ", style="bold")
    summary.append(f"{clean_count}/{len(results)}\n")
    summary.append("Repos with CI:    ", style="bold")
    summary.append(f"{ci_count}/{len(results)}\n")
    summary.append("Missing desc:     ", style="bold")
    summary.append(f"{no_desc_count} repos\n", style="yellow" if no_desc_count else "")
    summary.append("Missing topics:   ", style="bold")
    summary.append(f"{no_topics_count} repos", style="yellow" if no_topics_count else "")

    console.print(Panel(summary, title="Summary", expand=False))

    if not args.no_save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()

        # Save markdown
        md_file = OUTPUT_DIR / f"report-{today}.md"
        md_file.write_text(build_report_markdown(results))

        # Save JSON for delta tracking in future runs
        json_file = OUTPUT_DIR / f"report-{today}.json"
        json_file.write_text(json.dumps(results, indent=2))

        console.print(f"\n[dim]Reports saved to {OUTPUT_DIR}[/dim]")


if __name__ == "__main__":
    main()
