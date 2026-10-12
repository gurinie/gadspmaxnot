"""Official billing totals, distinct from operational duration estimates."""
from __future__ import annotations
import datetime as dt
import math
from collections import defaultdict


def summarize(payload: dict, *, included: int = 3000, reserve: int = 500,
              planned: float = 0, pending: float = 0, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    if (included <= 0 or not 0 <= reserve <= included or any(isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in (planned, pending))):
        raise ValueError('invalid allowance, reserve or planned/pending estimate')
    rows = payload.get("usageItems")
    if not isinstance(rows, list):
        raise ValueError("billing response has no usageItems list")
    repos, daily, skus = defaultdict(float), defaultdict(float), defaultdict(float)
    net = 0.0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid billing item")
        if str(row.get("product", "")).lower() != "actions" or str(row.get("unitType", "")).lower() != "minutes":
            continue
        quantity, amount = row.get("quantity"), row.get("netAmount")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
               for value in (quantity, amount)):
            raise ValueError("invalid Actions quantity or net amount")
        repo, sku = row.get("repositoryName"), row.get("sku")
        if not isinstance(repo, str) or not repo or not isinstance(sku, str) or not sku:
            raise ValueError("missing repository/runner SKU")
        date = str(row.get("date", ""))[:10]
        try:
            dt.date.fromisoformat(date)
        except ValueError as exc:
            raise ValueError("invalid billing date") from exc
        if date[:7] != now.strftime('%Y-%m'):
            raise ValueError('billing item belongs to a different month')
        repos[repo] += quantity
        skus[sku] += quantity
        daily[date] += quantity
        net += amount
    total = sum(repos.values())
    comparable = all(sku.lower() == "actions linux" for sku in skus)
    month_end = (now.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    elapsed = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400
    remaining_days = (month_end - now).total_seconds() / 86400
    recent_start = (now.date() - dt.timedelta(days=6)).isoformat()
    recent = sum(value for date, value in daily.items() if date >= recent_start) / min(7, max(1, now.day))
    forecast = total + recent * remaining_days
    warning = (not comparable or total >= included * 0.8 or total + planned + pending > included - reserve)
    return {"official_runner_minutes": total, "repositories": dict(sorted(repos.items(), key=lambda item: -item[1])),
            "runner_skus": dict(skus), "reported_net_usd": round(net, 8), "daily_minutes": dict(daily),
            "included_linux_minutes": included, "reserve_minutes": reserve,
            "remaining_linux_minutes": max(0, included - total) if comparable else None,
            "allowance_conversion_verified": comparable, "planned_minutes_estimate": planned,
            "pending_minutes_estimate": pending, "month_end_minutes_estimate": round(forecast, 1),
            "average_daily_minutes": round(total / max(elapsed, 1), 1),
            "warning_required": warning, "approval_required": False,
            "period": now.strftime("%Y-%m"), "checked_at_utc": now.isoformat()}


def fresh(snapshot: dict, now: dt.datetime) -> bool:
    try:
        age = (now - dt.datetime.fromisoformat(snapshot["checked_at_utc"])).total_seconds()
        return snapshot["source"] == "live_github_billing_api" and 0 <= age <= 600 and snapshot["period"] == now.strftime("%Y-%m")
    except (KeyError, ValueError, TypeError):
        return False
