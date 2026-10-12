"""Authenticated, bounded, read-only GitHub requests."""
from __future__ import annotations
import json
import subprocess


class Unavailable(RuntimeError):
    pass


def included_allowance(owner: str) -> int:
    profile = api("/user")
    if profile.get("login") != owner:
        raise Unavailable("billing owner differs from the authenticated personal account")
    allowance = {"pro": 3000, "free": 2000}.get(profile.get("plan", {}).get("name"))
    if allowance is None:
        raise Unavailable("included allowance unavailable for this account plan")
    return allowance


def api(path: str, *, paginate: bool = False, missing_ok: bool = False):
    args = ["gh", "api", path]
    if paginate:
        args += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=90 if paginate else 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable("GitHub query unavailable") from exc
    if result.returncode:
        if missing_ok and "HTTP 404" in result.stderr:
            return None
        raise Unavailable(f"GitHub query failed for {path.split('?')[0]}")
    try:
        data = json.loads(result.stdout)
        if not paginate:
            return data
        if all(isinstance(page, list) for page in data):
            return [item for page in data for item in page]
        if all(isinstance(page, dict) and isinstance(page.get("jobs"), list) for page in data):
            return [item for page in data for item in page["jobs"]]
        raise ValueError("unsupported pagination envelope")
    except (ValueError, TypeError) as exc:
        raise Unavailable("Unexpected GitHub response") from exc
