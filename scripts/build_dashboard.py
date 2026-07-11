#!/usr/bin/env python3
"""Build the ZeroClaw distribution and repository metrics dashboard.

The live services remain the source of truth. This script writes a timestamped
snapshot plus a self-contained HTML dashboard into this repository.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
INDEX_PATH = ROOT / "index.html"
LATEST_PATH = DATA_DIR / "latest.json"
DAILY_PATH = DATA_DIR / "daily.json"
DATABASE_PATH = DATA_DIR / "metrics.sqlite"
NOW = dt.datetime.now(dt.timezone.utc)
OWNER = "zeroclaw-labs"
REPO = "zeroclaw"
FULL_REPO = f"{OWNER}/{REPO}"
USER_AGENT = "zeroclaw-metrics-dashboard (https://github.com/zeroclaw-labs/zeroclaw)"


def run(args: list[str], *, input_text: str | None = None) -> str:
    return subprocess.check_output(args, cwd=ROOT, input=input_text, text=True)


def gh_api(path: str, jq: str | None = None) -> Any:
    args = ["gh", "api", path]
    if jq:
        args += ["--jq", jq]
    return json.loads(run(args))


def gh_api_paginated(path: str) -> list[Any]:
    raw = run(["gh", "api", path, "--paginate"])
    decoder = json.JSONDecoder()
    idx = 0
    out: list[Any] = []
    while idx < len(raw):
        while idx < len(raw) and raw[idx].isspace():
            idx += 1
        if idx >= len(raw):
            break
        value, idx = decoder.raw_decode(raw, idx)
        if isinstance(value, list):
            out.extend(value)
        else:
            out.append(value)
    return out


def get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_text(url: str, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_installable_asset(name: str) -> bool:
    if name in {"install.sh", "ZeroClaw.dmg"}:
        return True
    return (
        name.startswith("zeroclaw-")
        and (name.endswith(".tar.gz") or name.endswith(".zip"))
        and ".sigstore." not in name
    )


def asset_category(name: str) -> str:
    if name == "install.sh":
        return "Install script"
    if name == "ZeroClaw.dmg":
        return "DMG"
    if name.endswith(".zip"):
        return "Windows zip"
    if "apple-darwin" in name:
        return "macOS tarball"
    if "linux-android" in name or "androideabi" in name:
        return "Android"
    if "unknown-linux-gnu" in name:
        return "Linux GNU"
    if "unknown-linux-musl" in name:
        return "Linux musl"
    if "gnueabihf" in name:
        return "Linux ARM"
    return "Other installable"


def is_payload_asset(name: str) -> bool:
    return is_installable_asset(name) and name != "install.sh"


def collect_releases() -> dict[str, Any]:
    releases = gh_api_paginated(f"repos/{FULL_REPO}/releases")
    rows: list[dict[str, Any]] = []
    asset_totals: defaultdict[str, int] = defaultdict(int)

    for release in releases:
        published_at = parse_iso(release["published_at"])
        age_days = max((NOW - published_at).total_seconds() / 86400, 1 / 24)
        installable = [a for a in release.get("assets", []) if is_installable_asset(a["name"])]
        payload_assets = [a for a in installable if is_payload_asset(a["name"])]
        total = sum(int(a["download_count"]) for a in installable)
        payload_total = sum(int(a["download_count"]) for a in payload_assets)
        bootstrap_total = sum(int(a["download_count"]) for a in installable if a["name"] == "install.sh")
        if not total:
            continue

        mix: defaultdict[str, int] = defaultdict(int)
        for asset in installable:
            count = int(asset["download_count"])
            mix[asset_category(asset["name"])] += count
            asset_totals[asset_category(asset["name"])] += count

        rows.append(
            {
                "tag": release["tag_name"],
                "name": release.get("name") or release["tag_name"],
                "published_at": release["published_at"],
                "published_date": release["published_at"][:10],
                "prerelease": bool(release["prerelease"]),
                "asset_count": len(installable),
                "downloads": total,
                "payload_downloads": payload_total,
                "bootstrap_downloads": bootstrap_total,
                "age_days": age_days,
                "downloads_per_week": total / (age_days / 7),
                "payload_downloads_per_week": payload_total / (age_days / 7),
                "mix": dict(sorted(mix.items(), key=lambda item: item[1], reverse=True)),
            }
        )

    top_by_rate = sorted(rows, key=lambda row: (row["downloads_per_week"], row["downloads"]), reverse=True)
    stable = [row for row in top_by_rate if not row["prerelease"]]
    recent_stable = sorted(stable, key=lambda row: row["published_at"], reverse=True)

    return {
        "release_count": len(releases),
        "with_installable_downloads": len(rows),
        "installable_downloads_total": sum(row["downloads"] for row in rows),
        "payload_downloads_total": sum(row["payload_downloads"] for row in rows),
        "bootstrap_downloads_total": sum(row["bootstrap_downloads"] for row in rows),
        "stable_downloads_total": sum(row["downloads"] for row in rows if not row["prerelease"]),
        "prerelease_downloads_total": sum(row["downloads"] for row in rows if row["prerelease"]),
        "top_by_rate": top_by_rate[:15],
        "recent_stable": recent_stable[:12],
        "all_installable": sorted(rows, key=lambda row: row["published_at"], reverse=True),
        "asset_totals": dict(sorted(asset_totals.items(), key=lambda item: item[1], reverse=True)),
    }


def collect_ghcr() -> dict[str, Any]:
    token = run(["gh", "auth", "token"]).strip()
    package = gh_api(f"orgs/{OWNER}/packages/container/{REPO}")
    versions = gh_api_paginated(f"orgs/{OWNER}/packages/container/{REPO}/versions?per_page=100")
    auth_headers = {"Authorization": f"Bearer {token}"}
    package_html = get_text(f"https://github.com/orgs/{OWNER}/packages/container/package/{REPO}", auth_headers)

    total_match = re.search(r"Total downloads</span>\s*<h3 title=\"([0-9]+)\">([^<]+)</h3>", package_html)
    daily_rows = re.findall(r'data-merge-count="([0-9]+)"\s*data-date="([0-9-]+)"', package_html)
    daily = sorted({date: int(count) for count, date in daily_rows}.items())

    visible_versions: list[dict[str, Any]] = []
    for item in re.findall(r"<li[^>]*Box-row[^>]*>(.*?)</li>", package_html, re.S):
        if "Version downloads" not in item:
            continue
        labels = re.findall(r"\?tag=([^\"&]+)[^\"]*\">([^<]+)</a>", item)
        count_match = re.search(r'octicon-download.*?</svg>\s*([0-9,]+)\s*<span class="sr-only">Version downloads', item, re.S)
        digest_match = re.search(r"value=\"(sha256:[a-f0-9]+)\"", item)
        if labels and count_match:
            visible_versions.append(
                {
                    "tags": [html.unescape(label_text) for _, label_text in labels],
                    "downloads": int(count_match.group(1).replace(",", "")),
                    "digest": digest_match.group(1) if digest_match else None,
                }
            )

    tagged_versions = [
        version
        for version in versions
        if version.get("metadata", {}).get("container", {}).get("tags")
    ]
    tags = sorted({tag for version in tagged_versions for tag in version["metadata"]["container"]["tags"]})

    return {
        "package": {
            "id": package["id"],
            "visibility": package["visibility"],
            "created_at": package["created_at"],
            "updated_at": package["updated_at"],
            "version_count": package.get("version_count"),
            "html_url": package["html_url"],
        },
        "total_downloads": int(total_match.group(1)) if total_match else None,
        "total_downloads_display": total_match.group(2) if total_match else None,
        "last_30_daily": [{"date": date, "downloads": count} for date, count in daily],
        "last_30_downloads": sum(count for _, count in daily),
        "last_7_downloads": sum(count for _, count in daily[-7:]),
        "visible_versions": visible_versions[:12],
        "tag_count": len(tags),
        "release_tags_present": [tag for tag in ["latest", "v0.8.2", "debian", "v0.8.2-debian"] if tag in tags],
    }


def search_count(query: str) -> int:
    encoded = urllib.parse.quote(query, safe="")
    return int(gh_api(f"search/issues?q={encoded}")["total_count"])


def collect_github_repo() -> dict[str, Any]:
    repo = gh_api(f"repos/{FULL_REPO}")
    traffic_views = gh_api(f"repos/{FULL_REPO}/traffic/views?per=day")
    traffic_clones = gh_api(f"repos/{FULL_REPO}/traffic/clones?per=day")
    traffic_referrers = gh_api(f"repos/{FULL_REPO}/traffic/popular/referrers")
    traffic_paths = gh_api(f"repos/{FULL_REPO}/traffic/popular/paths")
    since = (NOW - dt.timedelta(days=7)).date().isoformat()
    commits = gh_api_paginated(f"repos/{FULL_REPO}/commits?sha=master&since={since}T00:00:00Z")
    contributor_stats = gh_api(f"repos/{FULL_REPO}/stats/contributors")
    commit_activity = gh_api(f"repos/{FULL_REPO}/stats/commit_activity")

    top_contributors = sorted(
        [
            {
                "login": item["author"]["login"] if item.get("author") else "unknown",
                "total": item["total"],
                "last_week": item["weeks"][-1].get("c", 0) if item.get("weeks") else 0,
            }
            for item in contributor_stats
        ],
        key=lambda item: item["last_week"],
        reverse=True,
    )[:10]

    return {
        "stars": repo["stargazers_count"],
        "forks": repo["forks_count"],
        "watchers": repo["subscribers_count"],
        "open_issues": repo["open_issues_count"],
        "pushed_at": repo["pushed_at"],
        "updated_at": repo["updated_at"],
        "traffic": {
            "views_14d": traffic_views["count"],
            "views_uniques_14d": traffic_views["uniques"],
            "clones_14d": traffic_clones["count"],
            "clones_uniques_14d": traffic_clones["uniques"],
            "views_daily": traffic_views["views"],
            "clones_daily": traffic_clones["clones"],
            "top_referrers": traffic_referrers,
            "popular_paths": traffic_paths,
        },
        "pulse_7d": {
            "since": since,
            "prs_opened": search_count(f"repo:{FULL_REPO} is:pr created:>={since}"),
            "prs_merged": search_count(f"repo:{FULL_REPO} is:pr merged:>={since}"),
            "issues_opened": search_count(f"repo:{FULL_REPO} is:issue created:>={since}"),
            "issues_closed": search_count(f"repo:{FULL_REPO} is:issue closed:>={since}"),
            "default_branch_commits": len(commits),
        },
        "commit_activity_latest_week": commit_activity[-1] if commit_activity else None,
        "top_contributors_latest_week": top_contributors,
    }


def collect_homebrew() -> dict[str, Any]:
    formula = get_json("https://formulae.brew.sh/api/formula/zeroclaw.json")

    def analytics(kind: str, period: str) -> dict[str, Any] | None:
        try:
            data = get_json(f"https://formulae.brew.sh/api/analytics/{kind}/{period}.json")
        except urllib.error.HTTPError:
            return None
        for item in data.get("items", []):
            if item.get("formula") == "zeroclaw":
                return item
        return None

    return {
        "version": formula["versions"]["stable"],
        "homepage": formula["homepage"],
        "install": {
            "30d": analytics("install", "30d"),
            "90d": analytics("install", "90d"),
            "365d": analytics("install", "365d"),
        },
        "install_on_request": {
            "30d": analytics("install-on-request", "30d"),
            "90d": analytics("install-on-request", "90d"),
            "365d": analytics("install-on-request", "365d"),
        },
        "build_error_30d": analytics("build-error", "30d"),
    }


def collect_aur() -> dict[str, Any]:
    data = get_json("https://aur.archlinux.org/rpc/v5/info?arg[]=zeroclawlabs&arg[]=zeroclaw")
    return {"packages": sorted(data.get("results", []), key=lambda item: item.get("Name", ""))}


def collect_crates() -> dict[str, Any]:
    crates = []
    for crate_name in ["zeroclaw", "aardvark-sys"]:
        crate = get_json(f"https://crates.io/api/v1/crates/{crate_name}")
        downloads = get_json(f"https://crates.io/api/v1/crates/{crate_name}/downloads")
        rows = downloads.get("version_downloads", [])
        if rows:
            latest = max(dt.date.fromisoformat(row["date"]) for row in rows)
        else:
            latest = NOW.date()

        def window(days: int) -> int:
            start = latest - dt.timedelta(days=days - 1)
            return sum(
                int(row["downloads"])
                for row in rows
                if start <= dt.date.fromisoformat(row["date"]) <= latest
            )

        crates.append(
            {
                "name": crate_name,
                "description": crate["crate"].get("description"),
                "repository": crate["crate"].get("repository"),
                "downloads": crate["crate"]["downloads"],
                "recent_downloads": crate["crate"]["recent_downloads"],
                "max_version": crate["crate"]["max_version"],
                "latest_daily_date": latest.isoformat(),
                "last_7_downloads": window(7),
                "last_30_downloads": window(30),
                "last_90_downloads": window(90),
            }
        )
    return {"crates": crates}


def collect_scoop() -> dict[str, Any]:
    repo = gh_api("repos/zeroclaw-labs/scoop-zeroclaw")
    views = gh_api("repos/zeroclaw-labs/scoop-zeroclaw/traffic/views?per=day")
    clones = gh_api("repos/zeroclaw-labs/scoop-zeroclaw/traffic/clones?per=day")
    manifest_content = run(
        ["gh", "api", "repos/zeroclaw-labs/scoop-zeroclaw/contents/bucket/zeroclaw.json", "--jq", ".content"]
    )
    manifest = json.loads(subprocess.check_output(["base64", "-d"], input=manifest_content, text=True))
    return {
        "repo": {
            "stars": repo["stargazers_count"],
            "forks": repo["forks_count"],
            "updated_at": repo["updated_at"],
            "html_url": repo["html_url"],
        },
        "manifest_version": manifest.get("version"),
        "download_url": manifest.get("architecture", {}).get("64bit", {}).get("url"),
        "traffic": {
            "views_14d": views["count"],
            "views_uniques_14d": views["uniques"],
            "clones_14d": clones["count"],
            "clones_uniques_14d": clones["uniques"],
        },
    }


def collect_docker_hub() -> dict[str, Any]:
    official_candidates = []
    for namespace in ["zeroclaw-labs", "zeroclawlabs", "zeroclaw", "jordanthejet"]:
        name = f"{namespace}/zeroclaw"
        try:
            data = get_json(f"https://hub.docker.com/v2/repositories/{name}/")
            official_candidates.append(data)
        except urllib.error.HTTPError:
            continue
    search = get_json("https://hub.docker.com/v2/search/repositories/?query=zeroclaw&page_size=25")
    return {
        "official_candidates": official_candidates,
        "search_count": search.get("count"),
        "top_community_results": [
            {
                "repo_name": item.get("repo_name"),
                "pull_count": item.get("pull_count"),
                "star_count": item.get("star_count"),
                "short_description": item.get("short_description"),
            }
            for item in search.get("results", [])[:10]
        ],
    }


def safe_collect(name: str, fn) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fn()}
    except Exception as exc:  # Keep dashboard useful if one source flakes.
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return html.escape(str(value))


def compact(value: int | None) -> str:
    if value is None:
        return "n/a"
    value = int(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if cleaned and re.fullmatch(r"-?\d+", cleaned):
            return int(cleaned)
    return None


def delta(current: int | None, previous: int | None) -> int | None:
    if current is None or previous is None:
        return None
    return current - previous


def signed(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+,}"


def data_for(snapshot: dict[str, Any], source: str) -> dict[str, Any]:
    wrapped = snapshot.get(source, {})
    if isinstance(wrapped, dict) and wrapped.get("ok") and isinstance(wrapped.get("data"), dict):
        return wrapped["data"]
    return {}


def first_crate_downloads(crates_data: dict[str, Any], name: str) -> int | None:
    for item in crates_data.get("crates", []):
        if item.get("name") == name:
            return as_int(item.get("downloads"))
    return None


def release_bootstrap_downloads(row: dict[str, Any]) -> int | None:
    explicit = as_int(row.get("bootstrap_downloads"))
    if explicit is not None:
        return explicit
    mix = row.get("mix") if isinstance(row.get("mix"), dict) else {}
    return as_int(mix.get("Install script"))


def release_payload_downloads(row: dict[str, Any]) -> int | None:
    explicit = as_int(row.get("payload_downloads"))
    if explicit is not None:
        return explicit
    total = as_int(row.get("downloads"))
    bootstrap = release_bootstrap_downloads(row)
    if total is None:
        return None
    return total - (bootstrap or 0)


def release_payload_rate(row: dict[str, Any]) -> float | None:
    explicit = row.get("payload_downloads_per_week")
    if isinstance(explicit, int | float):
        return float(explicit)
    payload = release_payload_downloads(row)
    age_days = row.get("age_days")
    if payload is None or not isinstance(age_days, int | float) or age_days <= 0:
        return None
    return payload / (age_days / 7)


def release_payload_total(releases: dict[str, Any]) -> int | None:
    explicit = as_int(releases.get("payload_downloads_total"))
    if explicit is not None:
        return explicit
    total = as_int(releases.get("installable_downloads_total"))
    bootstrap = release_bootstrap_total(releases)
    if total is None:
        return None
    return total - (bootstrap or 0)


def release_bootstrap_total(releases: dict[str, Any]) -> int | None:
    explicit = as_int(releases.get("bootstrap_downloads_total"))
    if explicit is not None:
        return explicit
    asset_totals = releases.get("asset_totals") if isinstance(releases.get("asset_totals"), dict) else {}
    return as_int(asset_totals.get("Install script"))


def snapshot_point(snapshot: dict[str, Any]) -> dict[str, Any]:
    github = data_for(snapshot, "github_repo")
    ghcr = data_for(snapshot, "ghcr")
    releases = data_for(snapshot, "releases")
    homebrew = data_for(snapshot, "homebrew")
    crates = data_for(snapshot, "crates_io")

    generated_at = snapshot.get("generated_at", "")
    try:
        generated_day = parse_iso(generated_at).date().isoformat()
    except ValueError:
        generated_day = generated_at[:10]

    crates_total = sum(
        as_int(item.get("downloads")) or 0 for item in crates.get("crates", [])
    )
    homebrew_365d = as_int(
        ((homebrew.get("install") or {}).get("365d") or {}).get("count")
    )

    return {
        "generated_at": generated_at,
        "day": generated_day,
        "stars": as_int(github.get("stars")),
        "forks": as_int(github.get("forks")),
        "watchers": as_int(github.get("watchers")),
        "open_issues": as_int(github.get("open_issues")),
        "repo_views_14d": as_int(github.get("traffic", {}).get("views_14d")),
        "repo_clones_14d": as_int(github.get("traffic", {}).get("clones_14d")),
        "prs_merged_7d": as_int(github.get("pulse_7d", {}).get("prs_merged")),
        "issues_opened_7d": as_int(github.get("pulse_7d", {}).get("issues_opened")),
        "ghcr_downloads": as_int(ghcr.get("total_downloads")),
        "release_downloads": as_int(releases.get("installable_downloads_total")),
        "release_payload_downloads": release_payload_total(releases),
        "release_bootstrap_downloads": release_bootstrap_total(releases),
        "stable_release_downloads": as_int(releases.get("stable_downloads_total")),
        "homebrew_installs_365d": homebrew_365d,
        "crates_downloads": crates_total if crates.get("crates") else None,
        "zeroclaw_crate_downloads": first_crate_downloads(crates, "zeroclaw"),
        "aardvark_sys_crate_downloads": first_crate_downloads(crates, "aardvark-sys"),
    }


def load_snapshot_history() -> list[dict[str, Any]]:
    by_timestamp: dict[str, dict[str, Any]] = {}
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            snapshot = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        point = snapshot_point(snapshot)
        if point["generated_at"]:
            by_timestamp[point["generated_at"]] = point
    return sorted(by_timestamp.values(), key=lambda item: item["generated_at"])


def release_rows_from_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    releases = data_for(snapshot, "releases")
    by_tag: dict[str, dict[str, Any]] = {}
    for key in ["all_installable", "recent_stable", "top_by_rate"]:
        for row in releases.get(key, []):
            if row.get("tag") and row.get("published_at") and as_int(row.get("downloads")) is not None:
                by_tag[row["tag"]] = row
    return list(by_tag.values())


def load_release_history() -> dict[str, list[dict[str, Any]]]:
    history: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            snapshot = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        generated_at = snapshot.get("generated_at")
        if not generated_at:
            continue
        for row in release_rows_from_snapshot(snapshot):
            history[row["tag"]].append(
                {
                    "generated_at": generated_at,
                    "downloads": release_payload_downloads(row),
                    "published_at": row["published_at"],
                    "prerelease": bool(row.get("prerelease")),
                }
            )
    return {
        tag: sorted(points, key=lambda item: item["generated_at"])
        for tag, points in history.items()
    }


def history_delta(history: list[dict[str, Any]], key: str) -> tuple[int | None, int | None, int | None]:
    latest = history[-1].get(key) if history else None
    first = history[0].get(key) if history else None
    previous = history[-2].get(key) if len(history) > 1 else None
    return as_int(latest), delta(as_int(latest), as_int(first)), delta(as_int(latest), as_int(previous))


def observed_distribution_delta(history: list[dict[str, Any]], *, latest_interval: bool = False) -> int | None:
    keys = ["ghcr_downloads", "release_payload_downloads", "crates_downloads"]
    if len(history) < 2:
        return None
    previous = history[-2] if latest_interval else history[0]
    latest = history[-1]
    pieces = [delta(as_int(latest.get(key)), as_int(previous.get(key))) for key in keys]
    if any(piece is None for piece in pieces):
        return None
    return sum(piece or 0 for piece in pieces)


DAILY_CUMULATIVE_KEYS = [
    "ghcr_downloads",
    "release_downloads",
    "release_payload_downloads",
    "release_bootstrap_downloads",
    "crates_downloads",
    "stars",
    "forks",
    "watchers",
    "open_issues",
]

DAILY_ROLLING_KEYS = [
    "homebrew_installs_365d",
    "repo_views_14d",
    "repo_clones_14d",
    "prs_merged_7d",
    "issues_opened_7d",
]


def close_by_utc_day(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes: dict[str, dict[str, Any]] = {}
    for point in history:
        day = point.get("day")
        if day:
            closes[day] = point
    return [closes[day] for day in sorted(closes)]


def daily_metrics(history: list[dict[str, Any]]) -> dict[str, Any]:
    closes = close_by_utc_day(history)
    clone_history = observed_clone_history()
    rows = []
    for index, point in enumerate(closes):
        previous = closes[index - 1] if index > 0 else {}
        counters = {
            key: as_int(point.get(key))
            for key in [*DAILY_CUMULATIVE_KEYS, *DAILY_ROLLING_KEYS]
        }
        deltas = {
            key: delta(as_int(point.get(key)), as_int(previous.get(key)))
            for key in [*DAILY_CUMULATIVE_KEYS, *DAILY_ROLLING_KEYS]
        }
        distribution_delta_parts = [deltas.get(key) for key in ["ghcr_downloads", "release_payload_downloads", "crates_downloads"]]
        distribution_delta = None
        if all(part is not None for part in distribution_delta_parts):
            distribution_delta = sum(part or 0 for part in distribution_delta_parts)

        rows.append(
            {
                "day": point["day"],
                "snapshot_at": point["generated_at"],
                "counters": counters,
                "deltas": deltas,
                "aggregate_distribution_delta": distribution_delta,
            }
        )

    return {
        "generated_at": NOW.isoformat(),
        "source": "data/snapshots",
        "day_boundary": "UTC",
        "method": "latest snapshot per UTC day, diffed against prior UTC day close",
        "clone_history_method": "deduplicated GitHub traffic clone day rows from stored snapshots; cumulative values are observed clone events, not all-time GitHub totals",
        "cumulative_delta_keys": DAILY_CUMULATIVE_KEYS,
        "rolling_window_delta_keys": DAILY_ROLLING_KEYS,
        "clone_history": clone_history,
        "rows": rows,
    }


def json_blob(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def count_value(row: dict[str, Any] | None) -> int | None:
    if not row:
        return None
    return as_int(row.get("count"))


def load_snapshot_documents() -> list[tuple[Path, dict[str, Any]]]:
    documents = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            documents.append((path, json.loads(path.read_text())))
        except json.JSONDecodeError:
            continue
    return documents


def observed_clone_history() -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for path, snapshot in load_snapshot_documents():
        source_snapshot_at = snapshot.get("generated_at") or path.stem
        traffic = data_for(snapshot, "github_repo").get("traffic", {})
        for row in traffic.get("clones_daily", []):
            timestamp = row.get("timestamp")
            if not timestamp:
                continue
            day = str(timestamp)[:10]
            existing = by_day.get(day)
            if existing is not None and str(source_snapshot_at) < str(existing.get("source_snapshot_at") or ""):
                continue
            by_day[day] = {
                "day": day,
                "timestamp": timestamp,
                "count": as_int(row.get("count")),
                "uniques": as_int(row.get("uniques")),
                "source_snapshot_at": source_snapshot_at,
            }

    cumulative_count = 0
    cumulative_uniques_sum = 0
    rows = []
    for day in sorted(by_day):
        row = by_day[day]
        cumulative_count += as_int(row.get("count")) or 0
        cumulative_uniques_sum += as_int(row.get("uniques")) or 0
        rows.append(
            {
                **row,
                "cumulative_count": cumulative_count,
                "cumulative_uniques_sum": cumulative_uniques_sum,
            }
        )
    return rows


def build_sqlite_database(daily: dict[str, Any]) -> None:
    tmp_path = DATABASE_PATH.with_suffix(".sqlite.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE snapshots (
          snapshot_at TEXT PRIMARY KEY,
          day TEXT NOT NULL,
          repo TEXT,
          path TEXT NOT NULL,
          raw_json TEXT NOT NULL
        );
        CREATE TABLE source_status (
          snapshot_at TEXT NOT NULL,
          source TEXT NOT NULL,
          ok INTEGER NOT NULL,
          error TEXT,
          data_json TEXT,
          PRIMARY KEY (snapshot_at, source)
        );
        CREATE TABLE metric_points (
          snapshot_at TEXT NOT NULL,
          day TEXT NOT NULL,
          metric TEXT NOT NULL,
          value INTEGER,
          PRIMARY KEY (snapshot_at, metric)
        );
        CREATE TABLE daily_metrics (
          day TEXT PRIMARY KEY,
          snapshot_at TEXT NOT NULL,
          aggregate_distribution_delta INTEGER,
          counters_json TEXT NOT NULL,
          deltas_json TEXT NOT NULL
        );
        CREATE TABLE daily_deltas (
          day TEXT NOT NULL,
          metric TEXT NOT NULL,
          value INTEGER,
          PRIMARY KEY (day, metric)
        );
        CREATE TABLE observed_clone_history (
          day TEXT PRIMARY KEY,
          timestamp TEXT NOT NULL,
          count INTEGER,
          uniques INTEGER,
          cumulative_count INTEGER,
          cumulative_uniques_sum INTEGER,
          source_snapshot_at TEXT NOT NULL
        );
        CREATE TABLE release_totals (
          snapshot_at TEXT NOT NULL,
          tag TEXT NOT NULL,
          published_at TEXT,
          published_date TEXT,
          prerelease INTEGER NOT NULL,
          downloads INTEGER,
          payload_downloads INTEGER,
          bootstrap_downloads INTEGER,
          downloads_per_week REAL,
          payload_downloads_per_week REAL,
          age_days REAL,
          asset_count INTEGER,
          mix_json TEXT,
          PRIMARY KEY (snapshot_at, tag)
        );
        CREATE TABLE ghcr_daily_chart (
          snapshot_at TEXT NOT NULL,
          day TEXT NOT NULL,
          downloads INTEGER,
          PRIMARY KEY (snapshot_at, day)
        );
        CREATE TABLE ghcr_versions (
          snapshot_at TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          downloads INTEGER,
          digest TEXT,
          PRIMARY KEY (snapshot_at, tags_json, digest)
        );
        CREATE TABLE github_traffic (
          snapshot_at TEXT NOT NULL,
          kind TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          count INTEGER,
          uniques INTEGER,
          PRIMARY KEY (snapshot_at, kind, timestamp)
        );
        CREATE TABLE github_traffic_referrers (
          snapshot_at TEXT NOT NULL,
          rank INTEGER NOT NULL,
          referrer TEXT NOT NULL,
          count INTEGER,
          uniques INTEGER,
          PRIMARY KEY (snapshot_at, referrer)
        );
        CREATE TABLE github_traffic_paths (
          snapshot_at TEXT NOT NULL,
          rank INTEGER NOT NULL,
          path TEXT NOT NULL,
          title TEXT,
          count INTEGER,
          uniques INTEGER,
          PRIMARY KEY (snapshot_at, path)
        );
        CREATE TABLE homebrew_analytics (
          snapshot_at TEXT NOT NULL,
          kind TEXT NOT NULL,
          period TEXT NOT NULL,
          formula TEXT,
          count INTEGER,
          number INTEGER,
          percent TEXT,
          PRIMARY KEY (snapshot_at, kind, period)
        );
        CREATE TABLE crates (
          snapshot_at TEXT NOT NULL,
          name TEXT NOT NULL,
          version TEXT,
          downloads INTEGER,
          recent_downloads INTEGER,
          last_7_downloads INTEGER,
          last_30_downloads INTEGER,
          last_90_downloads INTEGER,
          latest_daily_date TEXT,
          description TEXT,
          repository TEXT,
          PRIMARY KEY (snapshot_at, name)
        );
        CREATE TABLE aur_packages (
          snapshot_at TEXT NOT NULL,
          name TEXT NOT NULL,
          version TEXT,
          votes INTEGER,
          popularity REAL,
          maintainer TEXT,
          raw_json TEXT NOT NULL,
          PRIMARY KEY (snapshot_at, name)
        );
        CREATE TABLE scoop_metrics (
          snapshot_at TEXT NOT NULL,
          metric TEXT NOT NULL,
          value TEXT,
          PRIMARY KEY (snapshot_at, metric)
        );
        CREATE TABLE docker_hub_results (
          snapshot_at TEXT NOT NULL,
          repo_name TEXT NOT NULL,
          pull_count INTEGER,
          star_count INTEGER,
          description TEXT,
          raw_json TEXT NOT NULL,
          PRIMARY KEY (snapshot_at, repo_name)
        );
        CREATE INDEX idx_metric_points_metric_day ON metric_points(metric, day);
        CREATE INDEX idx_daily_deltas_metric_day ON daily_deltas(metric, day);
        CREATE INDEX idx_release_totals_tag ON release_totals(tag, snapshot_at);
        """
    )

    conn.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("generated_at", NOW.isoformat()),
            ("repo", FULL_REPO),
            ("source", "data/snapshots"),
            ("method", "derived from immutable JSON snapshots"),
        ],
    )

    source_names = [
        "github_repo",
        "releases",
        "ghcr",
        "homebrew",
        "aur",
        "crates_io",
        "scoop",
        "docker_hub",
    ]
    for path, snapshot in load_snapshot_documents():
        generated_at = snapshot.get("generated_at")
        if not generated_at:
            continue
        day = snapshot_point(snapshot)["day"]
        conn.execute(
            "INSERT INTO snapshots(snapshot_at, day, repo, path, raw_json) VALUES (?, ?, ?, ?, ?)",
            (generated_at, day, snapshot.get("repo"), str(path.relative_to(ROOT)), json_blob(snapshot)),
        )

        for source in source_names:
            wrapped = snapshot.get(source, {})
            if isinstance(wrapped, dict):
                conn.execute(
                    """
                    INSERT INTO source_status(snapshot_at, source, ok, error, data_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        generated_at,
                        source,
                        1 if wrapped.get("ok") else 0,
                        wrapped.get("error"),
                        json_blob(wrapped.get("data")) if "data" in wrapped else None,
                    ),
                )

        point = snapshot_point(snapshot)
        for metric_name, value in point.items():
            if metric_name in {"generated_at", "day"}:
                continue
            int_value = as_int(value)
            if int_value is not None:
                conn.execute(
                    "INSERT INTO metric_points(snapshot_at, day, metric, value) VALUES (?, ?, ?, ?)",
                    (generated_at, day, metric_name, int_value),
                )

        for row in release_rows_from_snapshot(snapshot):
            conn.execute(
                """
                INSERT INTO release_totals(
                  snapshot_at, tag, published_at, published_date, prerelease,
                  downloads, payload_downloads, bootstrap_downloads,
                  downloads_per_week, payload_downloads_per_week,
                  age_days, asset_count, mix_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    row.get("tag"),
                    row.get("published_at"),
                    row.get("published_date"),
                    1 if row.get("prerelease") else 0,
                    as_int(row.get("downloads")),
                    as_int(row.get("payload_downloads")),
                    as_int(row.get("bootstrap_downloads")),
                    row.get("downloads_per_week"),
                    row.get("payload_downloads_per_week"),
                    row.get("age_days"),
                    as_int(row.get("asset_count")),
                    json_blob(row.get("mix", {})),
                ),
            )

        ghcr = data_for(snapshot, "ghcr")
        for row in ghcr.get("last_30_daily", []):
            conn.execute(
                "INSERT OR REPLACE INTO ghcr_daily_chart(snapshot_at, day, downloads) VALUES (?, ?, ?)",
                (generated_at, row.get("date"), as_int(row.get("downloads"))),
            )
        for row in ghcr.get("visible_versions", []):
            conn.execute(
                "INSERT OR REPLACE INTO ghcr_versions(snapshot_at, tags_json, downloads, digest) VALUES (?, ?, ?, ?)",
                (generated_at, json_blob(row.get("tags", [])), as_int(row.get("downloads")), row.get("digest")),
            )

        github = data_for(snapshot, "github_repo")
        traffic = github.get("traffic", {})
        for kind, rows in [("views", traffic.get("views_daily", [])), ("clones", traffic.get("clones_daily", []))]:
            for row in rows:
                conn.execute(
                    "INSERT OR REPLACE INTO github_traffic(snapshot_at, kind, timestamp, count, uniques) VALUES (?, ?, ?, ?, ?)",
                    (
                        generated_at,
                        kind,
                        row.get("timestamp"),
                        as_int(row.get("count")),
                        as_int(row.get("uniques")),
                    ),
                )
        for rank, row in enumerate(traffic.get("top_referrers", []), start=1):
            conn.execute(
                """
                INSERT OR REPLACE INTO github_traffic_referrers(snapshot_at, rank, referrer, count, uniques)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    rank,
                    row.get("referrer"),
                    as_int(row.get("count")),
                    as_int(row.get("uniques")),
                ),
            )
        for rank, row in enumerate(traffic.get("popular_paths", []), start=1):
            conn.execute(
                """
                INSERT OR REPLACE INTO github_traffic_paths(snapshot_at, rank, path, title, count, uniques)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    rank,
                    row.get("path"),
                    row.get("title"),
                    as_int(row.get("count")),
                    as_int(row.get("uniques")),
                ),
            )

        homebrew = data_for(snapshot, "homebrew")
        for kind in ["install", "install_on_request"]:
            for period, row in (homebrew.get(kind) or {}).items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO homebrew_analytics(snapshot_at, kind, period, formula, count, number, percent)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generated_at,
                        kind,
                        period,
                        row.get("formula"),
                        count_value(row),
                        as_int(row.get("number")),
                        row.get("percent"),
                    ),
                )
        build_error = homebrew.get("build_error_30d")
        if build_error:
            conn.execute(
                """
                INSERT OR REPLACE INTO homebrew_analytics(snapshot_at, kind, period, formula, count, number, percent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    "build_error",
                    "30d",
                    build_error.get("formula"),
                    count_value(build_error),
                    as_int(build_error.get("number")),
                    build_error.get("percent"),
                ),
            )

        for row in data_for(snapshot, "crates_io").get("crates", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO crates(
                  snapshot_at, name, version, downloads, recent_downloads,
                  last_7_downloads, last_30_downloads, last_90_downloads,
                  latest_daily_date, description, repository
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    row.get("name"),
                    row.get("max_version"),
                    as_int(row.get("downloads")),
                    as_int(row.get("recent_downloads")),
                    as_int(row.get("last_7_downloads")),
                    as_int(row.get("last_30_downloads")),
                    as_int(row.get("last_90_downloads")),
                    row.get("latest_daily_date"),
                    row.get("description"),
                    row.get("repository"),
                ),
            )

        for row in data_for(snapshot, "aur").get("packages", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO aur_packages(snapshot_at, name, version, votes, popularity, maintainer, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    row.get("Name"),
                    row.get("Version"),
                    as_int(row.get("NumVotes")),
                    row.get("Popularity"),
                    row.get("Maintainer"),
                    json_blob(row),
                ),
            )

        scoop = data_for(snapshot, "scoop")
        for metric_name, value in [
            ("manifest_version", scoop.get("manifest_version")),
            ("download_url", scoop.get("download_url")),
            ("repo_stars", scoop.get("repo", {}).get("stars")),
            ("repo_forks", scoop.get("repo", {}).get("forks")),
            ("traffic_views_14d", scoop.get("traffic", {}).get("views_14d")),
            ("traffic_clones_14d", scoop.get("traffic", {}).get("clones_14d")),
        ]:
            if value is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO scoop_metrics(snapshot_at, metric, value) VALUES (?, ?, ?)",
                    (generated_at, metric_name, str(value)),
                )

        for row in data_for(snapshot, "docker_hub").get("top_community_results", []):
            conn.execute(
                """
                INSERT OR REPLACE INTO docker_hub_results(snapshot_at, repo_name, pull_count, star_count, description, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    generated_at,
                    row.get("repo_name"),
                    as_int(row.get("pull_count")),
                    as_int(row.get("star_count")),
                    row.get("short_description"),
                    json_blob(row),
                ),
            )

    for row in daily.get("rows", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_metrics(day, snapshot_at, aggregate_distribution_delta, counters_json, deltas_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["day"],
                row["snapshot_at"],
                as_int(row.get("aggregate_distribution_delta")),
                json_blob(row.get("counters", {})),
                json_blob(row.get("deltas", {})),
            ),
        )
        for metric_name, value in row.get("deltas", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO daily_deltas(day, metric, value) VALUES (?, ?, ?)",
                (row["day"], metric_name, as_int(value)),
            )
        conn.execute(
            "INSERT OR REPLACE INTO daily_deltas(day, metric, value) VALUES (?, ?, ?)",
            (row["day"], "aggregate_distribution_delta", as_int(row.get("aggregate_distribution_delta"))),
        )

    for row in daily.get("clone_history", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO observed_clone_history(
              day, timestamp, count, uniques,
              cumulative_count, cumulative_uniques_sum, source_snapshot_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["day"],
                row["timestamp"],
                as_int(row.get("count")),
                as_int(row.get("uniques")),
                as_int(row.get("cumulative_count")),
                as_int(row.get("cumulative_uniques_sum")),
                row.get("source_snapshot_at"),
            ),
        )

    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp_path.replace(DATABASE_PATH)


