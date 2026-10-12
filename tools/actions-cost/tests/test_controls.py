from __future__ import annotations
import datetime as dt
import json
import subprocess
import tempfile
import unittest
import shutil
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

from actions_cost.billing import fresh, summarize
from actions_cost.monitor import notification_changes, run as monitor_run
from actions_cost.policy import check, matrix_copies, load_workflow, trusted_bundle
from actions_cost.push import approved, gate, install, run_estimate

NOW = dt.datetime(2026, 10, 8, 10, tzinfo=dt.timezone.utc)
WORKFLOW = '''name: checks
on:
  pull_request:
  push:
    branches: [codex/design-playground]
concurrency:
  group: checks-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true
jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - run: python3 tools/actions-cost/check.py check
      - run: npm ci
        timeout-minutes: 5
'''


def billing(minutes=1000, sku="Actions Linux"):
    return {"usageItems": [{"date": "2026-10-08T00:00:00Z", "product": "actions", "sku": sku,
                            "quantity": minutes, "unitType": "Minutes", "netAmount": 0,
                            "repositoryName": "adynamite"},
                           {"product": "Actions", "quantity": 99999, "unitType": "GigabyteHours"}]}


def repo(root: Path, workflow=WORKFLOW):
    path = root / ".github/workflows/checks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow)
    data = {"schema_version": 1, "policy_version": "1.0.0", "repository": "gurinie/example",
            "default_branch": "codex/design-playground", "workflows": {
                ".github/workflows/checks.yaml": {"category": "validation", "expected_monthly_runs": 50,
                    "jobs": {"verify": {"runner": "github-hosted", "runner_labels": "ubuntu-latest",
                                        "timeout_minutes": 20, "max_parallel_copies": 1,
                                        "estimated_minutes_per_copy": 4}}}}, "exceptions": []}
    (root / ".github/actions-minutes-budget.json").write_text(json.dumps(data))
    return data


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
    def errors(self, workflow):
        repo(self.root, workflow)
        return check(self.root, today=NOW.date(), verify_bundle=False)
    def test_actual_nonstandard_default_branch(self):
        self.assertEqual(self.errors(WORKFLOW), [])
    def test_missing_and_oversized_timeout(self):
        for replacement in ["", "    timeout-minutes: 360\n"]:
            self.assertTrue(self.errors(WORKFLOW.replace("    timeout-minutes: 20\n", replacement)))
    def test_duplicate_feature_branch_trigger(self):
        self.assertTrue(self.errors(WORKFLOW.replace("branches: [codex/design-playground]", "branches: [main, codex/feature]")))
    def test_unbounded_setup(self):
        self.assertTrue(self.errors(WORKFLOW.replace("        timeout-minutes: 5\n", "")))
    def test_missing_concurrency(self):
        self.assertTrue(self.errors(WORKFLOW.replace("cancel-in-progress: true", "cancel-in-progress: false")))
    def test_new_workflow_discovered(self):
        repo(self.root)
        (self.root / ".github/workflows/new.yml").write_text(WORKFLOW)
        self.assertTrue(any("inventory" in error for error in check(self.root, verify_bundle=False)))
    def test_duplicate_yaml_keys_rejected(self):
        with self.assertRaises(ValueError): load_workflow("jobs: {}\njobs: {}\n")
    def test_documented_matrix_include_and_exclude(self):
        matrix = {"fruit": ["apple", "pear"], "animal": ["cat", "dog"],
                  "include": [{"color": "green"}, {"color": "pink", "animal": "cat"},
                              {"fruit": "apple", "shape": "circle"}, {"fruit": "banana"},
                              {"fruit": "banana", "animal": "cat"}]}
        self.assertEqual(matrix_copies({"strategy": {"matrix": matrix}}), 6)
        matrix["exclude"] = [{"fruit": "pear", "animal": "dog"}]
        self.assertEqual(matrix_copies({"strategy": {"matrix": matrix}}), 5)
    def test_dynamic_matrix_unavailable(self):
        with self.assertRaises(ValueError): matrix_copies({"strategy": {"matrix": "${{ fromJSON(needs.plan.outputs.matrix) }}"}})
    def test_manifest_runner_drift(self):
        self.assertTrue(self.errors(WORKFLOW.replace("ubuntu-latest", "windows-latest")))
    def test_local_and_external_reusable_workflows(self):
        source = WORKFLOW.replace('    runs-on: ubuntu-latest\n', '    uses: ./.github/workflows/missing.yml\n')
        self.assertTrue(self.errors(source))
        source = source.replace('./.github/workflows/missing.yml', 'other/repo/.github/workflows/check.yml@main')
        self.assertTrue(any('full SHA' in error for error in self.errors(source)))
    def test_valid_local_reusable_accounts_for_callee_and_shared_check(self):
        data=repo(self.root)
        child='.github/workflows/child.yml'
        source=WORKFLOW.replace('name: checks','name: reusable').replace('on:\n  pull_request:\n  push:\n    branches: [codex/design-playground]','on: workflow_call')
        (self.root/child).write_text(source)
        data['workflows'][child]=dict(data['workflows']['.github/workflows/checks.yaml'],expected_monthly_runs=0)
        (self.root/'.github/workflows/checks.yaml').write_text('on: pull_request\nconcurrency:\n  group: call-${{ github.ref }}\n  cancel-in-progress: true\njobs:\n  verify:\n    uses: ./.github/workflows/child.yml\n')
        data['workflows']['.github/workflows/checks.yaml']['jobs']={'verify':{'runner':'reusable','runner_labels':None,'timeout_minutes':None,'max_parallel_copies':1,'estimated_minutes_per_copy':4}}
        (self.root/'.github/actions-minutes-budget.json').write_text(json.dumps(data))
        self.assertEqual(check(self.root,verify_bundle=False),[])
        data['workflows']['.github/workflows/checks.yaml']['jobs']['verify']['estimated_minutes_per_copy']=0
        (self.root/'.github/actions-minutes-budget.json').write_text(json.dumps(data))
        self.assertTrue(any('callee cost' in error for error in check(self.root,verify_bundle=False)))

    def test_publisher_does_not_require_validation_cancellation(self):
        data = repo(self.root, WORKFLOW.replace("timeout-minutes: 20", "timeout-minutes: 35").replace("cancel-in-progress: true", "cancel-in-progress: false"))
        entry = data["workflows"][".github/workflows/checks.yaml"]
        entry["category"] = "publisher"; entry["jobs"]["verify"]["timeout_minutes"] = 35
        (self.root / ".github/actions-minutes-budget.json").write_text(json.dumps(data))
        self.assertEqual(check(self.root, verify_bundle=False), [])
    def test_expired_exception_is_not_a_pass(self):
        data = repo(self.root, WORKFLOW.replace("    timeout-minutes: 20\n", ""))
        data["workflows"][".github/workflows/checks.yaml"]["jobs"]["verify"]["timeout_minutes"] = None
        data["exceptions"] = [{"workflow": ".github/workflows/checks.yaml", "job": "verify", "rule": "timeout",
                               "expires": "2026-10-07", "reason": "next verified release", "approval_reference": "existing baseline"}]
        (self.root / ".github/actions-minutes-budget.json").write_text(json.dumps(data))
        self.assertTrue(check(self.root, today=NOW.date(), verify_bundle=False))


