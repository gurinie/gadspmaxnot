from __future__ import annotations
import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions_cost import VERSION
from actions_cost.monitor import notification_changes, run
from actions_cost.policy import check, trusted_bundle
from actions_cost.push import run_estimate
from actions_cost.github import api
from test_controls import repo, WORKFLOW, billing


class AdvisoryTests(unittest.TestCase):
    def test_backend_changes_exclude_website_and_manual_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = repo(root)
            website = ".github/workflows/website.yml"
            operation = ".github/workflows/operation.yaml"
            (root / website).write_text("on:\n  pull_request:\n    paths: ['website/**']\njobs: {}\n")
            (root / operation).write_text("on: workflow_dispatch\njobs: {}\n")
            for name in (website, operation):
                data["workflows"][name] = {"category": "deployment", "jobs": {
                    "build": {"runner": "github-hosted", "max_parallel_copies": 3,
                              "estimated_minutes_per_copy": 5}}}
            self.assertEqual(run_estimate(data, "refs/heads/topic", root,
                                          changed_paths=["backend/app.py"]), 4)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root,
                                          changed_paths=["website/page.tsx"]), 19)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root,
                                          event="workflow_dispatch"), 15)

    def test_branch_filters_use_pr_base_and_actual_default_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = repo(root, WORKFLOW.replace("  pull_request:", "  pull_request:\n    branches: [codex/design-playground]"))
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="push"), 0)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request"), 4)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request", base_ref="main"), 0)
            self.assertEqual(run_estimate(data, "refs/heads/codex/design-playground", root, event="push"), 4)

    def test_path_ignore_negation_types_and_unfamiliar_patterns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = repo(root, WORKFLOW.replace("  pull_request:", "  pull_request:\n    types: [ready_for_review]\n    paths: ['**', '!docs/**']"))
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request",
                                          changed_paths=["docs/test.md"], activity="ready_for_review"), 0)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request",
                                          changed_paths=["app.py"], activity="opened"), 0)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request",
                                          changed_paths=["app.py"], activity="ready_for_review"), 4)
            (root / ".github/workflows/checks.yaml").write_text(WORKFLOW.replace("  pull_request:", "  pull_request:\n    paths-ignore: ['docs/**']"))
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request", changed_paths=["docs/test.md"]), 0)
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request", changed_paths=["docs/test.md", "app.py"]), 4)
            (root / ".github/workflows/checks.yaml").write_text(WORKFLOW.replace("  pull_request:", "  pull_request:\n    paths: ['[ab]/**']"))
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request", changed_paths=["c/test.py"]), 4)
            (root / ".github/workflows/checks.yaml").write_text(WORKFLOW.replace("  pull_request:", "  pull_request:\n    paths-ignore: ['[ab]/**']"))
            self.assertEqual(run_estimate(data, "refs/heads/topic", root, event="pull_request", changed_paths=["c/test.py"]), 4)

    def test_reusable_definition_not_counted_as_standalone_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = repo(root)
            name = ".github/workflows/reusable.yaml"
            (root / name).write_text("on: workflow_call\njobs: {}\n")
            data["workflows"][name] = dict(data["workflows"][".github/workflows/checks.yaml"])
            self.assertEqual(run_estimate(data, "refs/heads/topic", root), 4)

    def test_forecast_frequency_is_advisory_not_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data = repo(root)
            del data["workflows"][".github/workflows/checks.yaml"]["expected_monthly_runs"]
            (root / ".github/actions-minutes-budget.json").write_text(json.dumps(data))
            self.assertEqual(check(root, verify_bundle=False), [])

    def test_known_legacy_pin_accepted_but_rehashed_tampering_not(self):
        current = json.loads((Path(__file__).resolve().parents[1] / "bundle.json").read_text())
        old = json.loads((Path(__file__).resolve().parents[1] / "trusted-bundles.json").read_text())["bundles"][0]
        self.assertTrue(trusted_bundle(current, current))
        self.assertTrue(trusted_bundle(old, current))
        changed = json.loads(json.dumps(old))
        changed["files"]["check.py"] = "0" * 64
        self.assertFalse(trusted_bundle(changed, current))


