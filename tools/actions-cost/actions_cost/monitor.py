"""Discover workflows and report changes; never dispatch, cancel, or edit GitHub."""
from __future__ import annotations
import base64
import concurrent.futures
import datetime as dt
import hashlib
import json
import tempfile
from pathlib import Path

from .billing import summarize
from .github import Unavailable, api, included_allowance
from .policy import check, trusted_bundle


def pending_account(owner: str) -> float | None:
    """Small admission query; unknown pending cost is not zero."""
    repos = [repo for repo in api("/user/repos?per_page=100", paginate=True)
             if repo["owner"]["login"] == owner and not repo["archived"]]

    def pending(repo):
        name = repo["full_name"]
        runs = [*api(f"/repos/{name}/actions/runs?status=in_progress&per_page=100").get("workflow_runs", []),
                *api(f"/repos/{name}/actions/runs?status=queued&per_page=100").get("workflow_runs", [])]
        if not runs:
            return 0.0
        body = content(name, ".github/actions-minutes-budget.json")
        declarations = json.loads(body).get("workflows", {}) if body else {}
        total = 0.0
        for run in runs:
            if run.get("event") == "dynamic" and run.get("actor", {}).get("login") == "dependabot[bot]":
                continue  # GitHub's Dependabot update-generation jobs are free.
            declaration = declarations.get(run.get("path", "").split("@")[0])
            if declaration:
                total += sum(job["max_parallel_copies"] * job["estimated_minutes_per_copy"]
                             for job in declaration["jobs"].values() if job["runner"] != "self-hosted")
            else:
                jobs = api(f"/repos/{name}/actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
                if not jobs or any("self-hosted" not in job.get("labels", []) for job in jobs
                                   if job.get("status") != "completed"):
                    return None
        return total

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(pending, repos))
    return None if any(value is None for value in values) else sum(values)


def content(repo: str, path: str):
    value = api(f"/repos/{repo}/contents/{path}", missing_ok=True)
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") != "file":
        raise Unavailable(f"unexpected file metadata: {repo}/{path}")
    return base64.b64decode(value["content"], validate=False)


def inspect_repository(repo: dict, *, expected_bundle: dict | None = None) -> dict:
    name = repo["full_name"]
    listing = api(f"/repos/{name}/contents/.github/workflows", missing_ok=True)
    files = [item for item in (listing or []) if item["name"].endswith((".yaml", ".yml"))]
    findings = []
    manifest = {}
    with tempfile.TemporaryDirectory(prefix="actions-policy-audit-") as temporary:
        root = Path(temporary)
        for item in files:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content(name, item["path"]))
        manifest_bytes = content(name, ".github/actions-minutes-budget.json")
        if manifest_bytes:
            target = root / ".github/actions-minutes-budget.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(manifest_bytes)
            try:
                manifest = json.loads(manifest_bytes)
                if manifest.get("default_branch") != repo["default_branch"]:
                    findings.append("manifest default branch differs from GitHub metadata")
            except ValueError:
                findings.append("invalid manifest JSON")
        findings += check(root, verify_bundle=False)
        if files:
            pin_bytes = content(name, "tools/actions-cost/bundle.json")
            if not pin_bytes:
                findings.append("missing shared checker bundle")
            else:
                try:
                    pin = json.loads(pin_bytes)
                    if expected_bundle is not None and not trusted_bundle(pin, expected_bundle):
                        findings.append("shared checker version/content pin differs from canonical bundle")
                    for path, digest in pin.get("files", {}).items():
                        relative = Path(path)
                        if relative.is_absolute() or ".." in relative.parts:
                            findings.append("invalid bundle file path"); continue
                        body = content(name, f"tools/actions-cost/{path}")
                        if body is None or hashlib.sha256(body).hexdigest() != digest:
                            findings.append(f"shared checker content differs: {path}")
                except (ValueError, TypeError):
                    findings.append("invalid shared checker pin")
    inventory_findings = sorted(set(findings))
    activity = inspect_activity(name, len(files), manifest)
    return {"repository": name, "default_branch": repo["default_branch"], "private": repo["private"],
            "workflow_count": len(files), "findings": sorted(set(inventory_findings + activity["findings"])),
            "inventory_findings": inventory_findings, "manifest": manifest,
            "inventory_checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "policy_version": manifest.get("policy_version"),
            "active_hosted_jobs": activity["active_hosted_jobs"],
            "pending_minutes_estimate": activity["pending_minutes_estimate"]}


