"""Operator interface. No command dispatches, cancels, or edits a GitHub run."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

from .billing import summarize
from .github import Unavailable, api, included_allowance
from .monitor import notification_changes, run
from .policy import check, projected_minutes
from .push import gate, install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "usage", "monitor", "install-hook", "push-gate", "record-approval"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--owner", default="gurinie")
    parser.add_argument("--included-minutes", type=int, default=3000)
    parser.add_argument("--reserve-minutes", type=int, default=500)
    parser.add_argument("--planned-minutes", type=float, default=0)
    parser.add_argument("--pending-minutes", type=float, default=0)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/github-actions-policy")
    parser.add_argument("--billing-json", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--repository")
    parser.add_argument("--ref")
    parser.add_argument("--approval-reference")
    parser.add_argument("--max-minutes", type=float)
    args = parser.parse_args(argv)
    if (args.included_minutes <= 0 or not 0 <= args.reserve_minutes <= args.included_minutes
            or any(not math.isfinite(value) or value < 0 for value in (args.planned_minutes, args.pending_minutes))):
        parser.error("invalid allowance/reserve/estimate")
    base = Path(__file__).resolve().parents[1]
    pin_path = base / "bundle.json"
    pin = json.loads(pin_path.read_text()) if pin_path.exists() else None
    try:
        if args.command == "check":
            errors = check(args.root.resolve())
            if errors:
                print("\n".join(errors), file=sys.stderr); return 1
            manifest = args.root / ".github/actions-minutes-budget.json"
            estimate = projected_minutes(json.loads(manifest.read_text())) if manifest.exists() else 0
            print(f"Actions cost policy passed; declared monthly hosted estimate: {estimate:g} minutes.")
        elif args.command == "usage":
            now = dt.datetime.now(dt.timezone.utc)
            payload = json.loads(args.billing_json.read_text()) if args.billing_json else api(
                f"/users/{args.owner}/settings/billing/usage?year={now.year}&month={now.month}")
            allowance = args.included_minutes if args.billing_json else included_allowance(args.owner)
            report = summarize(payload, included=allowance, reserve=args.reserve_minutes,
                               planned=args.planned_minutes, pending=args.pending_minutes, now=now)
            report["source"] = "saved_response" if args.billing_json else "live_github_billing_api"
            print(json.dumps(report, indent=2))
        elif args.command == "monitor":
            state_file = args.state / "monitor.json"
            try:
                previous = json.loads(state_file.read_text())
            except (OSError, ValueError):
                previous = None
            report = run(args.owner, included=args.included_minutes, reserve=args.reserve_minutes,
                         expected_bundle=pin, previous=previous)
            report["notifications"] = notification_changes(report, previous)
            args.state.mkdir(parents=True, exist_ok=True)
            temporary = state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(report, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            temporary.replace(state_file)
            print(json.dumps({"checked_at_utc": report["checked_at_utc"], "billing": report.get("billing"),
                              "repository_count": len(report["repositories"]), "notifications": report["notifications"]}, indent=2))
            return 2 if report["unavailable"] else 0
        elif args.command == "install-hook":
            print(install(args.root.resolve(), base / "check.py"))
        elif args.command == "push-gate":
            errors = gate(args.root.resolve(), sys.stdin.readlines(), state=args.state, expected_pin=pin)
            if errors:
                print("\n".join(errors), file=sys.stderr); return 3
        else:
            # This records operator evidence; it is not a substitute for human consent.
            import re
            if (not args.commit or not re.fullmatch(r"[a-f0-9]{40}", args.commit) or not args.repository
                    or not args.ref or not args.ref.startswith("refs/heads/") or not args.approval_reference
                    or args.max_minutes is None or not 0 < args.max_minutes <= 120):
                parser.error("record-approval requires exact repository, SHA, ref, bounded minutes and human approval reference")
            now = dt.datetime.now(dt.timezone.utc)
            evidence = {"repository": args.repository, "commit": args.commit, "ref": args.ref,
                        "max_minutes": args.max_minutes, "approval_reference": args.approval_reference,
                        "period": now.strftime("%Y-%m"), "approved_at_utc": now.isoformat(),
                        "expires_at_utc": (now + dt.timedelta(hours=1)).isoformat()}
            directory = args.state / "approvals"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{args.commit}.json"
            path.write_text(json.dumps(evidence, indent=2) + "\n"); path.chmod(0o600)
            print(path)
    except (Unavailable, ValueError, KeyError, TypeError, OSError) as exc:
        print(f"ACTIONS_COST_UNAVAILABLE: {exc}", file=sys.stderr); return 2
    return 0