class BillingTests(unittest.TestCase):
    def test_live_shape_and_storage_exclusion(self):
        self.assertEqual(summarize(billing(3015), now=NOW)["official_runner_minutes"], 3015)
    def test_threshold_and_reserve_are_advisory(self):
        self.assertFalse(summarize(billing(2300), now=NOW)["warning_required"])
        self.assertTrue(summarize(billing(2300), planned=150, pending=100, now=NOW)["warning_required"])
        self.assertTrue(summarize(billing(2400), now=NOW)["warning_required"])
        self.assertFalse(summarize(billing(4000), now=NOW)["approval_required"])
    def test_unknown_sku_not_false_remaining(self):
        report = summarize(billing(10, "Actions Windows"), now=NOW)
        self.assertIsNone(report["remaining_linux_minutes"]); self.assertTrue(report["warning_required"])
        self.assertFalse(report["approval_required"])
    def test_malformed_and_missing_unavailable(self):
        for payload in [{}, billing(float("nan"))]:
            with self.assertRaises(ValueError): summarize(payload, now=NOW)
    def test_wrong_month_or_invalid_pending_is_unavailable(self):
        with self.assertRaises(ValueError):summarize(billing(),now=NOW.replace(month=11))
        with self.assertRaises(ValueError):summarize(billing(),pending=float('nan'),now=NOW)

    def test_snapshot_freshness_and_month_rollover(self):
        value = summarize(billing(), now=NOW); value["source"] = "live_github_billing_api"
        self.assertTrue(fresh(value, NOW + dt.timedelta(minutes=9)))
        self.assertFalse(fresh(value, NOW + dt.timedelta(minutes=11)))
        self.assertFalse(fresh(value, NOW.replace(month=11)))
        value["source"] = "saved_response"; self.assertFalse(fresh(value, NOW))
    def test_alerts_deduplicated_and_recover(self):
        report = {"billing": summarize(billing(2500), now=NOW), "repositories": [], "unavailable": ["billing partial outage"]}
        self.assertTrue(notification_changes(report, None))
        same = json.loads(json.dumps(report)); self.assertEqual(notification_changes(same, report), [])
        same["unavailable"] = []; self.assertIn("RECOVERED: billing partial outage", notification_changes(same, report))


