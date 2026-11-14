"""Log LLM calls, look up official/editable prices, and aggregate costs."""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from . import config
from .db import (AppSetting, Conversation, Endpoint, LlmUsage, ModelPrice, Team, TeamMember,
                 now, select, session)
from .prices_seed import PREMIUM_REQUEST_PRICE_USD, SEED

log = logging.getLogger("teambots.usage")

_ctx: ContextVar[Optional["UsageCtx"]] = ContextVar("llm_usage_ctx", default=None)

SETTING_KEYS = {
    "copilot_billing": "token",  # token (current official) | premium_request (legacy)
    "premium_request_price_usd": str(PREMIUM_REQUEST_PRICE_USD),
    "display_currency": "USD",  # USD | EUR
    "eur_per_usd": "",  # empty = do not convert; user must set a rate
    "electricity_enabled": "0",
    "electricity_per_hour": "",  # amount in electricity_currency
    "electricity_currency": "EUR",
}


def _tz():
    return ZoneInfo(config.TIMEZONE)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class UsageCtx:
    origin: str = "chat"  # chat, routine, ask_bot, voice, overview, test, feed, route
    user_id: Optional[int] = None
    team_id: Optional[int] = None
    bot_id: Optional[int] = None
    conversation_id: Optional[int] = None
    endpoint_id: Optional[int] = None
    billable_request: bool = True  # False for tool-follow-up rounds and retries


def current() -> Optional[UsageCtx]:
    return _ctx.get()


@contextmanager
def bind(**kwargs):
    """Set or overlay logging context for LLM calls in this task."""
    parent = _ctx.get()
    if parent is None:
        ctx = UsageCtx(**{k: v for k, v in kwargs.items() if k in UsageCtx.__dataclass_fields__})
    else:
        ctx = replace(parent, **{k: v for k, v in kwargs.items() if v is not None and k in UsageCtx.__dataclass_fields__})
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)


def norm_model(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def get_setting(key: str) -> str:
    default = SETTING_KEYS.get(key, "")
    with session() as s:
        row = s.get(AppSetting, f"costs_{key}")
        return row.value if row and row.value != "" else default


def set_setting(key: str, value: str) -> None:
    if key not in SETTING_KEYS:
        raise ValueError(f"unknown costs setting: {key}")
    with session() as s:
        row = s.get(AppSetting, f"costs_{key}") or AppSetting(key=f"costs_{key}")
        row.value = "" if value is None else str(value)
        s.add(row)
        s.commit()


def all_settings() -> dict:
    out = dict(SETTING_KEYS)
    with session() as s:
        for k in SETTING_KEYS:
            row = s.get(AppSetting, f"costs_{k}")
            if row and row.value != "":
                out[k] = row.value
    return out


def parse_usage(data: dict | None) -> dict:
    """Pull token counts out of a chat-completions (or similar) response body."""
    u = (data or {}).get("usage") if isinstance(data, dict) else None
    if not isinstance(u, dict):
        u = {}

    def _int(val) -> int:
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    prompt = _int(u.get("prompt_tokens") or u.get("input_tokens"))
    completion = _int(u.get("completion_tokens") or u.get("output_tokens"))
    prompt_details = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
    completion_details = u.get("completion_tokens_details") if isinstance(u.get("completion_tokens_details"), dict) else {}
    cached = _int(
        prompt_details.get("cached_tokens")
        or u.get("cached_tokens")
        or u.get("cache_read_input_tokens")
        or u.get("prompt_cache_hit_tokens")
    )
    cache_write = _int(
        prompt_details.get("cache_creation_tokens")
        or u.get("cache_creation_input_tokens")
        or u.get("cache_write_tokens")
        or u.get("prompt_cache_creation_tokens")
    )
    reasoning = _int(
        completion_details.get("reasoning_tokens")
        or u.get("reasoning_tokens")
        or u.get("thinking_tokens")
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": reasoning,
    }


def is_local_endpoint(ep: Endpoint | None, base_url: str = "", name: str = "") -> bool:
    """Local / lab endpoints (Spark, mock, RFC1918) are priced at zero."""
    url = (base_url or (ep.base_url if ep else "") or "").strip()
    label = (name or (ep.name if ep else "") or "").lower()
    if any(w in label for w in ("mock", "spark", "dgx")):
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback)
    except ValueError:
        return host.endswith(".local") or host.endswith(".lan")