def inspect_activity(name: str, workflow_count: int, manifest: dict) -> dict:
    active, findings = [], []
    pending_estimate = 0.0
    if workflow_count:
        runs = [*api(f"/repos/{name}/actions/runs?status=in_progress&per_page=100").get("workflow_runs", []),
                *api(f"/repos/{name}/actions/runs?status=queued&per_page=100").get("workflow_runs", [])]
        now = dt.datetime.now(dt.timezone.utc)
        for run in runs:
            if run.get("event") == "dynamic" and run.get("actor", {}).get("login") == "dependabot[bot]":
                continue
            workflow_path = run.get("path", "").split("@")[0]
            declaration = manifest.get("workflows", {}).get(workflow_path)
            if declaration:
                if pending_estimate is not None:
                    pending_estimate += sum(entry["max_parallel_copies"] * entry["estimated_minutes_per_copy"]
                                            for entry in declaration["jobs"].values() if entry["runner"] != "self-hosted")
            else:
                pending_estimate = None
                findings.append(f"pending-run cost unavailable without manifest: {run['html_url']}")
            for job in api(f"/repos/{name}/actions/runs/{run['id']}/jobs?per_page=100", paginate=True):
                labels = job.get("labels", [])
                if job.get("status") != "in_progress" or "self-hosted" in labels or not job.get("started_at"):
                    continue
                elapsed = (now - dt.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))).total_seconds() / 60
                active.append({"job": job["name"], "elapsed_minutes": round(elapsed, 1), "url": job["html_url"]})
                fallback = 20 if declaration and declaration.get("category") == "validation" else 60
                limit = max((entry.get("timeout_minutes") or fallback
                             for entry in (declaration or {}).get("jobs", {}).values()
                             if entry.get("runner") != "self-hosted"), default=fallback)
                for key, entry in (declaration or {}).get("jobs", {}).items():
                    if job['name'] == key or job['name'].startswith(key + ' ('):
                        limit = entry.get("timeout_minutes") or limit
                if elapsed > limit:
                    findings.append(f"hosted job exceeds its {limit}-minute limit: {job['html_url']}")
    return {"findings": sorted(set(findings)), "active_hosted_jobs": active,
            "pending_minutes_estimate": pending_estimate}


def notification_changes(report: dict, previous: dict | None) -> list[str]:
    alerts = set()
    usage = report.get("billing")
    if usage:
        total, included = usage["official_runner_minutes"], usage["included_linux_minutes"]
        for percent in (50, 70, 80):
            if total >= included * percent / 100:
                alerts.add(f"{usage['period']}: included allowance crossed {percent}%")
        if not usage["allowance_conversion_verified"]:
            alerts.add("billing allowance conversion unavailable for reported runner SKUs")
        if usage["month_end_minutes_estimate"] > included - usage["reserve_minutes"]:
            alerts.add(f"{usage['period']}: projected spending breaches release/incident reserve")
    for error in report.get("unavailable", []):
        alerts.add(error)
    for repo in report.get("repositories", []):
        alerts.update(f"{repo['repository']}: {finding}" for finding in repo["findings"])
    old = set((previous or {}).get("active_alerts", []))
    # Missing evidence is not recovery. Preserve findings for sources not successfully read.
    if report.get("billing") is None:
        import re
        alerts.update(value for value in old if re.match(r"^\d{4}-\d{2}: ", value)
                      or value == "billing allowance conversion unavailable for reported runner SKUs")
    successful = {repo["repository"] for repo in report.get("repositories", [])}
    failed = set(report.get("audit_unavailable_repositories", []))
    if report.get("repository_listing_unavailable"):
        failed.update(value.split(": ", 1)[0] for value in old if value.startswith("gurinie/"))
    alerts.update(value for value in old if value.split(": ", 1)[0] in failed - successful)
    report["active_alerts"] = sorted(alerts)
    return [*(f"NEW: {value}" for value in sorted(alerts - old)), *(f"RECOVERED: {value}" for value in sorted(old - alerts))]


