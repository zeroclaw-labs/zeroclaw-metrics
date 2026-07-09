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

## Update Model

The `Collect Metrics` GitHub Actions workflow is the primary updater. It runs
daily, calls `scripts/build_dashboard.py`, commits a new timestamped snapshot
under `data/snapshots/`, refreshes `data/latest.json`, rebuilds `index.html`,
and publishes the result to GitHub Pages.

The dashboard's historical deltas are generated from `data/snapshots/` at build
time. There is no second aggregate database; the immutable snapshots are the
stored history.

`data/daily.json` and `data/metrics.sqlite` are derived views. Daily rows use
the latest snapshot for each UTC day as that day's close, then compute deltas
against the prior UTC day close. The SQLite database also includes raw snapshot
JSON and normalized tables for common queries. Clone history appears in
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
```

A remote ZeroClaw cron job can be used as an operational watchdog. It should
dispatch the same `Collect Metrics` workflow when the published snapshot is
stale rather than maintaining a separate metrics store.

## Sources

- GitHub Releases asset download counters.
- GitHub Container Registry package page download counters.
- GitHub repo counters, traffic, search, commit, and contributor endpoints.
- Homebrew Formulae analytics.
- AUR RPC package metadata.
- crates.io crate download endpoints.
- Scoop bucket repository traffic.
- Docker Hub search, used only to confirm no official Docker Hub image exists.

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
- Do not add package-manager counts together as unique users. Homebrew, Scoop,
  installers, and release assets can overlap.
- npm packages named `zeroclaw` or `zerocode` are unrelated and excluded.
