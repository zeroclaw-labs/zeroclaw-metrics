# ZeroClaw Metrics

Distribution and repository metrics dashboard for
[`zeroclaw-labs/zeroclaw`](https://github.com/zeroclaw-labs/zeroclaw).

The live services remain the source of truth. This repository stores
timestamped, point-in-time snapshots so trends can be computed over time.

## Contents

- `index.html` - static dashboard for GitHub Pages.
- `data/latest.json` - latest metrics snapshot.
- `data/daily.json` - generated day-by-day close and delta view.
- `data/metrics.sqlite` - generated SQLite database for ad hoc queries.
- `data/snapshots/` - immutable historical snapshots.
- `scripts/build_dashboard.py` - collector and dashboard renderer.
- `tests/` - collector parsing and metric-definition regression tests.

## Update Model

The `Collect Metrics` GitHub Actions workflow is the primary updater. It runs
daily, calls `scripts/build_dashboard.py`, commits a new timestamped snapshot
under `data/snapshots/`, refreshes `data/latest.json`, rebuilds `index.html`,
and publishes the result to GitHub Pages.

The dashboard's historical deltas are generated from `data/snapshots/` at build
time. There is no second aggregate database; the immutable snapshots are the
stored history.

`data/daily.json` and `data/metrics.sqlite` are derived views. Daily rows use
the most complete snapshot for each UTC day, preferring the latest on ties, then
compute deltas against the prior UTC day close. The SQLite database also includes
snapshot JSON and normalized tables for common queries. Clone history appears in
`data/daily.json` and the `observed_clone_history` SQLite table by deduplicating
daily clone rows from the stored GitHub traffic snapshots.

Example SQLite queries:

```sql
select day, value
from daily_deltas
where metric = 'aggregate_distribution_delta'
order by day;

select snapshot_at, tag, downloads
from release_totals
where tag = 'v0.8.2'
order by snapshot_at;

select day, count, cumulative_count
from observed_clone_history
order by day;

select snapshot_at, referrer, count, uniques
from github_traffic_referrers
order by snapshot_at desc, rank;
```

A remote ZeroClaw cron job can be used as an operational watchdog. It should
dispatch the same `Collect Metrics` workflow when the published snapshot is
stale rather than maintaining a separate metrics store.

## Sources

- GitHub Releases asset download counters.
- GitHub Container Registry package page download counters.
- GitHub repo counters, traffic, referrer, popular-path, search, commit, and
  contributor endpoints.
- Homebrew Formulae analytics.
- AUR RPC package metadata.
- crates.io crate download endpoints.
- Scoop bucket repository traffic.
- Docker Hub search, used only to confirm no official Docker Hub image exists.

## Methodology

This dashboard uses a snapshot-first model aligned with CHAOSS project-health
reporting. Versioned, normalized source records are stored under
`data/snapshots/`; they are not byte-for-byte API response archives. The
dashboard, `data/daily.json`, and `data/metrics.sqlite` are regenerated from
those snapshots.

Open standards and external frameworks used for vocabulary:

- The CHAOSS Starter Project Health model for time to first human response,
  change-request closure throughput, contributor absence factor, and release
  frequency.
- CHAOSS Number of Downloads and Clones vocabulary for adoption and reach.
- GitHub REST traffic windows for repository views, clones, referrers, and
  popular content.

## Token Setup

The scheduled workflow expects a repository secret named
`ZEROCLAW_METRICS_TOKEN`.

For the current collector, the token needs:

- `read:packages` for GHCR package access.
- Access to `zeroclaw-labs/zeroclaw` with permission to read traffic metrics.
- Normal public read access for releases, search, repo metadata, AUR, Homebrew,
  crates.io, and Docker Hub.

The workflow's default `GITHUB_TOKEN` is enough to commit refreshed snapshots
back to this repository, but it is not enough for all upstream ZeroClaw metrics.

## Caveats

- GHCR download counts are scraped from authenticated GitHub package HTML
  because REST package objects omit those fields.
- GitHub release downloads are cumulative per asset; downloads/week is an
  average since release publication unless computed from stored snapshots.
- `install.sh` is tracked as a bootstrap release asset, not a payload install.
  The default source-install path clones and builds the repository, so GitHub
  repo clone traffic is the better proxy for that path.
- Distribution deltas exclude `install.sh` and include prebuilt release payload
  downloads instead, alongside GHCR and crates.io.
- Release velocity comparisons use stored snapshots for observed first-21-day
  and latest-stable windows. Windows that predate this repository's snapshots
  are intentionally shown as unavailable instead of inferred.
- Homebrew exposes anonymous install analytics over rolling 30d, 90d, and 365d
  windows. These are install event counts, not download counts or lifetime
  totals.
- GitHub traffic endpoints expose only the last 14 days. GitHub does not expose
  an all-time clone total. Observed clone history is a cumulative clone-event
  count built from stored daily traffic rows; it cannot backfill traffic before
  the first saved rows, and summed daily uniques are not globally unique users.
- GitHub referrers and popular paths are top-10 rolling-window Insights metrics,
  not complete attribution logs.
- Seven-day GitHub activity windows use exact UTC timestamps, while CHAOSS
  starter-health metrics use an exact rolling 28-day window.
- First-response metrics exclude bots and the issue or pull-request author. The
  first 20 comments and reviews are inspected for each item; this policy is
  recorded in every snapshot. Unanswered items less than 48 hours old remain
  pending and are excluded from the 48-hour service-level denominator.
- Change-request closure throughput is pull requests closed during the window
  divided by pull requests opened during the window. It can exceed 100% when a
  project reduces an older backlog.
- Contributor absence factor is based on non-bot default-branch commit authors,
  not every form of community contribution.
- Core GitHub, release, and GHCR fields are validated before publication. A core
  failure stops the workflow rather than replacing the latest good dashboard.
- Do not add package-manager counts together as unique users. Homebrew, Scoop,
  installers, and release assets can overlap.
- npm packages named `zeroclaw` or `zerocode` are unrelated and excluded.
