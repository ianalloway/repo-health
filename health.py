#!/usr/bin/env python3
"""
repo-health: Scan all GitHub repos for ianalloway and score their health.
Outputs a ranked rich table and saves a markdown report.
"""

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from rich import box
from rich.console import Console
from rich.table import Table

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "ianalloway")
OUTPUT_DIR = Path.home() / ".repo-health"
STALE_DAYS = 90

API_BASE = "https://api.github.com"


def github_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def fetch_repos(user: str) -> list[dict]:
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/users/{user}/repos?per_page=100&page={page}&type=owner"
        resp = requests.get(url, headers=github_headers(), timeout=15)
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


def score_repo(repo: dict, has_readme: bool, has_license: bool) -> tuple[int, list[str]]:
    flags = []
    score = 100

    if not has_readme:
        score -= 25
        flags.append("no README")
    if not has_license:
        score -= 20
        flags.append("no LICENSE")
    if not repo.get("description"):
        score -= 15
        flags.append("no description")

    # Stale check
    pushed_at = repo.get("pushed_at") or repo.get("updated_at")
    if pushed_at:
        last = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - last).days
        if age_days > STALE_DAYS:
            score -= 20
            flags.append(f"stale ({age_days}d)")
    else:
        flags.append("no push date")
        score -= 10

    open_issues = repo.get("open_issues_count", 0)
    if open_issues > 10:
        score -= 10
        flags.append(f"{open_issues} open issues")
    elif open_issues > 0:
        score -= 5

    return max(0, score), flags


def build_report(results: list[dict]) -> str:
    lines = [
        f"# Repo Health Report",
        f"Generated: {date.today().isoformat()}",
        f"User: {GITHUB_USER}",
        f"Total repos: {len(results)}",
        "",
        "| Repo | Score | Flags | Last Push | Open Issues |",
        "|------|-------|-------|-----------|-------------|",
    ]
    for r in results:
        flags = ", ".join(r["flags"]) if r["flags"] else "clean"
        lines.append(
            f"| [{r['name']}]({r['url']}) | {r['score']} | {flags} | {r['last_push']} | {r['open_issues']} |"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Scan GitHub repos for health metrics.")
    parser.add_argument("--user", default=GITHUB_USER, help="GitHub username")
    parser.add_argument("--token", default=GITHUB_TOKEN, help="GitHub API token")
    parser.add_argument("--no-save", action="store_true", help="Skip saving markdown report")
    parser.add_argument("--stale-days", type=int, default=STALE_DAYS, help="Days before a repo is stale")
    args = parser.parse_args()

    global GITHUB_TOKEN, GITHUB_USER, STALE_DAYS
    GITHUB_TOKEN = args.token
    GITHUB_USER = args.user
    STALE_DAYS = args.stale_days

    console = Console()
    console.print(f"[cyan]Fetching repos for {GITHUB_USER}...[/cyan]")

    repos = fetch_repos(GITHUB_USER)
    if not repos:
        console.print("[red]No repos found or API error.[/red]")
        return

    console.print(f"[green]Found {len(repos)} repos. Checking health...[/green]\n")

    results = []
    for repo in repos:
        name = repo["name"]
        console.print(f"  checking {name}...", end="\r")
        has_readme = check_file_exists(GITHUB_USER, name, "README.md")
        has_license = check_file_exists(GITHUB_USER, name, "LICENSE") or check_file_exists(GITHUB_USER, name, "LICENSE.md")
        score, flags = score_repo(repo, has_readme, has_license)

        pushed_at = repo.get("pushed_at") or "N/A"
        if pushed_at != "N/A":
            pushed_at = pushed_at[:10]

        results.append({
            "name": name,
            "score": score,
            "flags": flags,
            "last_push": pushed_at,
            "open_issues": repo.get("open_issues_count", 0),
            "url": repo.get("html_url", ""),
        })

    results.sort(key=lambda x: x["score"], reverse=True)

    table = Table(
        title=f"Repo Health: {GITHUB_USER}",
        box=box.ROUNDED,
        show_header=True,
        expand=True,
    )
    table.add_column("Repo", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right", width=7)
    table.add_column("Flags", style="yellow")
    table.add_column("Last Push", width=12)
    table.add_column("Issues", justify="right", width=8)

    for r in results:
        score_color = "green" if r["score"] >= 70 else ("yellow" if r["score"] >= 40 else "red")
        flags_str = ", ".join(r["flags"]) if r["flags"] else "[dim]clean[/dim]"
        table.add_row(
            r["name"],
            f"[{score_color}]{r['score']}[/{score_color}]",
            flags_str,
            r["last_push"],
            str(r["open_issues"]),
        )

    console.print()
    console.print(table)

    avg_score = sum(r["score"] for r in results) / len(results) if results else 0
    console.print(f"\n[bold]Average score:[/bold] {avg_score:.1f}/100")
    console.print(f"[bold]Clean repos:[/bold] {sum(1 for r in results if not r['flags'])}/{len(results)}")

    if not args.no_save:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        report_file = OUTPUT_DIR / f"report-{date.today().isoformat()}.md"
        report_file.write_text(build_report(results))
        console.print(f"\n[dim]Report saved to {report_file}[/dim]")


if __name__ == "__main__":
    main()