def run(owner: str, *, included: int, reserve: int, expected_bundle: dict | None = None,
        previous: dict | None = None) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    report = {"checked_at_utc": now.isoformat(), "owner": owner, "repositories": [], "unavailable": [],
              "audit_unavailable_repositories": []}
    try:
        measured_allowance = included_allowance(owner)
        if measured_allowance != included:
            report["unavailable"].append("configured allowance differs from the live account plan")
            included = measured_allowance
        usage = api(f"/users/{owner}/settings/billing/usage?year={now.year}&month={now.month}")
        report["billing"] = summarize(usage, included=included, reserve=reserve, now=now)
        report["billing"]["source"] = "live_github_billing_api"
    except (Unavailable, ValueError) as exc:
        report["billing"] = None
        report["unavailable"].append(f"billing unavailable: {exc}")
    try:
        repos = [repo for repo in api("/user/repos?per_page=100", paginate=True)
                 if repo["owner"]["login"] == owner and not repo["archived"]]
    except Unavailable as exc:
        report["unavailable"].append(str(exc))
        report["repository_listing_unavailable"] = True
        return report
    cached = dict((previous or {}).get("inventory_cache", {}))
    cached.update({repo["repository"]: repo for repo in (previous or {}).get("repositories", [])})
    def inspect(repo):
        prior = cached.get(repo["full_name"])
        try:
            age = (now - dt.datetime.fromisoformat(prior["inventory_checked_at_utc"])).total_seconds()
            reusable = (0 <= age < 86400 and prior["default_branch"] == repo["default_branch"]
                        and prior.get("canonical_pin") == expected_bundle)
        except (KeyError, TypeError, ValueError):
            reusable = False
        if reusable:
            activity = inspect_activity(repo["full_name"], prior["workflow_count"], prior["manifest"])
            value = dict(prior, **activity)
            value["findings"] = sorted(set(prior["inventory_findings"] + activity["findings"]))
            value["inventory_source"] = "daily_cache"
            return value
        value = inspect_repository(repo, expected_bundle=expected_bundle)
        value["canonical_pin"] = expected_bundle
        value["inventory_source"] = "live_github"
        return value
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        future_repos = {pool.submit(inspect, repo): repo for repo in repos}
        for future in concurrent.futures.as_completed(future_repos):
            try:
                report["repositories"].append(future.result())
            except (Unavailable, ValueError, KeyError, TypeError, OSError) as exc:
                report["unavailable"].append(f"{future_repos[future]['full_name']}: audit unavailable ({exc})")
                report["audit_unavailable_repositories"].append(future_repos[future]["full_name"])
                prior = cached.get(future_repos[future]["full_name"])
                if prior:
                    report.setdefault("inventory_cache", {})[prior["repository"]] = prior
    report["repositories"].sort(key=lambda item: item["repository"])
    values = [item["pending_minutes_estimate"] for item in report["repositories"]]
    report["pending_minutes_estimate"] = None if any(value is None for value in values) or report['unavailable'] else sum(values)
    if report.get("billing"):
        report["billing"]["pending_minutes_estimate"] = report["pending_minutes_estimate"]
        billing = report["billing"]
        billing["warning_required"] |= (report["pending_minutes_estimate"] is None or
                                        billing["official_runner_minutes"] + report["pending_minutes_estimate"] > included - reserve)
    return report
