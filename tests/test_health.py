"""Unit tests for repo-health. No network: every GitHub call is mocked."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import health


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(health.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


# ---------- scoring ----------

def healthy_repo(**overrides):
    repo = {
        "name": "good",
        "description": "a repo",
        "pushed_at": _iso(1),
        "open_issues_count": 0,
        "stargazers_count": 0,
        "forks_count": 0,
    }
    repo.update(overrides)
    return repo


def test_perfect_repo_scores_100_with_no_flags():
    score, flags = health.score_repo(healthy_repo(), True, True, True, ["cli"])
    assert score == 100
    assert flags == []


def test_each_missing_signal_applies_its_named_penalty():
    score, flags = health.score_repo(healthy_repo(description=""), False, False, False, [])
    expected = 100 - (
        health.MISSING_README_PENALTY
        + health.MISSING_LICENSE_PENALTY
        + health.MISSING_DESCRIPTION_PENALTY
        + health.MISSING_CI_PENALTY
        + health.MISSING_TOPICS_PENALTY
    )
    assert score == expected
    assert flags == ["no README", "no LICENSE", "no description", "no CI", "no topics"]


def test_stale_repo_is_flagged(monkeypatch):
    monkeypatch.setattr(health, "STALE_DAYS", 90)
    score, flags = health.score_repo(healthy_repo(pushed_at=_iso(200)), True, True, True, ["x"])
    assert score == 100 - health.STALE_PENALTY
    assert any(f.startswith("stale (") for f in flags)


def test_missing_push_date_is_penalised():
    score, flags = health.score_repo(healthy_repo(pushed_at=None), True, True, True, ["x"])
    assert score == 100 - health.NO_PUSH_DATE_PENALTY
    assert "no push date" in flags


@pytest.mark.parametrize(
    "issues,penalty,flagged",
    [(0, 0, False), (3, health.SOME_ISSUES_PENALTY, False), (11, health.MANY_ISSUES_PENALTY, True)],
)
def test_open_issue_penalties(issues, penalty, flagged):
    score, flags = health.score_repo(healthy_repo(open_issues_count=issues), True, True, True, ["x"])
    assert score == 100 - penalty
    assert (f"{issues} open issues" in flags) is flagged


def test_score_is_clamped_to_0_100():
    bonus, _ = health.score_repo(healthy_repo(stargazers_count=50, forks_count=9), True, True, True, ["x"])
    assert bonus == 100
    worst, _ = health.score_repo(
        healthy_repo(description="", pushed_at=_iso(999), open_issues_count=99), False, False, False, []
    )
    assert worst >= 0


# ---------- sorting / report ----------

def _row(name, score, stars, last_push, open_issues, flags=(), has_ci=True, topics=()):
    return {
        "name": name, "score": score, "stars": stars, "last_push": last_push,
        "open_issues": open_issues, "flags": list(flags), "url": "u",
        "has_ci": has_ci, "topics": list(topics),
    }


ROWS = [
    _row("b", 50, 1, "2026-01-01", 2),
    _row("A", 90, 5, "N/A", 0, flags=["no CI"], has_ci=False, topics=["t"]),
    _row("c", 70, 3, "2026-06-01", 9),
]


def test_sort_by_score_descending():
    assert [r["name"] for r in health.sort_results(ROWS, "score")] == ["A", "c", "b"]


def test_sort_by_name_is_alphabetical_case_insensitive():
    assert [r["name"] for r in health.sort_results(ROWS, "name")] == ["A", "b", "c"]


def test_sort_by_last_push_treats_na_as_oldest():
    assert [r["name"] for r in health.sort_results(ROWS, "last_push")] == ["c", "b", "A"]


def test_markdown_report_lists_every_repo():
    md = health.build_report_markdown(ROWS)
    assert md.startswith("# Repo Health Report")
    assert "Average score: 70.0/100" in md
    for r in ROWS:
        assert f"[{r['name']}]" in md


# ---------- auth / CLI ----------

def test_token_only_comes_from_env(monkeypatch):
    monkeypatch.setattr(health, "GITHUB_TOKEN", "")
    assert "Authorization" not in health.github_headers()
    monkeypatch.setattr(health, "GITHUB_TOKEN", "abc")
    assert health.github_headers()["Authorization"] == "Bearer abc"


def test_token_cli_argument_is_rejected_without_echoing_it(capsys):
    with pytest.raises(SystemExit) as exc:
        health.main(["--token", "ghp_supersecret"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN" in err
    assert "ghp_supersecret" not in err


def test_help_does_not_advertise_token_flag():
    assert "--token" not in health.build_parser().format_help()


# ---------- rate limiting ----------

def test_rate_limited_get_returns_first_ok_response():
    with mock.patch.object(health.requests, "get", return_value=FakeResp(200)) as get:
        assert health._rate_limited_get("u").status_code == 200
    assert get.call_count == 1


def test_429_is_retried_with_exponential_backoff(no_sleep):
    responses = [FakeResp(429), FakeResp(429), FakeResp(200)]
    with mock.patch.object(health.requests, "get", side_effect=responses) as get:
        assert health._rate_limited_get("u").status_code == 200
    assert get.call_count == 3
    backoffs = [s for s in no_sleep if s != health._RATE_INTERVAL]
    assert backoffs == [1.0, 2.0]


def test_retry_after_header_is_honoured(no_sleep):
    responses = [FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200)]
    with mock.patch.object(health.requests, "get", side_effect=responses):
        health._rate_limited_get("u")
    assert 7.0 in no_sleep


def test_secondary_rate_limit_403_waits_for_reset(no_sleep, monkeypatch):
    monkeypatch.setattr(health.time, "time", lambda: 1000.0)
    limited = FakeResp(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1005"})
    with mock.patch.object(health.requests, "get", side_effect=[limited, FakeResp(200)]) as get:
        assert health._rate_limited_get("u").status_code == 200
    assert get.call_count == 2
    assert 5.0 in no_sleep


def test_plain_403_is_not_retried():
    with mock.patch.object(health.requests, "get", return_value=FakeResp(403)) as get:
        assert health._rate_limited_get("u").status_code == 403
    assert get.call_count == 1


def test_long_reset_does_not_hang(monkeypatch):
    monkeypatch.setattr(health.time, "time", lambda: 0.0)
    limited = FakeResp(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3600"})
    with mock.patch.object(health.requests, "get", return_value=limited) as get:
        assert health._rate_limited_get("u").status_code == 403
    assert get.call_count == 1


def test_gives_up_after_max_retries():
    with mock.patch.object(health.requests, "get", return_value=FakeResp(429)) as get:
        assert health._rate_limited_get("u").status_code == 429
    assert get.call_count == health.MAX_RETRIES


# ---------- API helpers (mocked) ----------

def test_fetch_repos_paginates_until_empty():
    pages = [FakeResp(200, [{"name": "a"}, {"name": "b"}]), FakeResp(200, [{"name": "c"}]), FakeResp(200, [])]
    with mock.patch.object(health.requests, "get", side_effect=pages):
        assert [r["name"] for r in health.fetch_repos("me")] == ["a", "b", "c"]


def test_fetch_repos_exits_on_bad_credentials():
    with mock.patch.object(health.requests, "get", return_value=FakeResp(401)):
        with pytest.raises(SystemExit):
            health.fetch_repos("me")


def test_check_repo_health_uses_one_root_listing():
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.endswith("/contents/"):
            return FakeResp(200, [{"name": "README.md"}, {"name": "LICENSE"}, {"name": "main.py"}])
        if url.endswith("/.github/workflows"):
            return FakeResp(200, [{"name": "ci.yml"}])
        if url.endswith("/topics"):
            return FakeResp(200, {"names": ["cli"]})
        raise AssertionError(url)

    with mock.patch.object(health.requests, "get", side_effect=fake_get):
        result = health.check_repo_health("me", healthy_repo(html_url="https://x"))
    assert len(calls) == 3
    assert result["score"] == 100
    assert result["has_ci"] is True
    assert result["topics"] == ["cli"]


def test_missing_readme_and_license_detected_case_insensitively():
    def fake_get(url, **kwargs):
        if url.endswith("/contents/"):
            return FakeResp(200, [{"name": "readme.rst"}, {"name": "src"}])
        return FakeResp(404)

    with mock.patch.object(health.requests, "get", side_effect=fake_get):
        result = health.check_repo_health("me", healthy_repo())
    assert "no README" not in result["flags"]
    assert "no LICENSE" in result["flags"]


def test_main_json_output_end_to_end(capsys, monkeypatch):
    monkeypatch.setattr(health, "GITHUB_TOKEN", "t")

    def fake_get(url, **kwargs):
        if "/users/me/repos" in url:
            return FakeResp(200, [healthy_repo(name="r1")] if "&page=1&" in url else [])
        if url.endswith("/contents/"):
            return FakeResp(200, [{"name": "README.md"}, {"name": "LICENSE"}])
        if url.endswith("/.github/workflows"):
            return FakeResp(200, [{"name": "ci.yml"}])
        if url.endswith("/topics"):
            return FakeResp(200, {"names": ["x"]})
        raise AssertionError(url)

    with mock.patch.object(health.requests, "get", side_effect=fake_get):
        health.main(["--user", "me", "--json", "--no-save", "-P", "1"])
    out = capsys.readouterr().out
    data = json.loads(out[out.index("["):])
    assert [r["name"] for r in data] == ["r1"]
    assert data[0]["score"] == 100
