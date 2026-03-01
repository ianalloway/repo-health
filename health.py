#!/usr/bin/env python3
"""
repo-health: Scan all GitHub repos for ianalloway and score their health.
Outputs a ranked rich table and saves a markdown report.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from rich import box
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "ianalloway")
OUTPUT_DIR = Path.home() / ".repo-health"
STALE_DAYS = 90

API_BASE = "https://api.github.com"


def github_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers


def fetch_repos(user: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/users/{user}/repos?per_page=100&page={page}&type=owner"
        resp = requests.get(url, headers=github_headers(), timeout=15)
        if resp.status_code == 401:
            print("[error] GitHub API: bad credentials. Set GITHUB_TOKEN env var.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code != 200:
            print(f"[error] GitHub API returned {resp.status_code}: {resp.text}", file=sys.stderr)
            break
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def check_file_exists(user: str, repo: str, filename: str) -> bool:
    url = f"{API_BASE}/repos/{user}/{repo}/contents/{filename}"
    resp = requests.get(url, headers=github_headers(), timeout=10)
    return resp.status_code == 200


def check_ci_exists(user: str, repo: str) -> bool:
    """Check if the repo has a GitHub Actions workflow."""
    url = f"{API_BASE}/repos/{user}/{repo}/contents/.github/workflows"
    resp = requests.get(url, headers=github_headers(), timeout=10)
    if resp.status_code == 200:
        try:
            files = resp.json()
            return isinstance(files, list) and len(files) > 0
        except (ValueError, TypeError):
            return False
    return False


def check_topics(user: str, repo: str) -> list[str]:
    """Fetch topics/tags for a repo."""
    url = f"{API_BASE}/repos/{user}/{repo}/topics"
    resp = requests.get(
        url,
        headers={**github_headers(), "Accept": "application/vnd.github.mercy-preview+json"},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json().get("names", [])
    return []


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
        score -= 25
        flags.append("no README")
    if not has_license:
        score -= 15
        flags.append("no LICENSE")
    if not repo.get("description"):
        score -= 15
        flags.append("no description")
    if not has_ci:
        score -= 10
        flags.append("no CI")
    if not topics:
        score -= 5
        flags.append("no topics")

    # Stale check
    pushed_at = repo.get("pushed_at") or repo.get("updated_at")
    if pushed_at:
        last = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - last).days
        if age_days > STALE_DAYS:
            score -= 15
            flags.append(f"stale ({age_days}d)")
    else:
        flags.append("no push date")
        score -= 10

    open_issues = repo.get("open_issues_count", 0)
    if open_issues > 10:
        score -= 10
        flags.append(f"{open_issues} open issues")
    elif open_issues > 0:
        score -= 3

    # Bonus: starred or forked repos get a small credibility bump
    if repo.get("stargazers_count", 0) >= 5:
        score += 5
    if repo.get("forks_count", 0) >= 2:
        score += 3

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


def main():
    global GITHUB_TOKEN, GITHUB_USER, STALE_DAYS

    parser = argparse.ArgumentParser(description="Scan GitHub repos for health metrics.")
    parser.add_argument("--user", default=GITHUB_USER, help="GitHub username")
    parser.add_argument("--token", default=GITHUB_TOKEN, help="GitHub API token")
    parser.add_argument("--no-save", action="store_true", help="Skip saving report files")
    parser.add_argument("--stale-days", type=int, default=STALE_DAYS, help="Days before a repo is stale")
    parser.add_argument("--min-score", type=int, default=0, help="Only show repos with score below this value")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    GITHUB_TOKEN = args.token
    GITHUB_USER = args.user
    STALE_DAYS = args.stale_days

    console = Console()

    if not GITHUB_TOKEN:
        console.print("[yellow]Warning: No GITHUB_TOKEN set. Rate limits will apply (60 req/hr).[/yellow]")

    console.print(f"[cyan]Fetching repos for {GITHUB_USER}...[/cyan]")

    repos = fetch_repos(GITHUB_USER)
    if not repos:
        console.print("[red]No repos found or API error.[/red]")
        return

    console.print(f"[green]Found {len(repos)} repos. Checking health...[/green]\n")

    previous_scores = load_previous_report()
    results = []

    for repo in repos:
        name = repo["name"]
        console.print(f"  checking [cyan]{name}[/cyan]...", end="\r")
        has_readme = check_file_exists(GITHUB_USER, name, "README.md")
        has_license = (
            check_file_exists(GITHUB_USER, name, "LICENSE")
            or check_file_exists(GITHUB_USER, name, "LICENSE.md")
        )
        has_ci = check_ci_exists(GITHUB_USER, name)
        topics = check_topics(GITHUB_USER, name)
        score, flags = score_repo(repo, has_readme, has_license, has_ci, topics)

        pushed_at = repo.get("pushed_at") or "N/A"
        if pushed_at != "N/A":
            pushed_at = pushed_at[:10]

        prev_score = previous_scores.get(name)
        delta = (score - prev_score) if prev_score is not None else None

        results.append({
            "name": name,
            "score": score,
            "flags": flags,
            "last_push": pushed_at,
            "open_issues": repo.get("open_issues_count", 0),
            "stars": repo.get("stargazers_count", 0),
            "url": repo.get("html_url", ""),
            "has_ci": has_ci,
            "topics": topics,
            "delta": delta,
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    # Filter if --min-score flag used (show only repos below threshold)
    display_results = results
    if args.min_score > 0:
        display_results = [r for r in results if r["score"] < args.min_score]

    if args.json:
        print(json.dumps(display_results, indent=2))
        return

    # Rich table
    table = Table(
        title=f"Repo Health: {GITHUB_USER}",
        box=box.ROUNDED,
        show_header=True,
        expand=True,
    )
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right", width=8)
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
    summary.append(f"\nAverage score:    ", style="bold")
    summary.append(f"{avg_score:.1f}/100\n")
    summary.append(f"Clean repos:      ", style="bold")
    summary.append(f"{clean_count}/{len(results)}\n")
    summary.append(f"Repos with CI:    ", style="bold")
    summary.append(f"{ci_count}/{len(results)}\n")
    summary.append(f"Missing desc:     ", style="bold")
    summary.append(f"{no_desc_count} repos\n", style="yellow" if no_desc_count else "")
    summary.append(f"Missing topics:   ", style="bold")
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