class HookTests(unittest.TestCase):
    def test_offline_guard_allows_real_push_without_usage_approval(self):
        for minutes, succeeds in [(2300, True), (2400, True), (4000, True), (None, True)]:
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as temporary:
                base=Path(temporary); root=base/'checkout'; bare=base/'remote.git'
                subprocess.run(['git','init','-q',str(root)],check=True)
                subprocess.run(['git','init','-q','--bare',str(bare)],check=True)
                for key,value in [('user.name','Fixture'),('user.email','fixture@example.invalid')]:
                    subprocess.run(['git','config',key,value],cwd=root,check=True)
                subprocess.run(['git','remote','add','origin',str(bare)],cwd=root,check=True)
                repo(root)
                canonical=Path(__file__).resolve().parents[1]; bundle=root/'tools/actions-cost'
                pin=json.loads((canonical/'bundle.json').read_text())
                for name in [*pin['files'],'bundle.json']:
                    target=bundle/name; target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(canonical/name,target)
                subprocess.run(['git','add','.'],cwd=root,check=True)
                subprocess.run(['git','commit','-qm','bounded fixture'],cwd=root,check=True)
                controller=base/'controller.py'
                controller.write_text('import sys\nsys.path.insert(0,'+repr(str(canonical))+')\nfrom unittest.mock import patch\nfrom actions_cost.cli import main\nwith patch("actions_cost.github.api", side_effect=AssertionError("Push guard must remain offline")):\n    raise SystemExit(main())\n')
                install(root,controller)
                attempt=subprocess.run(['git','push','origin','HEAD:refs/heads/topic'],cwd=root,capture_output=True,text=True)
                self.assertEqual(attempt.returncode == 0,succeeds,attempt.stderr)
                present=subprocess.run(['git','--git-dir',str(bare),'show-ref'],capture_output=True).returncode==0
                self.assertEqual(present,succeeds)

    def test_preview_deployment_included_in_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); data=repo(root)
            declaration=data['workflows']['.github/workflows/checks.yaml']
            declaration['category']='deployment'
            self.assertEqual(run_estimate(data,'refs/heads/topic',root),4)

    def test_real_push_blocks_unsafe_committed_head_even_with_clean_worktree_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); root = base / 'checkout'; bare = base / 'remote.git'
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','init','-q','--bare',str(bare)],check=True)
            subprocess.run(['git','config','user.name','Fixture'],cwd=root,check=True)
            subprocess.run(['git','config','user.email','fixture@example.invalid'],cwd=root,check=True)
            subprocess.run(['git','remote','add','origin',str(bare)],cwd=root,check=True)
            repo(root, WORKFLOW.replace('    timeout-minutes: 20\n',''))
            canonical=Path(__file__).resolve().parents[1]; bundle=root/'tools/actions-cost'
            shutil.copytree(canonical/'actions_cost',bundle/'actions_cost',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy2(canonical/'check.py',bundle/'check.py')
            files=[*sorted((bundle/'actions_cost').glob('*.py')),bundle/'check.py']
            pin={'version':'1.0.0','files':{f.relative_to(bundle).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
            (bundle/'bundle.json').write_text(json.dumps(pin))
            subprocess.run(['git','add','.'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','unsafe fixture'],cwd=root,check=True)
            # A local repair must not disguise an unsafe outgoing committed SHA.
            (root/'.github/workflows/checks.yaml').write_text(WORKFLOW)
            controller=base/'controller.py'
            controller.write_text('import sys\nsys.path.insert(0,'+repr(str(canonical))+')\nfrom actions_cost.cli import main\nraise SystemExit(main())\n')
            install(root,controller)
            attempt=subprocess.run(['git','push','origin','HEAD:refs/heads/topic'],cwd=root,capture_output=True,text=True)
            self.assertNotEqual(attempt.returncode,0)
            self.assertIn('timeout',attempt.stderr)
            refs=subprocess.check_output(['git','--git-dir',str(bare),'show-ref'],text=True) if subprocess.run(['git','--git-dir',str(bare),'show-ref'],capture_output=True).returncode==0 else ''
            self.assertEqual(refs,'')

    def test_tracked_custom_hook_and_other_hooks_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);subprocess.run(['git','init','-q',str(root)],check=True)
            hooks=root/'.githooks';hooks.mkdir()
            (hooks/'pre-push').write_text('#!/bin/sh\ncat >/dev/null\n');(hooks/'pre-push').chmod(0o755)
            (hooks/'pre-commit').write_text('#!/bin/sh\nexit 0\n');(hooks/'pre-commit').chmod(0o755)
            original=(hooks/'pre-push').read_bytes()
            subprocess.run(['git','config','core.hooksPath','.githooks'],cwd=root,check=True)
            installed=install(root,root/'controller.py')
            self.assertEqual((hooks/'pre-push').read_bytes(),original)
            self.assertTrue((installed.parent/'pre-commit').is_symlink())

    def test_existing_hook_and_stdin_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); subprocess.run(["git", "init", "-q", str(root)], check=True)
            old = root / ".git/hooks/pre-push"
            old.write_text('#!/bin/sh\ncat > "$(git rev-parse --show-toplevel)/previous-input"\n'); old.chmod(0o755)
            controller = root / "controller.py"
            controller.write_text('import sys\nassert len(sys.stdin.read().split()) == 4\n')
            hook = install(root, controller); install(root, controller)
            line = "refs/heads/x " + "1" * 40 + " refs/heads/x " + "0" * 40 + "\n"
            subprocess.run([str(hook), "origin"], input=line, text=True, cwd=root, check=True)
            self.assertEqual((root / "previous-input").read_text(), line)
    def test_approval_binding_and_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary); (state / "approvals").mkdir()
            sha = "1" * 40
            value = {"repository": "gurinie/example", "commit": sha, "ref": "refs/heads/x", "max_minutes": 20,
                     "period": "2026-10", "approval_reference": "user message 123", "approved_at_utc": NOW.isoformat(),
                     "expires_at_utc": (NOW + dt.timedelta(hours=1)).isoformat()}
            (state / "approvals" / f"{sha}.json").write_text(json.dumps(value))
            self.assertTrue(approved(state, "gurinie/example", sha, "refs/heads/x", 20, NOW))
            self.assertFalse(approved(state, "gurinie/example", sha, "refs/heads/y", 20, NOW))
            self.assertFalse(approved(state, "gurinie/example", sha, "refs/heads/x", 21, NOW))
            self.assertFalse(approved(state, "gurinie/example", sha, "refs/heads/x", 20, NOW + dt.timedelta(hours=2)))


if __name__ == "__main__": unittest.main()
