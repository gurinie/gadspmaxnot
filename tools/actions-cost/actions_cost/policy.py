"""Validate workflow cost declarations without executing repository code."""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path

import yaml

from . import VERSION, SUPPORTED_VERSIONS


class UniqueLoader(yaml.BaseLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
CATEGORIES = {"validation", "build", "deployment", "publisher", "operation", "monitor"}


def trusted_bundle(pin: dict, current: dict) -> bool:
    if pin == current:
        return True
    try:
        historical = json.loads((Path(__file__).resolve().parents[1] / "trusted-bundles.json").read_text())
        return pin in historical["bundles"]
    except (OSError, KeyError, ValueError, TypeError):
        return False


def has_shared_check(workflow: dict, root: Path, seen: set[str] | None = None) -> bool:
    seen = seen or set()
    for job in workflow['jobs'].values():
        if any('tools/actions-cost/check.py' in step.get('run', '') for step in job.get('steps', [])):
            return True
        reference = job.get('uses', '')
        if reference.startswith('./') and reference not in seen:
            callee = root / reference[2:]
            if callee.is_file() and not callee.is_symlink():
                if has_shared_check(load_workflow(callee.read_text()), root, seen | {reference}):
                    return True
    return False


def load_workflow(source: str) -> dict:
    parsed = yaml.load(source, Loader=UniqueLoader)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("jobs"), dict):
        raise ValueError("workflow must contain a jobs mapping")
    return parsed


def events(workflow: dict) -> dict:
    raw = workflow.get("on", {})
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {event: {} for event in raw}
    if not isinstance(raw, dict):
        raise ValueError("invalid workflow events")
    return raw


def matrix_copies(job: dict) -> int:
    matrix = job.get("strategy", {}).get("matrix", {})
    if not isinstance(matrix, dict):
        raise ValueError("dynamic matrix requires an explicitly reviewed bound")
    if not matrix:
        return 1
    axes = {key: value for key, value in matrix.items() if key not in {"include", "exclude"}}
    if any(not isinstance(value, list) or not value for value in axes.values()):
        raise ValueError("matrix axes must be fixed nonempty lists")
    if math.prod(len(value) for value in axes.values()) > 128:
        raise ValueError("matrix fan-out exceeds the inspection limit")
    originals = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else []
    excludes, includes = matrix.get("exclude", []), matrix.get("include", [])
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in (excludes, includes)):
        raise ValueError("invalid matrix include/exclude")
    originals = [row for row in originals if not any(all(row.get(k) == v for k, v in ex.items()) for ex in excludes)]
    extra = 0
    for included in includes:
        if not any(all(key not in axes or row.get(key) == value for key, value in included.items()) for row in originals):
            extra += 1
    return len(originals) + extra


def runner_kind(job: dict) -> str:
    if "uses" in job:
        return "reusable"
    runner = job.get("runs-on")
    labels = runner if isinstance(runner, list) else [runner]
    if not labels or any(not isinstance(label, str) or "${{" in label for label in labels):
        raise ValueError("unknown/dynamic runner placement requires explicit review")
    return "self-hosted" if "self-hosted" in labels else "github-hosted"


def bundle_errors(root: Path) -> list[str]:
    path = root / "tools/actions-cost/bundle.json"
    if not path.exists():
        return ["missing pinned tools/actions-cost bundle"]
    try:
        pin = json.loads(path.read_text())
        if pin["version"] not in SUPPORTED_VERSIONS or not isinstance(pin["files"], dict):
            raise ValueError("unsupported bundle version")
        for name, expected in pin["files"].items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("invalid bundle path")
            actual = hashlib.sha256((path.parent / relative).read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"bundle content differs: {name}")
    except (KeyError, ValueError, OSError, TypeError) as exc:
        return [f"invalid bundle: {exc}"]
    return []