def _price_row(s, provider: str, model: str, billing_mode: str) -> Optional[ModelPrice]:
    want = norm_model(model)
    if not want:
        return None
    rows = s.exec(select(ModelPrice).where(ModelPrice.billing_mode == billing_mode)).all()
    exact, loose = [], []
    for r in rows:
        keys = {norm_model(r.model)}
        for a in json.loads(r.aliases_json or "[]"):
            keys.add(norm_model(a))
        if want not in keys:
            continue
        (exact if (r.provider or "") == (provider or "") else loose).append(r)
    if exact:
        return exact[0]
    return loose[0] if loose else None


def price_for(provider: str, model: str, billing_mode: str = "token") -> Optional[ModelPrice]:
    with session() as s:
        return _price_row(s, provider, model, billing_mode)


def compute_cost(*, provider: str, model: str, endpoint: Endpoint | None,
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 cached_tokens: int = 0, cache_write_tokens: int = 0,
                 reasoning_tokens: int = 0, latency_ms: int = 0,
                 success: bool = True, billable_request: bool = True,
                 price_row: ModelPrice | None = None, settings: dict | None = None) -> dict:
    """Return a snapshot {cost_usd, cost_unknown, billing_mode, premium_requests, notes}."""
    def setting(key: str) -> str:
        if settings is not None:
            return settings[key] if key in settings else SETTING_KEYS.get(key, "")
        return get_setting(key)

    local = is_local_endpoint(endpoint)
    if local:
        cost = 0.0
        notes = "local endpoint (priced at 0)"
        if setting("electricity_enabled") == "1" and setting("electricity_per_hour"):
            try:
                rate = float(setting("electricity_per_hour"))
            except ValueError:
                rate = None
            cur = (setting("electricity_currency") or "EUR").upper()
            if rate is not None and rate >= 0 and latency_ms > 0:
                hours = latency_ms / 3_600_000
                elec = hours * rate
                if cur == "EUR":
                    fx = setting("eur_per_usd")
                    if fx:
                        try:
                            cost = elec / float(fx)
                            notes = f"electricity estimate {elec:.6f} EUR at {fx} EUR/USD"
                        except ValueError:
                            return dict(cost_usd=None, cost_unknown=True, billing_mode="electricity",
                                        premium_requests=0.0, notes="electricity rate currency EUR, no EUR/USD rate set")
                    else:
                        return dict(cost_usd=None, cost_unknown=True, billing_mode="electricity",
                                    premium_requests=0.0, notes="electricity is in EUR; set an EUR/USD rate to convert")
                elif cur == "USD":
                    cost = elec
                    notes = f"electricity estimate {elec:.6f} USD"
                else:
                    return dict(cost_usd=None, cost_unknown=True, billing_mode="electricity",
                                premium_requests=0.0, notes=f"unsupported electricity currency {cur}")
        return dict(cost_usd=cost, cost_unknown=False, billing_mode="zero",
                    premium_requests=0.0, notes=notes)

    provider = provider or (getattr(endpoint, "provider", None) if endpoint else "") or "openai"
    mode = "token"
    if provider == "github_copilot":
        mode = setting("copilot_billing") or "token"
        if mode not in ("token", "premium_request"):
            mode = "token"

    if price_row is not None:
        row = price_row
    else:
        with session() as s:
            row = _price_row(s, provider, model, mode)

    if mode == "premium_request":
        pr_price = setting("premium_request_price_usd")
        try:
            unit = float(pr_price)
        except ValueError:
            unit = None
        if not billable_request:
            return dict(cost_usd=0.0, cost_unknown=False, billing_mode=mode,
                        premium_requests=0.0,
                        notes="follow-up or retry; tokens are logged, no extra premium request")
        if row is None or row.premium_multiplier is None or unit is None:
            return dict(cost_usd=None, cost_unknown=True, billing_mode=mode,
                        premium_requests=0.0,
                        notes="price unknown: no official premium-request multiplier (or unit price) for this model")
        reqs = float(row.premium_multiplier)
        return dict(cost_usd=reqs * unit, cost_unknown=False, billing_mode=mode,
                    premium_requests=reqs,
                    notes=f"{reqs:g}× premium requests × ${unit}/request")

    # token billing
    if row is None or row.input_per_million is None or row.output_per_million is None:
        return dict(cost_usd=None, cost_unknown=True, billing_mode="token",
                    premium_requests=0.0, notes="price unknown: no official token price for this model")

    billed_prompt = max(int(prompt_tokens) - int(cached_tokens or 0), 0)
    missing = []
    cost = (billed_prompt / 1_000_000) * float(row.input_per_million)
    cost += (int(completion_tokens) / 1_000_000) * float(row.output_per_million)
    if cached_tokens:
        if row.cached_per_million is None:
            missing.append("cached")
        else:
            cost += (int(cached_tokens) / 1_000_000) * float(row.cached_per_million)
    if cache_write_tokens:
        if row.cache_write_per_million is None:
            missing.append("cache_write")
        else:
            cost += (int(cache_write_tokens) / 1_000_000) * float(row.cache_write_per_million)
    # reasoning tokens are already part of completion_tokens on the official pages we use
    if missing:
        return dict(cost_usd=None, cost_unknown=True, billing_mode="token",
                    premium_requests=0.0,
                    notes="price unknown: official page has no " + " / ".join(missing) + " rate for this model")
    return dict(cost_usd=cost, cost_unknown=False, billing_mode="token",
                premium_requests=0.0, notes="")