def weekly_observed_release_rate(
    points: list[dict[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, Any]:
    window = [
        point
        for point in points
        if start <= parse_iso(point["generated_at"]) <= end and as_int(point.get("downloads")) is not None
    ]
    if len(window) < 2:
        return {"rate": None, "downloads": None, "days": None, "points": len(window)}
    first = window[0]
    last = window[-1]
    elapsed_days = (parse_iso(last["generated_at"]) - parse_iso(first["generated_at"])).total_seconds() / 86400
    if elapsed_days <= 0:
        return {"rate": None, "downloads": None, "days": None, "points": len(window)}
    downloads = (as_int(last["downloads"]) or 0) - (as_int(first["downloads"]) or 0)
    return {
        "rate": downloads / elapsed_days * 7,
        "downloads": downloads,
        "days": elapsed_days,
        "points": len(window),
    }


def release_velocity_rows(
    current_rows: list[dict[str, Any]],
    release_history: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    stable_rows = sorted(
        [row for row in current_rows if not row.get("prerelease")],
        key=lambda row: row["published_at"],
    )
    next_stable_by_tag = {
        row["tag"]: stable_rows[index + 1]["published_at"] if index + 1 < len(stable_rows) else None
        for index, row in enumerate(stable_rows)
    }

    out = []
    for row in sorted(stable_rows, key=lambda item: item["published_at"], reverse=True):
        published = parse_iso(row["published_at"])
        latest_until = parse_iso(next_stable_by_tag[row["tag"]]) if next_stable_by_tag[row["tag"]] else NOW
        points = release_history.get(row["tag"], [])
        first_21d = weekly_observed_release_rate(points, published, min(published + dt.timedelta(days=21), NOW))
        latest_stable = weekly_observed_release_rate(points, published, min(latest_until, NOW))
        out.append(
            {
                **row,
                "first_21d_observed": first_21d,
                "latest_stable_observed": latest_stable,
                "latest_stable_until": latest_until.isoformat(),
            }
        )
    return out


def rate_cell(rate: dict[str, Any]) -> str:
    if rate.get("rate") is None:
        points = rate.get("points")
        if points:
            return "n/a (1 point)"
        return "n/a"
    return f"{rate['rate']:.0f}"


def coverage_cell(rate: dict[str, Any]) -> str:
    if rate.get("days") is None:
        return "n/a"
    return f"{rate['downloads']:+,} over {rate['days']:.1f}d"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    body = "\n".join(
        "<tr>" + "".join(f"<td>{metric(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    return f"<div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def svg_bars(rows: list[dict[str, Any]], *, date_key: str, value_key: str, height: int = 92) -> str:
    if not rows:
        return "<p class=\"muted\">No daily data available.</p>"
    max_value = max(max(int(row[value_key]) for row in rows), 1)
    bar_gap = 3
    bar_width = 9
    width = len(rows) * (bar_width + bar_gap)
    bars = []
    for index, row in enumerate(rows):
        value = int(row[value_key])
        bar_height = max(2, round((value / max_value) * (height - 20)))
        x = index * (bar_width + bar_gap)
        y = height - bar_height - 14
        date = html.escape(str(row[date_key]))
        bars.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" rx="2">'
            f'<title>{date}: {value:,}</title></rect>'
        )
    return f'<svg class="bars" viewBox="0 0 {width} {height}" role="img" aria-label="Daily downloads">{"" .join(bars)}</svg>'


def svg_line(rows: list[dict[str, Any]], *, date_key: str, value_key: str, height: int = 118) -> str:
    points = [
        (str(row[date_key]), as_int(row.get(value_key)))
        for row in rows
        if as_int(row.get(value_key)) is not None
    ]
    if len(points) < 2:
        return "<p class=\"muted\">Not enough historical snapshots yet.</p>"

    values = [value for _, value in points if value is not None]
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 1)
    width = max(220, (len(points) - 1) * 48)
    left = 10
    right = width - 10
    top = 10
    bottom = height - 22
    x_step = (right - left) / max(len(points) - 1, 1)
    coords = []
    dots = []
    for index, (date, value) in enumerate(points):
        if value is None:
            continue
        x = left + index * x_step
        y = bottom - ((value - min_value) / span) * (bottom - top)
        coords.append(f"{x:.1f},{y:.1f}")
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3"><title>{html.escape(date)}: {value:,}</title></circle>'
        )
    return (
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(value_key)} over time">'
        f'<polyline points="{" ".join(coords)}"></polyline>{"".join(dots)}'
        f'<text x="{left}" y="{height - 4}">{html.escape(points[0][0])}</text>'
        f'<text x="{right}" y="{height - 4}" text-anchor="end">{html.escape(points[-1][0])}</text>'
        "</svg>"
    )


def render_dashboard(snapshot: dict[str, Any]) -> str:
    github = snapshot["github_repo"]
    releases = snapshot["releases"]
    ghcr = snapshot["ghcr"]
    homebrew = snapshot["homebrew"]
    aur = snapshot["aur"]
    crates = snapshot["crates_io"]
    scoop = snapshot["scoop"]
    docker_hub = snapshot["docker_hub"]

    release_data = releases["data"] if releases["ok"] else {}
    ghcr_data = ghcr["data"] if ghcr["ok"] else {}
    github_data = github["data"] if github["ok"] else {}
    homebrew_data = homebrew["data"] if homebrew["ok"] else {}
    aur_data = aur["data"] if aur["ok"] else {}
    crates_data = crates["data"] if crates["ok"] else {}
    scoop_data = scoop["data"] if scoop["ok"] else {}
    docker_hub_data = docker_hub["data"] if docker_hub["ok"] else {}

    history = load_snapshot_history()
    daily = daily_metrics(history)
    clone_history = daily.get("clone_history", [])
    clone_first_day = clone_history[0]["day"] if clone_history else None
    clone_latest_day = clone_history[-1]["day"] if clone_history else None
    observed_clone_events = clone_history[-1]["cumulative_count"] if clone_history else None
    release_history = load_release_history()
    first_point = history[0] if history else {}
    latest_point = history[-1] if history else {}
    tracking_days = 0.0
    if first_point.get("generated_at") and latest_point.get("generated_at"):
        tracking_days = max(
            (parse_iso(latest_point["generated_at"]) - parse_iso(first_point["generated_at"])).total_seconds() / 86400,
            0.0,
        )
    cards = [
        ("GHCR Downloads", compact(ghcr_data.get("total_downloads")), "Total container package downloads"),
        ("GHCR 30d", compact(ghcr_data.get("last_30_downloads")), "Scraped from authenticated package chart"),
        ("Prebuilt Payloads", compact(release_data.get("payload_downloads_total")), "Release assets excluding install.sh bootstrap"),
        ("Bootstrap Script", compact(release_data.get("bootstrap_downloads_total")), "install.sh release asset downloads"),
        ("Homebrew 30d", metric((homebrew_data.get("install", {}).get("30d") or {}).get("count")), "Homebrew Core installs"),
        ("Repo Stars", compact(github_data.get("stars")), "GitHub repository stars"),
        ("Traffic 14d", compact(github_data.get("traffic", {}).get("views_14d")), "Repository page views"),
        ("Clones 14d", compact(github_data.get("traffic", {}).get("clones_14d")), "Repository clones"),
        ("Observed Clones", compact(observed_clone_events), f"Clone events since {clone_first_day or 'first stored day'}"),
        ("PRs Merged 7d", metric(github_data.get("pulse_7d", {}).get("prs_merged")), "Pulse-like activity"),
        ("Snapshots", metric(len(history)), f"Tracking window {tracking_days:.1f} days"),
    ]

    current_release_rows = release_rows_from_snapshot(snapshot)
    release_velocity = release_velocity_rows(current_release_rows, release_history)
    release_rows = [
        [
            row["tag"],
            row["published_date"],
            f"{row['age_days']:.1f}d",
            release_payload_downloads(row),
            release_bootstrap_downloads(row),
            f"{release_payload_rate(row) or 0:.0f}",
            rate_cell(row["first_21d_observed"]),
            rate_cell(row["latest_stable_observed"]),
            coverage_cell(row["latest_stable_observed"]),
        ]
        for row in release_velocity[:12]
    ]

    ghcr_version_rows = [
        [", ".join(item["tags"]), item["downloads"], item.get("digest", "")[:19] + "..."]
        for item in ghcr_data.get("visible_versions", [])[:8]
    ]

    hb_rows = []
    for period in ["30d", "90d", "365d"]:
        install = (homebrew_data.get("install") or {}).get(period) or {}
        request = (homebrew_data.get("install_on_request") or {}).get(period) or {}
        hb_rows.append([period, install.get("count"), request.get("count")])

    aur_rows = [
        [
            item.get("Name"),
            item.get("Version"),
            item.get("NumVotes"),
            f"{item.get('Popularity', 0):.6f}",
            item.get("Maintainer"),
        ]
        for item in aur_data.get("packages", [])
    ]

    crate_rows = [
        [
            item["name"],
            item["max_version"],
            item["downloads"],
            item["last_7_downloads"],
            item["last_30_downloads"],
            item["last_90_downloads"],
        ]
        for item in crates_data.get("crates", [])
    ]

    pulse = github_data.get("pulse_7d", {})
    pulse_rows = [
        ["PRs opened", pulse.get("prs_opened")],
        ["PRs merged", pulse.get("prs_merged")],
        ["Issues opened", pulse.get("issues_opened")],
        ["Issues closed", pulse.get("issues_closed")],
        ["Default-branch commits", pulse.get("default_branch_commits")],
    ]

    contributor_rows = [
        [item["login"], item["last_week"], item["total"]]
        for item in github_data.get("top_contributors_latest_week", [])
    ]
    traffic = github_data.get("traffic", {})
    referrer_rows = [
        [item.get("referrer"), item.get("count"), item.get("uniques")]
        for item in traffic.get("top_referrers", [])[:10]
    ]
    popular_path_rows = [
        [item.get("path"), item.get("title"), item.get("count"), item.get("uniques")]
        for item in traffic.get("popular_paths", [])[:10]
    ]

    docker_rows = [
        [item["repo_name"], item["pull_count"], item["star_count"], item.get("short_description") or ""]
        for item in docker_hub_data.get("top_community_results", [])[:8]
    ]

    cumulative_metrics = [
        ("GHCR downloads", "ghcr_downloads", "Cumulative GitHub Container Registry package counter"),
        ("Prebuilt release payload downloads", "release_payload_downloads", "Cumulative GitHub release payload asset counter"),
        ("Bootstrap install.sh downloads", "release_bootstrap_downloads", "Cumulative bootstrap script release asset counter"),
        ("crates.io downloads", "crates_downloads", "Cumulative crate download counters"),
        ("Repo stars", "stars", "Current GitHub repository stars"),
        ("Repo forks", "forks", "Current GitHub repository forks"),
    ]
    growth_rows = []
    for label, key, note in cumulative_metrics:
        current = latest_point.get(key) if latest_point else None
        growth_rows.append([label, current, note])

    clone_history_rows = [
        [
            row["day"],
            row.get("count"),
            row.get("uniques"),
            row.get("cumulative_count"),
        ]
        for row in clone_history[-14:]
    ]

    rolling_metric_rows = []
    for label, key, note in [
        ("Homebrew installs 365d", "homebrew_installs_365d", "Rolling 365-day Homebrew install events"),
        ("Repo views 14d", "repo_views_14d", "Rolling 14-day GitHub repository views"),
        ("Repo clones 14d", "repo_clones_14d", "Rolling 14-day GitHub repository clone events"),
        ("PRs merged 7d", "prs_merged_7d", "Rolling 7-day merged pull requests"),
        ("Issues opened 7d", "issues_opened_7d", "Rolling 7-day opened issues"),
    ]:
        current = latest_point.get(key) if latest_point else None
        rolling_metric_rows.append([label, current, note])

    methodology_rows = [
        ["GitHub stars/forks", "CHAOSS-style project popularity", "Cumulative", "GitHub repository metadata counters."],
        ["GitHub views/clones", "Attention and source-install proxy", "Rolling 14d", "GitHub Insights traffic API; no lifetime clone/view total is exposed."],
        ["Observed clone history", "Source-install adoption proxy", "Observed cumulative", "Deduped daily clone rows stored from the rolling GitHub traffic API."],
        ["Referrers and paths", "Discovery channels", "Rolling top 10", "GitHub Insights top referrers and popular content for the recent traffic window."],
        ["GHCR downloads", "Container distribution", "Cumulative plus chart window", "Authenticated package UI counters; REST package objects omit pulls."],
        ["Release payload assets", "Binary distribution", "Cumulative", "GitHub release asset counters, excluding install.sh bootstrap downloads."],
        ["Homebrew installs", "Package-manager adoption", "Rolling 30d/90d/365d", "Homebrew anonymous install analytics, not lifetime downloads."],
    ]

    status_rows = [
        ["GitHub repo + traffic", "ok" if github["ok"] else github.get("error")],
        ["GitHub releases", "ok" if releases["ok"] else releases.get("error")],
        ["GHCR package UI", "ok" if ghcr["ok"] else ghcr.get("error")],
        ["Homebrew", "ok" if homebrew["ok"] else homebrew.get("error")],
        ["AUR", "ok" if aur["ok"] else aur.get("error")],
        ["crates.io", "ok" if crates["ok"] else crates.get("error")],
        ["Scoop", "ok" if scoop["ok"] else scoop.get("error")],
        ["Docker Hub search", "ok" if docker_hub["ok"] else docker_hub.get("error")],
    ]

    css = """
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #667085;
      --line: #d8dee8;
      --blue: #2563eb;
      --green: #168a5b;
      --amber: #b7791f;
      --red: #c24132;
      --teal: #0f766e;
      --shadow: 0 1px 2px rgb(16 24 40 / 7%);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      background: #17202a;
      color: white;
      padding: 28px max(24px, calc((100vw - 1180px) / 2));
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 28px; font-weight: 700; letter-spacing: 0; }
    header p { color: #d6dde8; margin-top: 8px; max-width: 840px; }
    main {
      width: min(1180px, calc(100% - 32px));
      margin: 24px auto 48px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .card, section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }
    .card {
      min-height: 112px;
      padding: 16px;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
      font-weight: 700;
    }
    .value {
      display: block;
      margin: 8px 0 4px;
      font-size: 30px;
      line-height: 1.05;
      font-weight: 760;
      white-space: nowrap;
    }
    .note, .muted { color: var(--muted); }
    .note { font-size: 13px; }
    section {
      margin-top: 16px;
      padding: 18px;
    }
    .section-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 12px;
    }
    h2 { font-size: 18px; }
    .split {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(300px, .9fr);
      gap: 16px;
      align-items: start;
    }
    .table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; min-width: 560px; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { background: #f1f4f8; color: #344054; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
    tr:last-child td { border-bottom: 0; }
    .bars {
      width: 100%;
      height: 108px;
      display: block;
      overflow: visible;
    }
    .bars rect { fill: var(--teal); }
    .bars rect:nth-child(3n) { fill: var(--blue); }
    .bars rect:nth-child(5n) { fill: var(--green); }
    .line-chart {
      width: 100%;
      height: 132px;
      display: block;
      overflow: visible;
    }
    .line-chart polyline {
      fill: none;
      stroke: var(--blue);
      stroke-width: 3;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    .line-chart circle { fill: var(--green); stroke: white; stroke-width: 1.5; }
    .line-chart text { fill: var(--muted); font-size: 11px; }
    .callout {
      border-left: 4px solid var(--amber);
      padding: 12px 14px;
      background: #fff8e8;
      border-radius: 6px;
      margin-top: 12px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      background: #eef2ff;
      color: #3342a0;
      font-size: 12px;
      font-weight: 700;
      margin-right: 6px;
      margin-top: 6px;
    }
    footer {
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto 32px;
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 960px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .split { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      header { padding: 22px 16px; }
      main { width: calc(100% - 24px); margin-top: 16px; }
      .grid { grid-template-columns: 1fr; }
      .value { font-size: 26px; }
      section { padding: 14px; }
      .section-head { display: block; }
      .section-head .muted { margin-top: 4px; }
    }
    """

    cards_html = "\n".join(
        f'<article class="card"><span class="label">{html.escape(title)}</span>'
        f'<span class="value">{value}</span><p class="note">{html.escape(note)}</p></article>'
        for title, value, note in cards
    )

    ghcr_daily_rows = ghcr_data.get("last_30_daily", [])
    repo_view_rows = github_data.get("traffic", {}).get("views_daily", [])
    repo_clone_rows = github_data.get("traffic", {}).get("clones_daily", [])

    release_mix = release_data.get("asset_totals", {})
    mix_rows = [
        [name, count, f"{count / max(sum(release_mix.values()), 1) * 100:.1f}%"]
        for name, count in sorted(release_mix.items(), key=lambda item: item[1], reverse=True)
    ]

    error_banner = ""
    failed = [name for name, wrapped in snapshot.items() if isinstance(wrapped, dict) and wrapped.get("ok") is False]
    if failed:
        error_banner = (
            '    <section><h2>Partial Data</h2><p class="callout">'
            f'Some sources failed: {html.escape(", ".join(failed))}. See source status for details.'
            "</p></section>\n"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ZeroClaw Metrics Dashboard</title>
  <style>{css}</style>
</head>
<body>
  <header>
    <h1>ZeroClaw Metrics Dashboard</h1>
    <p>Distribution, release, repository, and package-manager metrics snapshot for {html.escape(FULL_REPO)}. Generated {html.escape(snapshot["generated_at"])}.</p>
  </header>
  <main>
{error_banner}    <div class="grid">{cards_html}</div>

	    <section>
	      <div class="section-head">
	        <h2>Current Scale</h2>
	        <p class="muted">Latest snapshot values from {metric(len(history))} immutable snapshots in <code>data/snapshots/</code></p>
	      </div>
	      <div class="split">
	        <div>
	          {table(["Metric", "Current", "Meaning"], growth_rows)}
	          <p class="callout">Snapshot-to-snapshot change columns are intentionally excluded from the main dashboard because this tracking repository started recently and short-interval movement understates project scale. Raw day-by-day change rows remain available in <code>data/daily.json</code> and <code>data/metrics.sqlite</code>.</p>
	        </div>
	        <div>
	          <h3>GHCR cumulative downloads</h3>
	          {svg_line(history, date_key="day", value_key="ghcr_downloads")}
	        </div>
	      </div>
	      <h3 style="margin-top:16px;">Rolling windows</h3>
	      <p class="note">Homebrew and GitHub traffic/pulse APIs expose rolling windows, not lifetime totals. These rows show the latest reported window value only.</p>
	      {table(["Metric", "Current window", "Window"], rolling_metric_rows)}
	      <h3 style="margin-top:16px;">Observed clone history</h3>
	      <p class="note">GitHub does not expose lifetime clone totals. This table deduplicates the stored daily rows from the rolling 14-day traffic API, so the cumulative count starts at {html.escape(clone_first_day or "the first stored clone day")} and currently ends at {html.escape(clone_latest_day or "n/a")}. Daily uniques are not globally deduplicated.</p>
	      {table(["UTC day", "Clone events", "Daily unique cloners", "Observed cumulative"], clone_history_rows)}
	    </section>

    <section>
      <div class="section-head">
        <h2>GHCR Container Downloads</h2>
        <p class="muted">Total {metric(ghcr_data.get("total_downloads"))}; last 7 days {metric(ghcr_data.get("last_7_downloads"))}</p>
      </div>
      <div class="split">
        <div>
          {svg_bars(ghcr_daily_rows, date_key="date", value_key="downloads")}
          <p class="note">Daily values are scraped from the authenticated GitHub Packages chart because REST package objects omit pull/download counts.</p>
        </div>
        <div>
          {table(["Tags", "Downloads", "Digest"], ghcr_version_rows)}
        </div>
      </div>
    </section>

    <section>
      <div class="section-head">
        <h2>GitHub Release Assets</h2>
        <p class="muted">Prebuilt payloads are separated from install.sh bootstrap downloads</p>
      </div>
      <div class="split">
	        <div>{table(["Release", "Published", "Age", "Payload downloads", "install.sh", "Payload avg/wk", "Observed first 21d/wk", "Observed latest-stable/wk", "Observed latest-stable window"], release_rows)}</div>
        <div>{table(["Asset category", "Downloads", "Share"], mix_rows)}</div>
      </div>
      <p class="callout"><code>install.sh</code> is a bootstrap path: default installs clone and build the repo, while <code>--prebuilt</code> downloads release payload assets. Treat repo clone traffic as the better proxy for source-build installs. First-21-day and latest-stable rates are computed only from stored snapshots.</p>
    </section>

	    <section>
	      <div class="section-head">
	        <h2>Repository Activity</h2>
	        <p class="muted">Pulse-like window since {html.escape(str(pulse.get("since", "n/a")))}</p>
	      </div>
      <div class="split">
        <div>
          <h3>Views</h3>
          {svg_bars(repo_view_rows, date_key="timestamp", value_key="count")}
          <h3 style="margin-top:16px;">Clones</h3>
          {svg_bars(repo_clone_rows, date_key="timestamp", value_key="count")}
          <p class="note">Views: {metric(github_data.get("traffic", {}).get("views_14d"))} total, {metric(github_data.get("traffic", {}).get("views_uniques_14d"))} unique. Clones: {metric(github_data.get("traffic", {}).get("clones_14d"))} total, {metric(github_data.get("traffic", {}).get("clones_uniques_14d"))} unique.</p>
        </div>
	        <div>
	          {table(["Pulse metric", "Count"], pulse_rows)}
	          {table(["Contributor", "Latest week commits", "Total commits"], contributor_rows)}
	        </div>
	      </div>
	      <h3 style="margin-top:16px;">Top referrers</h3>
	      {table(["Referrer", "Views", "Unique visitors"], referrer_rows)}
	      <h3 style="margin-top:16px;">Popular content</h3>
	      {table(["Path", "Title", "Views", "Unique visitors"], popular_path_rows)}
	    </section>

    <section>
      <div class="section-head">
        <h2>Package Managers</h2>
        <p class="muted">External package surfaces with usable metrics</p>
      </div>
      <div class="split">
        <div>
          <h3>Homebrew Core</h3>
          {table(["Window", "Installs", "Install on request"], hb_rows)}
          <h3 style="margin-top:16px;">crates.io</h3>
          {table(["Crate", "Version", "Total", "7d", "30d", "90d"], crate_rows)}
        </div>
        <div>
          <h3>AUR</h3>
          {table(["Package", "Version", "Votes", "Popularity", "Maintainer"], aur_rows)}
          <h3 style="margin-top:16px;">Scoop Bucket</h3>
          {table(["Metric", "Value"], [
              ["Manifest version", scoop_data.get("manifest_version")],
              ["Repo views 14d", scoop_data.get("traffic", {}).get("views_14d")],
              ["Repo clones 14d", scoop_data.get("traffic", {}).get("clones_14d")],
              ["Stars", scoop_data.get("repo", {}).get("stars")],
          ])}
        </div>
      </div>
      <p class="callout">Do not add package-manager counts together as unique users. Homebrew, Scoop, and installers can ultimately fetch GitHub release assets, and GHCR counts image pulls rather than people.</p>
    </section>

    <section>
      <div class="section-head">
        <h2>Docker Hub Search</h2>
        <p class="muted">No official Docker Hub image found; these are community results</p>
      </div>
      {table(["Repository", "Pulls", "Stars", "Description"], docker_rows)}
    </section>

	    <section>
	      <div class="section-head">
	        <h2>Recommended Permanent Home</h2>
	        <p class="muted">Keep snapshots separate from source code</p>
      </div>
      <p>This repository is the durable home for ZeroClaw metrics. The collector runs from <code>scripts/build_dashboard.py</code>, writes immutable snapshots under <code>data/snapshots/</code>, refreshes <code>data/latest.json</code>, derives <code>data/daily.json</code> and <code>data/metrics.sqlite</code>, and publishes this static <code>index.html</code> through GitHub Pages. The main application repository should link here rather than storing metrics snapshots in product source.</p>
      <div>
        <span class="pill">Collector: scripts/metrics</span>
        <span class="pill">Snapshots: separate metrics store</span>
        <span class="pill">SQLite: data/metrics.sqlite</span>
        <span class="pill">Dashboard: GitHub Pages /metrics</span>
        <span class="pill">Docs link: maintainer docs</span>
      </div>
	    </section>

	    <section>
	      <div class="section-head">
	        <h2>Methodology</h2>
	        <p class="muted">Investor-readable metric definitions and caveats</p>
	      </div>
	      {table(["Metric family", "Meaning", "Window", "Method"], methodology_rows)}
	      <p class="callout">This dashboard follows a snapshot-first model aligned with CHAOSS-style project health reporting: raw platform responses are preserved under <code>data/snapshots/</code>, and aggregate JSON, SQLite, and HTML views are regenerated from those snapshots.</p>
	    </section>

	    <section>
      <div class="section-head">
        <h2>Source Status</h2>
        <p class="muted">Collector health for this snapshot</p>
      </div>
      {table(["Source", "Status"], status_rows)}
    </section>
  </main>
  <footer>
    Source snapshot: <code>data/latest.json</code>. Daily diffs: <code>data/daily.json</code>. Query database: <code>data/metrics.sqlite</code>. The live platforms remain canonical; this dashboard is a point-in-time operational view.
  </footer>
</body>
</html>
"""


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": NOW.isoformat(),
        "repo": FULL_REPO,
        "github_repo": safe_collect("github_repo", collect_github_repo),
        "releases": safe_collect("releases", collect_releases),
        "ghcr": safe_collect("ghcr", collect_ghcr),
        "homebrew": safe_collect("homebrew", collect_homebrew),
        "aur": safe_collect("aur", collect_aur),
        "crates_io": safe_collect("crates_io", collect_crates),
        "scoop": safe_collect("scoop", collect_scoop),
        "docker_hub": safe_collect("docker_hub", collect_docker_hub),
        "notes": [
            "GHCR download counts are scraped from authenticated GitHub package HTML because REST objects omit those fields.",
            "GitHub release downloads are cumulative per asset; downloads/week is an average since release publication.",
            "install.sh is a bootstrap asset; default source installs clone and build the repo, so clone traffic is the better proxy for that path.",
            "Traffic endpoints expose only the last 14 days; observed clone history is built from stored daily rows, not a GitHub lifetime total.",
            "GitHub top referrers and popular paths are rolling-window Insights metrics and are snapshot daily.",
            "Scoop installs ultimately hit GitHub release assets, so avoid double-counting with release downloads.",
            "npm packages named zeroclaw/zerocode are unrelated and intentionally excluded.",
        ],
    }
    snapshot_text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    snapshot_name = NOW.isoformat(timespec="seconds").replace(":", "-").replace("+", "-") + ".json"
    (SNAPSHOT_DIR / snapshot_name).write_text(snapshot_text)
    LATEST_PATH.write_text(snapshot_text)
    daily = daily_metrics(load_snapshot_history())
    DAILY_PATH.write_text(json.dumps(daily, indent=2, sort_keys=True) + "\n")
    build_sqlite_database(daily)
    INDEX_PATH.write_text(render_dashboard(snapshot))
    print(INDEX_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