class MonitorTests(unittest.TestCase):
    def test_paginated_job_envelopes_are_parsed(self):
        from types import SimpleNamespace
        output = json.dumps([{"total_count": 2, "jobs": [{"id": 1}]}, {"total_count": 2, "jobs": [{"id": 2}]}])
        with patch("actions_cost.github.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=output)):
            self.assertEqual(api("/repos/gurinie/example/actions/runs/1/jobs", paginate=True), [{"id": 1}, {"id": 2}])

    def test_partial_outage_is_not_false_policy_recovery(self):
        previous = {"active_alerts": ["gurinie/example: missing shared checker bundle",
                                      "2026-10: included allowance crossed 80%"]}
        report = {"billing": None, "repositories": [], "unavailable": ["gurinie/example: audit unavailable"],
                  "audit_unavailable_repositories": ["gurinie/example"]}
        changes = notification_changes(report, previous)
        self.assertFalse(any(item.startswith("RECOVERED") for item in changes))
        second = dict(report)
        self.assertEqual(notification_changes(second, report), [])
        healthy = {"billing": {"official_runner_minutes": 2500, "included_linux_minutes": 3000,
                               "period": "2026-10", "allowance_conversion_verified": True,
                               "month_end_minutes_estimate": 2000, "reserve_minutes": 500},
                   "repositories": [{"repository": "gurinie/example", "findings": []}], "unavailable": []}
        self.assertIn("RECOVERED: gurinie/example: missing shared checker bundle",
                      notification_changes(healthy, report))

    def test_repository_listing_outage_preserves_policy_findings(self):
        previous = {"active_alerts": ["gurinie/example: missing shared checker bundle"]}
        report = {"billing": None, "repositories": [], "unavailable": ["repository listing unavailable"],
                  "repository_listing_unavailable": True}
        self.assertNotIn("RECOVERED: gurinie/example: missing shared checker bundle",
                         notification_changes(report, previous))

    def test_inventory_cached_daily_but_jobs_remain_live(self):
        now = dt.datetime.now(dt.timezone.utc)
        metadata = {"full_name": "gurinie/example", "default_branch": "codex/design-playground",
                    "private": True, "archived": False, "owner": {"login": "gurinie"}}
        usage = billing(); usage["usageItems"][0]["date"] = now.date().isoformat()
        value = {"repository": metadata["full_name"], "default_branch": metadata["default_branch"],
                 "private": True, "workflow_count": 1, "manifest": {}, "inventory_findings": [],
                 "findings": [], "active_hosted_jobs": [], "pending_minutes_estimate": 0,
                 "inventory_checked_at_utc": (now - dt.timedelta(hours=1)).isoformat(), "canonical_pin": None}
        def response(path, **kwargs):
            return [metadata] if path.startswith("/user/repos") else usage
        activity = {"findings": [], "active_hosted_jobs": [], "pending_minutes_estimate": 0}
        with patch("actions_cost.monitor.api", side_effect=response), patch("actions_cost.monitor.included_allowance", return_value=3000), patch("actions_cost.monitor.inspect_repository", return_value=dict(value)) as inventory, patch("actions_cost.monitor.inspect_activity", return_value=activity) as jobs:
            report = run("gurinie", included=3000, reserve=500, previous={"repositories": [value]})
            inventory.assert_not_called(); jobs.assert_called_once()
            self.assertEqual(report["repositories"][0]["inventory_source"], "daily_cache")
            value["inventory_checked_at_utc"] = (now - dt.timedelta(days=2)).isoformat()
            run("gurinie", included=3000, reserve=500, previous={"repositories": [value]})
            inventory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