def check(root: Path, *, today: dt.date | None = None, verify_bundle: bool = True) -> list[str]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    paths = sorted({*root.glob(".github/workflows/*.yml"), *root.glob(".github/workflows/*.yaml")})
    manifest_path = root / ".github/actions-minutes-budget.json"
    if not manifest_path.exists():
        return ["missing Actions minutes manifest"] if paths else []
    try:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema_version") != 1 or manifest.get("policy_version") not in SUPPORTED_VERSIONS:
            raise ValueError("unsupported manifest version")
        branch = manifest["default_branch"]
        declarations = manifest["workflows"]
        if not isinstance(branch, str) or not branch or not isinstance(declarations, dict):
            raise ValueError("invalid default branch/workflow inventory")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return [f"invalid Actions minutes manifest: {exc}"]
    errors = bundle_errors(root) if paths and verify_bundle else []
    actual_paths = {path.relative_to(root).as_posix() for path in paths}
    if set(declarations) != actual_paths:
        errors.append(f"workflow manifest inventory differs: {sorted(set(declarations) ^ actual_paths)}")

    def exception(workflow: str, job: str, rule: str, copies: int = 0) -> bool:
        for item in manifest.get("exceptions", []):
            if (item.get("workflow"), item.get("job", "*"), item.get("rule")) != (workflow, job, rule):
                continue
            try:
                expiry = dt.date.fromisoformat(item["expires"])
                return bool(item.get("reason") and item.get("approval_reference") and expiry >= today
                            and (rule != "fanout" or item.get("max_parallel_copies", 0) >= copies))
            except (KeyError, ValueError, TypeError):
                return False
        return False

    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                raise ValueError("workflow files must not be symlinks")
            workflow, declared = load_workflow(path.read_text()), declarations.get(name, {})
            category = declared.get("category")
            if category not in CATEGORIES:
                raise ValueError("missing/invalid workflow category")
            trigger = events(workflow)
            if category == "validation":
                if "schedule" in trigger:
                    errors.append(f"{name}: validation must not add paid cron")
                if "pull_request" in trigger:
                    concurrency = workflow.get("concurrency", {})
                    group = concurrency.get("group", "") if isinstance(concurrency, dict) else ""
                    if (not isinstance(concurrency, dict) or concurrency.get("cancel-in-progress") != "true"
                            or not any(key in group for key in ("github.ref", "github.head_ref", "pull_request.number"))):
                        errors.append(f"{name}: validation cancellation must be scoped to its PR/ref")
                    if "push" in trigger:
                        push = trigger["push"]
                        if not isinstance(push, dict) or push.get("branches") != [branch] or "tags" in push:
                            errors.append(f"{name}: duplicate push/PR validation; permit only the actual default branch push")
                if not has_shared_check(workflow, root):
                    errors.append(f"{name}: run the shared checker inside an existing validation job")
            jobs = declared.get("jobs", {})
            if set(jobs) != set(workflow["jobs"]):
                errors.append(f"{name}: job inventory differs from the minutes manifest")
            frequency = declared.get("expected_monthly_runs", 0)
            if isinstance(frequency, bool) or not isinstance(frequency, (int, float)) or not math.isfinite(frequency) or frequency < 0:
                errors.append(f"{name}: declare expected monthly runs for cost review")
            for job_name, job in workflow["jobs"].items():
                entry = jobs.get(job_name, {})
                kind, copies = runner_kind(job), matrix_copies(job)
                if kind == "reusable":
                    reference = job["uses"]
                    if reference.startswith("./"):
                        callee = root / reference[2:]
                        if not callee.is_file() or callee.is_symlink():
                            errors.append(f"{name}/{job_name}: missing local reusable workflow")
                        else:
                            callee_path = callee.relative_to(root).as_posix()
                            callee_jobs = declarations.get(callee_path, {}).get('jobs', {})
                            called_cost = sum(value['max_parallel_copies'] * value['estimated_minutes_per_copy']
                                              for value in callee_jobs.values() if value['runner'] != 'self-hosted')
                            if not callee_jobs or entry.get('estimated_minutes_per_copy', -1) < called_cost:
                                errors.append(f'{name}/{job_name}: reusable caller estimate omits declared callee cost')
                    else:
                        import re
                        if not re.search(r"@[a-f0-9]{40}$", reference):
                            errors.append(f"{name}/{job_name}: external reusable workflow must be pinned to a full SHA")
                        if not entry.get("estimated_minutes_per_copy", 0):
                            errors.append(f"{name}/{job_name}: declare the called workflow's hosted cost")
                raw_timeout = job.get("timeout-minutes")
                timeout = int(raw_timeout) if isinstance(raw_timeout, str) and raw_timeout.isdecimal() else None
                if (entry.get("runner") != kind or entry.get("runner_labels") != job.get("runs-on")
                        or entry.get("timeout_minutes") != timeout or entry.get("max_parallel_copies") != copies):
                    errors.append(f"{name}/{job_name}: runner, timeout or fan-out changed without matching cost declaration")
                estimate = entry.get("estimated_minutes_per_copy")
                if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or not math.isfinite(estimate) or estimate < 0:
                    errors.append(f"{name}/{job_name}: invalid per-copy minute estimate")
                elif kind == "github-hosted" and estimate < 1:
                    errors.append(f"{name}/{job_name}: hosted job estimates must include at least one rounded minute")
                if kind != "github-hosted":
                    if category == "validation" and "pull_request" in trigger and kind == "self-hosted":
                        errors.append(f"{name}/{job_name}: PR validation must remain on hosted runners")
                    continue
                maximum = 30 if category == "validation" else 60
                if (timeout is None or not 1 <= timeout <= maximum) and not exception(name, job_name, "timeout"):
                    errors.append(f"{name}/{job_name}: explicit hosted timeout must be 1..{maximum}")
                if copies > 4 and not exception(name, job_name, "fanout", copies):
                    errors.append(f"{name}/{job_name}: more than four copies require reviewed fan-out")
                for step in job.get("steps", []):
                    command = step.get("run", "")
                    if any(token in command for token in ("apt-get", "apt.postgresql.org.sh", "npm ci", "pip install")):
                        value = step.get("timeout-minutes")
                        bounded = isinstance(value, str) and value.isdecimal() and 1 <= int(value) <= 5
                        if not bounded and not exception(name, job_name, "setup"):
                            errors.append(f"{name}/{job_name}: package setup needs timeout-minutes <= 5")
        except (ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
            errors.append(f"{name}: {exc}")
    return errors


def projected_minutes(manifest: dict) -> float:
    return sum(workflow.get("expected_monthly_runs", 0) * sum(
        job["max_parallel_copies"] * job["estimated_minutes_per_copy"]
        for job in workflow["jobs"].values() if job["runner"] != "self-hosted"
    ) for workflow in manifest["workflows"].values())
