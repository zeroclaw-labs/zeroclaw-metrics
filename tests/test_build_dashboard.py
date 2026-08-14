import datetime as dt
import importlib.util
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class PublicInferenceUsageTests(unittest.TestCase):
    PAGE = r'''\"totalTokens\":300,\"rank\":null,\"modelsUsed\":2
    \"data\":[{\"x\":\"2026-08-12 00:00:00\",\"ys\":{\"vendor/model-a-20260801\":200,\"Others\":100}}],\"appName\":\"ZeroClaw\",\"forecast\":\"forecast-1d\"
    \"appModelAnalytics\":[{\"date\":\"2026-08-12\",\"model_permaslug\":\"vendor/model-a-20260801\",\"total_tokens\":200}],\"appName\":\"ZeroClaw\"'''

    CATALOG = {
        "data": [
            {
                "id": "vendor/model-a",
                "name": "Vendor: Model A",
                "canonical_slug": "vendor/model-a-20260801",
                "pricing": {"prompt": "0.000001", "completion": "0.000003"},
            },
            {
                "id": "vendor/model-a:free",
                "name": "Vendor: Model A (free)",
                "canonical_slug": "vendor/model-a-20260801",
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }

    def test_compact_formats_billion_scale_usage(self):
        self.assertEqual("14.8B", dashboard.compact(14_847_281_097))

    def test_openrouter_uses_brand_name_in_dashboard_labels(self):
        self.assertEqual(
            "OpenRouter", dashboard.public_usage_source_name({"source": "openrouter"})
        )

    def test_public_dashboard_does_not_render_spend(self):
        renderer = inspect.getsource(dashboard.render_dashboard)
        self.assertNotIn("estimated_spend_usd", renderer)
        self.assertNotIn("OpenRouter Spend", renderer)

    def test_public_dashboard_labels_openrouter_as_last_30_days(self):
        renderer = inspect.getsource(dashboard.render_dashboard)
        self.assertIn("Tokens — Last 30 Days", renderer)
        self.assertIn("Usage — Last 30 Days", renderer)

    def test_dashboard_uses_canonical_brand_surface(self):
        renderer = inspect.getsource(dashboard.render_dashboard)
        self.assertIn('class="hero"', renderer)
        self.assertIn('href="assets/style.css"', renderer)
        self.assertIn('src="assets/zeroclaw-labs-mark.png"', renderer)
        self.assertTrue((SCRIPT_PATH.parents[1] / "assets" / "style.css").is_file())
        self.assertTrue(
            (SCRIPT_PATH.parents[1] / "assets" / "zeroclaw-labs-mark.png").is_file()
        )

    def test_parses_embedded_openrouter_usage(self):
        parsed = dashboard.parse_openrouter_app_page(self.PAGE)
        self.assertEqual(300, parsed["reported_total_tokens"])
        self.assertEqual(2, parsed["models_used"])
        self.assertEqual(200, parsed["daily"][0]["ys"]["vendor/model-a-20260801"])
        self.assertEqual("vendor/model-a-20260801", parsed["model_totals"][0]["model_permaslug"])

    def test_estimate_records_coverage_and_output_share_range(self):
        catalog = dashboard.openrouter_price_catalog(self.CATALOG)
        estimate = dashboard.estimate_public_usage(
            {"vendor/model-a-20260801": 200_000_000},
            catalog,
            total_tokens=300_000_000,
        )
        self.assertEqual(200_000_000, estimate["priced_tokens"])
        self.assertAlmostEqual(66.6667, estimate["pricing_coverage_pct"], places=4)
        self.assertAlmostEqual(360.0, estimate["estimated_spend_usd"])
        self.assertLess(estimate["estimated_spend_low_usd"], estimate["estimated_spend_usd"])
        self.assertGreater(estimate["estimated_spend_high_usd"], estimate["estimated_spend_usd"])

    def test_collector_emits_normalized_provider_contract(self):
        with (
            mock.patch.object(dashboard, "get_text", return_value=self.PAGE),
            mock.patch.object(dashboard, "get_json", return_value=self.CATALOG),
        ):
            source = dashboard.collect_openrouter_public_usage()
        self.assertEqual("openrouter", source["source"])
        self.assertEqual("zeroclaw", source["entity"])
        self.assertEqual(300, source["reported_total_tokens"])
        self.assertEqual(1, source["window_days"])
        self.assertIn("input_output_split", source["estimation_method"])
        self.assertAlmostEqual(0.00036, source["daily"][0]["estimated_spend_usd"])

    def test_provider_failure_does_not_suppress_healthy_sources(self):
        def failed_source():
            raise RuntimeError("provider unavailable")

        healthy = {
            "source": "provider-a",
            "entity": "zeroclaw",
            "daily": [],
            "top_models": [],
            "estimate": {},
        }
        with mock.patch.object(
            dashboard,
            "PUBLIC_USAGE_SOURCE_COLLECTORS",
            (("provider-a", lambda: healthy), ("provider-b", failed_source)),
        ):
            result = dashboard.collect_public_inference_usage()

        self.assertEqual([healthy], result["sources"])
        self.assertTrue(result["source_status"][0]["ok"])
        self.assertFalse(result["source_status"][1]["ok"])
        self.assertIn("provider unavailable", result["source_status"][1]["error"])

    def test_observed_history_uses_latest_overlapping_observation(self):
        def snapshot(generated_at, tokens, partial):
            return {
                "generated_at": generated_at,
                "public_inference_usage": {
                    "ok": True,
                    "data": {
                        "sources": [
                            {
                                "source": "provider-a",
                                "entity": "zeroclaw",
                                "daily": [
                                    {
                                        "day": "2026-08-12",
                                        "total_tokens": tokens,
                                        "priced_tokens": tokens,
                                        "pricing_coverage_pct": 100.0,
                                        "estimated_spend_usd": tokens / 100,
                                        "estimated_spend_low_usd": tokens / 200,
                                        "estimated_spend_high_usd": tokens / 50,
                                        "is_partial": partial,
                                    }
                                ],
                            }
                        ]
                    },
                },
            }

        documents = [
            (Path("early.json"), snapshot("2026-08-12T08:00:00Z", 100, True)),
            (Path("late.json"), snapshot("2026-08-13T08:00:00Z", 150, False)),
        ]
        with mock.patch.object(dashboard, "load_snapshot_documents", return_value=documents):
            rows = dashboard.observed_public_usage_history()
        self.assertEqual(1, len(rows))
        self.assertEqual(150, rows[0]["total_tokens"])
        self.assertFalse(rows[0]["is_partial"])
        self.assertEqual("2026-08-13T08:00:00Z", rows[0]["source_snapshot_at"])

    def test_sqlite_exposes_normalized_public_usage_tables(self):
        source = {
            "source": "provider-a",
            "entity": "zeroclaw",
            "source_url": "https://example.test/usage",
            "window_start": "2026-08-12",
            "window_end": "2026-08-12",
            "window_days": 1,
            "reported_total_tokens": 100,
            "models_used": 1,
            "estimate": {
                "priced_tokens": 100,
                "pricing_coverage_pct": 100.0,
                "estimated_spend_usd": 1.0,
                "estimated_spend_low_usd": 0.8,
                "estimated_spend_high_usd": 1.2,
            },
            "estimation_method": {"currency": "USD"},
            "top_models": [
                {
                    "model_key": "vendor/model",
                    "model_id": "vendor/model",
                    "model_name": "Model",
                    "tokens": 100,
                    "input_usd_per_million": 1.0,
                    "output_usd_per_million": 3.0,
                    "estimated_spend_usd": 0.00012,
                }
            ],
            "daily": [
                {
                    "day": "2026-08-12",
                    "total_tokens": 100,
                    "priced_tokens": 100,
                    "pricing_coverage_pct": 100.0,
                    "estimated_spend_usd": 1.0,
                    "estimated_spend_low_usd": 0.8,
                    "estimated_spend_high_usd": 1.2,
                    "is_partial": False,
                    "models": [],
                }
            ],
        }
        snapshot = {
            "generated_at": "2026-08-13T08:00:00Z",
            "repo": dashboard.FULL_REPO,
            "public_inference_usage": {
                "ok": True,
                "collected_at": "2026-08-13T08:00:00Z",
                "data": {"sources": [source]},
            },
        }
        history = [
            {
                "source": "provider-a",
                "entity": "zeroclaw",
                "day": "2026-08-12",
                "total_tokens": 100,
                "priced_tokens": 100,
                "pricing_coverage_pct": 100.0,
                "estimated_spend_usd": 1.0,
                "estimated_spend_low_usd": 0.8,
                "estimated_spend_high_usd": 1.2,
                "is_partial": False,
                "observed_cumulative_tokens": 100,
                "observed_cumulative_estimated_spend_usd": 1.0,
                "source_snapshot_at": "2026-08-13T08:00:00Z",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "metrics.sqlite"
            with (
                mock.patch.object(dashboard, "DATABASE_PATH", database),
                mock.patch.object(
                    dashboard,
                    "load_snapshot_documents",
                    return_value=[(Path(dashboard.ROOT) / "snapshot.json", snapshot)],
                ),
            ):
                dashboard.build_sqlite_database(
                    {"rows": [], "clone_history": [], "public_usage_history": history}
                )
            connection = sqlite3.connect(database)
            try:
                self.assertEqual(
                    ("provider-a", 100, 1.0),
                    connection.execute(
                        "select source, total_tokens, estimated_spend_usd from public_usage_windows"
                    ).fetchone(),
                )
                self.assertEqual(
                    ("2026-08-12", 100, 1.0),
                    connection.execute(
                        "select day, total_tokens, estimated_spend_usd from observed_public_usage_history"
                    ).fetchone(),
                )
            finally:
                connection.close()


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