def log_call(*, endpoint: Endpoint | None, model: str, tokens: dict | None = None,
             latency_ms: int = 0, success: bool = True, error: str = "",
             retry: bool = False, raw: dict | None = None) -> Optional[LlmUsage]:
    """Persist one LLM attempt. Never raises into the caller."""
    try:
        ctx = current() or UsageCtx()
        tok = dict(tokens or {})
        if raw is not None and not tok:
            tok = parse_usage(raw)
        provider = (getattr(endpoint, "provider", None) if endpoint else None) or "openai"
        model_id = (model or (endpoint.default_model if endpoint else "") or "").strip()
        snap = compute_cost(
            provider=provider, model=model_id, endpoint=endpoint,
            prompt_tokens=int(tok.get("prompt_tokens") or 0),
            completion_tokens=int(tok.get("completion_tokens") or 0),
            cached_tokens=int(tok.get("cached_tokens") or 0),
            cache_write_tokens=int(tok.get("cache_write_tokens") or 0),
            reasoning_tokens=int(tok.get("reasoning_tokens") or 0),
            latency_ms=int(latency_ms or 0), success=success,
            billable_request=bool(ctx.billable_request) and not retry and success,
        )
        row = LlmUsage(
            created_at=now(),
            user_id=ctx.user_id,
            team_id=ctx.team_id if ctx.team_id is not None else (endpoint.team_id if endpoint else None),
            bot_id=ctx.bot_id,
            conversation_id=ctx.conversation_id,
            endpoint_id=ctx.endpoint_id or (endpoint.id if endpoint else None),
            provider=provider,
            model=model_id,
            prompt_tokens=int(tok.get("prompt_tokens") or 0),
            completion_tokens=int(tok.get("completion_tokens") or 0),
            cached_tokens=int(tok.get("cached_tokens") or 0),
            cache_write_tokens=int(tok.get("cache_write_tokens") or 0),
            reasoning_tokens=int(tok.get("reasoning_tokens") or 0),
            latency_ms=int(latency_ms or 0),
            success=bool(success),
            error=(error or "")[:500],
            retry=bool(retry),
            origin=ctx.origin or "chat",
            billing_mode=snap["billing_mode"],
            premium_requests=float(snap["premium_requests"] or 0),
            cost_usd=snap["cost_usd"],
            cost_unknown=bool(snap["cost_unknown"]),
            notes=(snap.get("notes") or "")[:500],
        )
        with session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
        return row
    except Exception:  # noqa: BLE001
        log.exception("failed to log LLM usage")
        return None


class Timer:
    def __init__(self):
        self.t0 = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)


