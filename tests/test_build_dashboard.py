import datetime as dt
import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_dashboard.py"
SPEC = importlib.util.spec_from_file_location("build_dashboard", SCRIPT_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(dashboard)


class GhcrParserTests(unittest.TestCase):
    def test_parses_required_counters_and_versions(self):
        source = """
        <span>Total downloads</span><h3 title="12345">12.3K</h3>
        <div data-merge-count="7" data-date="2026-07-10"></div>
        <div data-merge-count="9" data-date="2026-07-11"></div>
        <li class="Box-row">
          <a href="?tag=latest">latest</a>
          <svg class="octicon-download"></svg> 1,234 <span class="sr-only">Version downloads</span>
          <input value="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
        </li>
        """
        parsed = dashboard.parse_ghcr_html(source)
        self.assertEqual(12345, parsed["total_downloads"])
        self.assertEqual(16, parsed["last_30_downloads"])
        self.assertEqual(1234, parsed["visible_versions"][0]["downloads"])

    def test_rejects_markup_without_required_counter(self):
        with self.assertRaisesRegex(ValueError, "total download counter"):
            dashboard.parse_ghcr_html('<div data-merge-count="1" data-date="2026-07-11"></div>')


class ChaossMetricTests(unittest.TestCase):
    def test_response_excludes_author_and_bots(self):
        item = {
            "createdAt": "2026-07-10T00:00:00Z",
            "closedAt": "2026-07-11T00:00:00Z",
            "author": {"login": "author", "__typename": "User"},
            "comments": {
                "nodes": [
                    {"createdAt": "2026-07-10T01:00:00Z", "author": {"login": "author", "__typename": "User"}},
                    {"createdAt": "2026-07-10T02:00:00Z", "author": {"login": "ci[bot]", "__typename": "Bot"}},
                    {"createdAt": "2026-07-10T03:00:00Z", "author": {"login": "maintainer", "__typename": "User"}},
                ]
            },
            "reviews": {"nodes": []},
        }
        summary = dashboard.summarize_responsiveness([item])
        self.assertEqual(3.0, summary["median_first_response_hours"])
        self.assertEqual(100.0, summary["responded_within_48h_pct"])
        self.assertEqual(24.0, summary["median_time_to_close_hours"])

    def test_late_unanswered_items_remain_in_48_hour_denominator(self):
        answered = {
            "createdAt": "2026-07-01T00:00:00Z",
            "closedAt": None,
            "author": {"login": "a", "__typename": "User"},
            "comments": {"nodes": [{"createdAt": "2026-07-01T01:00:00Z", "author": {"login": "b", "__typename": "User"}}]},
        }
        unanswered = {
            "createdAt": "2026-07-01T00:00:00Z",
            "closedAt": None,
            "author": {"login": "c", "__typename": "User"},
            "comments": {"nodes": []},
        }
        summary = dashboard.summarize_responsiveness([answered, unanswered])
        self.assertEqual(50.0, summary["responded_within_48h_pct"])
        self.assertEqual(1, summary["unanswered"])

    def test_recent_unanswered_item_is_pending_not_late(self):
        created = (dashboard.NOW - dt.timedelta(hours=12)).isoformat()
        pending = {
            "createdAt": created,
            "closedAt": None,
            "author": {"login": "new", "__typename": "User"},
            "comments": {"nodes": []},
        }
        summary = dashboard.summarize_responsiveness([pending])
        self.assertEqual(1, summary["pending_within_48h"])
        self.assertEqual(0, summary["response_sla_eligible"])
        self.assertIsNone(summary["responded_within_48h_pct"])

    def test_contributor_absence_factor_reaches_half_of_commits(self):
        self.assertEqual(2, dashboard.contributor_absence_factor({"a": 40, "b": 20, "c": 20, "d": 20}))
        self.assertIsNone(dashboard.contributor_absence_factor({}))


class SnapshotTests(unittest.TestCase):
    def test_daily_close_prefers_completeness_then_recency(self):
        history = [
            {"day": "2026-07-11", "generated_at": "2026-07-11T08:00:00Z", "stars": 10, "forks": 2},
            {"day": "2026-07-11", "generated_at": "2026-07-11T09:00:00Z", "stars": 11, "forks": None},
            {"day": "2026-07-12", "generated_at": "2026-07-12T08:00:00Z", "stars": 12, "forks": 3},
        ]
        closes = dashboard.close_by_utc_day(history)
        self.assertEqual("2026-07-11T08:00:00Z", closes[0]["generated_at"])
        self.assertEqual("2026-07-12", closes[1]["day"])

    def test_core_validation_rejects_missing_required_field(self):
        with self.assertRaisesRegex(ValueError, "traffic.clones_14d"):
            dashboard.validate_source_data(
                "github_repo",
                {
                    "stars": 1,
                    "open_issues": 2,
                    "open_pull_requests": 3,
                    "traffic": {"views_14d": 4},
                },
            )

    def test_asset_classification_excludes_signatures(self):
        self.assertTrue(dashboard.is_payload_asset("zeroclaw-x86_64-unknown-linux-gnu.tar.gz"))
        self.assertFalse(dashboard.is_payload_asset("zeroclaw-x86_64.sigstore.json"))
        self.assertFalse(dashboard.is_payload_asset("install.sh"))


if __name__ == "__main__":
    unittest.main()
