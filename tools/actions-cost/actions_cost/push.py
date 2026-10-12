"""Offline policy checks of outgoing commits; preserve existing hooks."""
from __future__ import annotations
import datetime as dt
import json
import subprocess
import tempfile
from pathlib import Path

from .policy import check, events, load_workflow

MARKER = "# github-actions-cost-managed-hook"
ZERO_SHA = "0" * 40


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def snapshot(root: Path, sha: str, destination: Path) -> None:
    for name in git(root, "ls-tree", "-r", "--name-only", sha).splitlines():
        if not (name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
                or name == ".github/actions-minutes-budget.json" or name.startswith("tools/actions-cost/")):
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe cost-control path in outgoing commit")
        mode = git(root, "ls-tree", sha, "--", name).split()[0]
        if mode not in {"100644", "100755"}:
            raise ValueError(f"cost-control files must be regular files: {name}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", f"{sha}:{name}"], cwd=root))


def _matches(pattern: str, value: str) -> bool | None:
    """Common GitHub glob syntax; unfamiliar syntax stays an advisory upper bound."""
    import re
    if any(character in pattern for character in ("[", "]", "+", "{", "}", "?")):
        return None
    result, index = "", 0
    while index < len(pattern):
        if pattern[index:index + 3] == "**/":
            result += "(?:.*/)?"; index += 3
        elif pattern[index:index + 2] == "**":
            result += ".*"; index += 2
        elif pattern[index] == "*":
            result += "[^/]*"; index += 1
        elif pattern[index] == "?":
            result += "[^/]"; index += 1
        else:
            result += re.escape(pattern[index]); index += 1
    return re.fullmatch(result, value) is not None


def _filter(patterns: list[str], value: str) -> bool | None:
    selected = False
    for pattern in patterns:
        negative = pattern.startswith("!")
        matched = _matches(pattern[1:] if negative else pattern, value)
        if matched is None:
            return None
        if matched:
            selected = not negative
    return selected


def _triggered(trigger: dict, event: str, branch: str, changed_paths: list[str] | None,
               activity: str | None) -> bool:
    if event not in trigger:
        return False
    options = trigger[event]
    if not isinstance(options, dict):
        return True
    if activity and options.get("types") and activity not in options["types"]:
        return False
    if options.get("branches") and _filter(options["branches"], branch) is False:
        return False
    if options.get("branches-ignore") and _filter(options["branches-ignore"], branch) is True:
        return False
    if changed_paths is not None:
        if options.get("paths") and not any(_filter(options["paths"], path) is not False for path in changed_paths):
            return False
        if options.get("paths-ignore") and all(_filter(options["paths-ignore"], path) is True for path in changed_paths):
            return False
    return True


def run_estimate(manifest: dict, remote_ref: str, root: Path | None = None, *,
                 event: str | None = None, changed_paths: list[str] | None = None,
                 activity: str | None = None, base_ref: str | None = None) -> float:
    """Advisory hosted upper bound after known event/ref/path filters, never admission."""
    branch = remote_ref.removeprefix("refs/heads/")
    workflows = manifest.get("workflows", {})
    total = 0.0
    for path, declaration in workflows.items():
        trigger = events(load_workflow((root / path).read_text())) if root else {}
        selected = False
        for candidate in ([event] if event else ["push", "pull_request", "pull_request_target"]):
            target = (base_ref or manifest["default_branch"]) if candidate.startswith("pull_request") else branch
            selected |= _triggered(trigger, candidate, target, changed_paths, activity)
        # No expression interpreter: unresolved job conditions remain upper bounds.
        if selected:
            total += sum(job["max_parallel_copies"] * job["estimated_minutes_per_copy"]
                         for job in declaration["jobs"].values() if job["runner"] != "self-hosted")
    return total