def period_bounds(period: str, at: datetime | None = None) -> tuple[datetime, datetime]:
    """Inclusive-start, exclusive-end UTC window for today / 7d / 30d in the app timezone."""
    now_utc = _aware(at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = now_utc.astimezone(_tz())
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = today + timedelta(days=1)
    if period == "today":
        start = today
    elif period == "7d":
        start = today - timedelta(days=6)
    elif period == "30d":
        start = today - timedelta(days=29)
    else:
        raise ValueError("period must be today, 7d or 30d")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _sum_cost(rows: list[LlmUsage]) -> dict:
    known = [r.cost_usd for r in rows if r.cost_usd is not None and not r.cost_unknown]
    unknown = sum(1 for r in rows if r.cost_unknown or (r.cost_usd is None and r.success))
    return {
        "calls": len(rows),
        "success": sum(1 for r in rows if r.success),
        "errors": sum(1 for r in rows if not r.success),
        "prompt_tokens": sum(r.prompt_tokens for r in rows),
        "completion_tokens": sum(r.completion_tokens for r in rows),
        "cached_tokens": sum(r.cached_tokens for r in rows),
        "reasoning_tokens": sum(r.reasoning_tokens for r in rows),
        "premium_requests": sum(r.premium_requests for r in rows),
        "cost_usd": round(sum(known), 6) if known or not rows else 0.0,
        "unknown_calls": unknown,
    }


def _group(rows: list[LlmUsage], key_fn, label_fn) -> list[dict]:
    buckets: dict = {}
    order = []
    for r in rows:
        k = key_fn(r)
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(r)
    out = []
    for k in order:
        rec = _sum_cost(buckets[k])
        rec.update(label_fn(k, buckets[k]))
        out.append(rec)
    out.sort(key=lambda x: (-(x["cost_usd"] or 0), -x["calls"]))
    return out


def summarize(period: str, *, team_ids: list[int] | None, include_unassigned: bool = False,
              user_id: int | None = None) -> dict:
    start, end = period_bounds(period)
    with session() as s:
        rows = s.exec(select(LlmUsage).where(LlmUsage.created_at >= start, LlmUsage.created_at < end)).all()
        if team_ids is not None:
            allowed = set(team_ids)
            rows = [r for r in rows if (r.team_id in allowed) or (include_unassigned and r.team_id is None and (user_id is None or r.user_id == user_id))]
        bots = {b.id: b for b in s.exec(select(__import__("app.db", fromlist=["Bot"]).Bot)).all()} if False else None
        from .db import Bot
        bots = {b.id: b for b in s.exec(select(Bot)).all()}
        eps = {e.id: e for e in s.exec(select(Endpoint)).all()}
        convs = {c.id: c for c in s.exec(select(Conversation)).all()}
        teams = {t.id: t for t in s.exec(select(Team)).all()}

    totals = _sum_cost(rows)

    def bot_label(k, grp):
        b = bots.get(k)
        return {"id": k, "name": b.name if b else "(no bot)", "handle": getattr(b, "handle", "")}

    def model_label(k, grp):
        return {"model": k[1], "provider": k[0]}

    def ep_label(k, grp):
        e = eps.get(k)
        return {"id": k, "name": e.name if e else "(no endpoint)"}

    def origin_label(k, grp):
        return {"origin": k or "chat"}

    def conv_label(k, grp):
        c = convs.get(k)
        name = "(no conversation)"
        if c:
            name = c.name or f"{c.kind} {c.id}"
        return {"id": k, "name": name, "kind": getattr(c, "kind", "")}

    daily = []
    # bucket by local date
    by_day: dict[str, list] = {}
    for r in rows:
        day = _aware(r.created_at).astimezone(_tz()).date().isoformat()
        by_day.setdefault(day, []).append(r)
    # fill every day in the window
    start_l = start.astimezone(_tz()).date()
    end_l = (end - timedelta(seconds=1)).astimezone(_tz()).date()
    d = start_l
    while d <= end_l:
        key = d.isoformat()
        rec = _sum_cost(by_day.get(key, []))
        rec["date"] = key
        daily.append(rec)
        d += timedelta(days=1)

    expensive = _group([r for r in rows if r.conversation_id], lambda r: r.conversation_id, conv_label)[:10]

    return {
        "period": period,
        "timezone": config.TIMEZONE,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": totals,
        "by_bot": _group(rows, lambda r: r.bot_id, bot_label),
        "by_model": _group(rows, lambda r: (r.provider, r.model), model_label),
        "by_endpoint": _group(rows, lambda r: r.endpoint_id, ep_label),
        "by_origin": _group(rows, lambda r: r.origin or "chat", origin_label),
        "by_day": daily,
        "expensive_conversations": expensive,
    }


def bot_cost(bot_id: int, period: str = "30d") -> dict:
    start, end = period_bounds(period)
    with session() as s:
        rows = s.exec(select(LlmUsage).where(
            LlmUsage.bot_id == bot_id, LlmUsage.created_at >= start, LlmUsage.created_at < end)).all()
    return {"period": period, **_sum_cost(rows)}


def budget_status(team_id: int) -> dict:
    """Current calendar-month spend vs the project's monthly budget."""
    with session() as s:
        team = s.get(Team, team_id)
        budget = getattr(team, "monthly_budget", None) if team else None
        currency = (getattr(team, "budget_currency", None) or "USD") if team else "USD"
        block = bool(getattr(team, "budget_block", False)) if team else False
    start_local = datetime.now(_tz()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    start, end = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    with session() as s:
        rows = s.exec(select(LlmUsage).where(
            LlmUsage.team_id == team_id, LlmUsage.created_at >= start, LlmUsage.created_at < end)).all()
    spent_usd = _sum_cost(rows)["cost_usd"] or 0.0
    unknown = _sum_cost(rows)["unknown_calls"]
    spent = spent_usd
    if budget is not None and currency == "EUR":
        fx = get_setting("eur_per_usd")
        try:
            spent = spent_usd * float(fx) if fx else None
        except ValueError:
            spent = None
    ratio = None
    level = "ok"
    if budget and budget > 0 and spent is not None:
        ratio = spent / float(budget)
        if ratio >= 1:
            level = "over"
        elif ratio >= 0.8:
            level = "warn"
    return {
        "team_id": team_id,
        "budget": budget,
        "currency": currency,
        "block": block,
        "spent": spent,
        "spent_usd": spent_usd,
        "unknown_calls": unknown,
        "ratio": ratio,
        "level": level,
        "month_start": start.isoformat(),
    }


def should_block(team_id: int | None) -> Optional[str]:
    if not team_id:
        return None
    st = budget_status(team_id)
    if st["block"] and st["level"] == "over":
        return (f"This project has reached its monthly budget "
                f"({st['spent']} {st['currency']} of {st['budget']}).")
    return None


def seed_prices(*, overwrite_official: bool = False) -> dict:
    """Insert official seed rows. Never overwrites a user-edited row."""
    added = updated = skipped = 0
    with session() as s:
        existing = s.exec(select(ModelPrice)).all()
        index = {(r.provider, norm_model(r.model), r.billing_mode): r for r in existing}
        for item in SEED:
            key = (item["provider"], norm_model(item["model"]), item["billing_mode"])
            row = index.get(key)
            payload = dict(
                provider=item["provider"], model=item["model"],
                billing_mode=item["billing_mode"], currency=item["currency"],
                input_per_million=item["input_per_million"],
                output_per_million=item["output_per_million"],
                cached_per_million=item["cached_per_million"],
                cache_write_per_million=item["cache_write_per_million"],
                premium_multiplier=item["premium_multiplier"],
                source_url=item["source_url"], source_date=item["source_date"],
                notes=item.get("notes") or "",
                aliases_json=json.dumps(item.get("aliases") or []),
                user_edited=False,
                updated_at=now(),
            )
            if row is None:
                row = ModelPrice(**payload)
                s.add(row)
                index[key] = row
                added += 1
            elif row.user_edited:
                skipped += 1
            elif overwrite_official or row.source_date < item["source_date"]:
                for k, v in payload.items():
                    setattr(row, k, v)
                s.add(row)
                updated += 1
            else:
                skipped += 1
        s.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


def cache_models(endpoint: Endpoint, models: list[dict]) -> None:
    endpoint.models_json = json.dumps(models)
    endpoint.models_fetched_at = now()


def cached_models(endpoint: Endpoint) -> list[dict]:
    raw = getattr(endpoint, "models_json", None) or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def display_amount(usd: Optional[float], settings: dict | None = None) -> dict:
    """Convert a USD amount to the configured display currency when a rate is set."""
    settings = settings or all_settings()
    cur = (settings.get("display_currency") or "USD").upper()
    if usd is None:
        return {"amount": None, "currency": cur, "unknown": True}
    if cur == "EUR":
        fx = settings.get("eur_per_usd") or ""
        try:
            return {"amount": round(usd * float(fx), 6), "currency": "EUR", "unknown": False, "eur_per_usd": float(fx)}
        except (TypeError, ValueError):
            return {"amount": usd, "currency": "USD", "unknown": False, "note": "set an EUR/USD rate to show euros"}
    return {"amount": usd, "currency": "USD", "unknown": False}


def user_can_view_costs(s, user_id: int) -> bool:
    if s.exec(select(TeamMember).where(TeamMember.user_id == user_id, TeamMember.role == "admin")).first():
        return True
    from .db import Bot
    return s.exec(select(Bot).where(Bot.owner_id == user_id)).first() is not None


def user_admin_team_ids(s, user_id: int) -> list[int]:
    return [m.team_id for m in s.exec(select(TeamMember).where(TeamMember.user_id == user_id, TeamMember.role == "admin")).all()]
