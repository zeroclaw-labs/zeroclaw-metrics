# ZeroClaw Metrics

Distribution and repository metrics dashboard for
[`zeroclaw-labs/zeroclaw`](https://github.com/zeroclaw-labs/zeroclaw).

The live services remain the source of truth. This repository stores
timestamped, point-in-time snapshots so trends can be computed over time.

## Contents

- `index.html` - static dashboard for GitHub Pages.
- `data/latest.json` - latest metrics snapshot.
- `data/snapshots/` - immutable historical snapshots.
- `scripts/build_dashboard.py` - collector and dashboard renderer.

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
- GitHub traffic endpoints expose only the last 14 days.
- Do not add package-manager counts together as unique users. Homebrew, Scoop,
  installers, and release assets can overlap.
- npm packages named `zeroclaw` or `zerocode` are unrelated and excluded.