def approved(state: Path, repo: str, sha: str, ref: str, minutes: float, now: dt.datetime) -> bool:
    path = state / "approvals" / f"{sha}.json"
    try:
        value = json.loads(path.read_text())
        expires = dt.datetime.fromisoformat(value["expires_at_utc"])
        created = dt.datetime.fromisoformat(value["approved_at_utc"])
        return (value["repository"] == repo and value["commit"] == sha and value["ref"] == ref
                and value["period"] == now.strftime("%Y-%m") and value["max_minutes"] >= minutes
                and bool(value["approval_reference"]) and created <= now <= expires
                and 0 < (expires - created).total_seconds() <= 3600)
    except (OSError, KeyError, ValueError, TypeError):
        return False


def gate(root: Path, lines: list[str], *, state: Path, expected_pin: dict | None = None) -> list[str]:
    errors = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            errors.append("malformed pre-push ref update"); continue
        _, sha, ref, _ = fields
        if sha == ZERO_SHA or not ref.startswith("refs/heads/"):
            continue  # Release tags keep their existing operator gates.
        with tempfile.TemporaryDirectory(prefix="actions-push-") as temporary:
            candidate = Path(temporary)
            snapshot(root, sha, candidate)
            issues = check(candidate)
            errors += [f"{ref}: {issue}" for issue in issues]
            if issues:
                continue
            manifest_path = candidate / ".github/actions-minutes-budget.json"
            if not manifest_path.exists():
                continue  # No Actions workflows.
            manifest = json.loads(manifest_path.read_text())
            if not manifest["workflows"]:
                continue
            pin_path = candidate / "tools/actions-cost/bundle.json"
            if expected_pin is not None:
                from .policy import trusted_bundle
                if not trusted_bundle(json.loads(pin_path.read_text()), expected_pin):
                    errors.append(f"{ref}: bundled policy is not a known trusted content pin")
    return errors


def install(root: Path, controller: Path) -> Path:
    inherited = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=root, capture_output=True, text=True)
    local = subprocess.run(["git", "config", "--local", "--get", "core.hooksPath"], cwd=root, capture_output=True, text=True)
    if inherited.returncode == 0 and local.returncode != 0:
        raise ValueError("inherited hooksPath: review it before changing hooks shared with other repositories")
    value = local.stdout.strip() if local.returncode == 0 else git(root, "rev-parse", "--git-path", "hooks")
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = root / directory
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    common = (common if common.is_absolute() else root / common).resolve()
    directory = directory.resolve()
    if directory != common and common not in directory.parents and root.resolve() not in directory.parents:
        raise ValueError("hook directory is outside this repository; review shared hooks before changing them")
    # Do not rewrite tracked hooks. Preserve every original hook through a private wrapper directory.
    if local.returncode == 0 and directory.exists() and common not in directory.parents:
        import os
        original = directory
        directory = common / "hooks/actions-cost"
        directory.mkdir(parents=True, exist_ok=True)
        for item in original.iterdir():
            target = directory / item.name
            if item.name != "pre-push" and not target.exists():
                os.symlink(str(item.resolve()), target)
        backup = directory / "pre-push.actions-cost-previous"
        if (original / "pre-push").exists() and not backup.exists():
            os.symlink(str((original / "pre-push").resolve()), backup)
        subprocess.run(["git", "config", "--local", "core.hooksPath", str(directory)], cwd=root, check=True)
    directory.mkdir(parents=True, exist_ok=True)
    target, backup = directory / "pre-push", directory / "pre-push.actions-cost-previous"
    if target.exists() and MARKER not in target.read_text(errors="replace"):
        if backup.exists():
            raise ValueError("previous hook backup exists; preserve and review before installing")
        target.replace(backup)
    import shlex
    target.write_text("#!/bin/sh\n" + MARKER + "\nset -eu\n"
                      'input=$(mktemp)\ntrap \'rm -f "$input"\' EXIT\ncat > "$input"\n'
                      f'python3 {shlex.quote(str(controller))} push-gate --root "$(git rev-parse --show-toplevel)" < "$input"\n'
                      f'if [ -x {shlex.quote(str(backup.resolve()))} ]; then\n'
                      f'  {shlex.quote(str(backup.resolve()))} "$@" < "$input"\nfi\n')
    target.chmod(0o755)
    return target
