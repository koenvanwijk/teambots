"""Cost tracking API: usage summaries, price table, settings, budgets."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import usage
from .access import team_role
from .api import current_user
from .db import Bot, Endpoint, ModelPrice, Team, User, now, select, session

router = APIRouter(prefix="/api")


def _need(cond, code=403, msg="not allowed"):
    if not cond:
        raise HTTPException(code, msg)


def _can_view(s, user: User) -> bool:
    return usage.user_can_view_costs(s, user.id)


def _scope(s, user: User, team_id: Optional[int]):
    """Teams this person may see costs for, plus whether unassigned (Overview) rows are included."""
    admin_ids = usage.user_admin_team_ids(s, user.id)
    owned_bot_teams = [b.team_id for b in s.exec(select(Bot).where(Bot.owner_id == user.id)).all() if b.team_id]
    visible = sorted(set(admin_ids) | set(owned_bot_teams))
    include_unassigned = True  # Overview / voice for this user
    if team_id is not None:
        _need(team_id in visible or team_role(s, user.id, team_id) == "admin", 404, "project not found")
        # members who aren't admin/owner shouldn't open the page; we already gated can_view
        if team_id not in visible and team_role(s, user.id, team_id) != "admin":
            raise HTTPException(403, "not allowed")
        return [team_id], False
    return visible, include_unassigned


def _money(usd, settings):
    return usage.display_amount(usd, settings)


@router.get("/costs")
def get_costs(period: str = "30d", team_id: Optional[int] = None, user: User = Depends(current_user)):
    _need(period in ("today", "7d", "30d"), 400, "period: today | 7d | 30d")
    with session() as s:
        _need(_can_view(s, user), 403, "only project admins and bot owners can view costs")
        team_ids, include_unassigned = _scope(s, user, team_id)
        budgets = []
        for tid in (team_ids or []):
            t = s.get(Team, tid)
            if t:
                st = usage.budget_status(tid)
                st["name"] = t.name
                budgets.append(st)
    data = usage.summarize(period, team_ids=team_ids, include_unassigned=include_unassigned, user_id=user.id)
    settings = usage.all_settings()
    data["settings"] = settings
    data["display"] = _money(data["totals"]["cost_usd"], settings)
    data["budgets"] = budgets
    data["can_edit"] = True  # gated again on write routes
    return data


@router.get("/costs/bots")
def get_bot_costs(period: str = "30d", team_id: Optional[int] = None, user: User = Depends(current_user)):
    """Compact per-bot totals for the cost indicator on the bot list."""
    _need(period in ("today", "7d", "30d"), 400, "period: today | 7d | 30d")
    with session() as s:
        if not _can_view(s, user):
            return {"period": period, "bots": []}
        team_ids, include_unassigned = _scope(s, user, team_id)
    data = usage.summarize(period, team_ids=team_ids, include_unassigned=include_unassigned, user_id=user.id)
    settings = usage.all_settings()
    bots = []
    for row in data["by_bot"]:
        shown = _money(row["cost_usd"], settings)
        bots.append({
            "id": row["id"], "name": row["name"], "handle": row.get("handle") or "",
            "calls": row["calls"], "cost": shown, "unknown_calls": row["unknown_calls"],
        })
    return {"period": period, "bots": bots, "currency": settings.get("display_currency") or "USD"}


class SettingsIn(BaseModel):
    copilot_billing: Optional[str] = None
    premium_request_price_usd: Optional[float] = None
    display_currency: Optional[str] = None
    eur_per_usd: Optional[str] = None  # empty string clears the rate
    electricity_enabled: Optional[bool] = None
    electricity_per_hour: Optional[str] = None
    electricity_currency: Optional[str] = None


@router.get("/costs/settings")
def get_settings(user: User = Depends(current_user)):
    with session() as s:
        _need(_can_view(s, user), 403, "not allowed")
    return usage.all_settings()


@router.patch("/costs/settings")
def patch_settings(body: SettingsIn, user: User = Depends(current_user)):
    with session() as s:
        _need(usage.user_admin_team_ids(s, user.id), 403, "only a project admin can change cost settings")
    data = body.model_dump(exclude_unset=True)
    if "copilot_billing" in data:
        _need(data["copilot_billing"] in ("token", "premium_request"), 400,
              "copilot_billing: token | premium_request")
        usage.set_setting("copilot_billing", data["copilot_billing"])
    if "premium_request_price_usd" in data:
        v = data["premium_request_price_usd"]
        _need(v is not None and v >= 0, 400, "premium_request_price_usd must be 0 or more")
        usage.set_setting("premium_request_price_usd", str(v))
    if "display_currency" in data:
        _need(data["display_currency"] in ("USD", "EUR"), 400, "display_currency: USD | EUR")
        usage.set_setting("display_currency", data["display_currency"])
    if "eur_per_usd" in data:
        raw = "" if data["eur_per_usd"] is None else str(data["eur_per_usd"]).strip()
        if raw:
            try:
                _need(float(raw) > 0, 400, "eur_per_usd must be a positive number")
            except ValueError:
                raise HTTPException(400, "eur_per_usd must be a positive number")
        usage.set_setting("eur_per_usd", raw)
    if "electricity_enabled" in data:
        usage.set_setting("electricity_enabled", "1" if data["electricity_enabled"] else "0")
    if "electricity_per_hour" in data:
        raw = "" if data["electricity_per_hour"] is None else str(data["electricity_per_hour"]).strip()
        if raw:
            try:
                _need(float(raw) >= 0, 400, "electricity_per_hour must be 0 or more")
            except ValueError:
                raise HTTPException(400, "electricity_per_hour must be 0 or more")
        usage.set_setting("electricity_per_hour", raw)
    if "electricity_currency" in data:
        _need(data["electricity_currency"] in ("USD", "EUR"), 400, "electricity_currency: USD | EUR")
        usage.set_setting("electricity_currency", data["electricity_currency"])
    return usage.all_settings()


def _price_out(r: ModelPrice) -> dict:
    return {
        "id": r.id, "provider": r.provider, "model": r.model, "billing_mode": r.billing_mode,
        "currency": r.currency,
        "input_per_million": r.input_per_million, "output_per_million": r.output_per_million,
        "cached_per_million": r.cached_per_million, "cache_write_per_million": r.cache_write_per_million,
        "premium_multiplier": r.premium_multiplier,
        "source_url": r.source_url, "source_date": r.source_date, "notes": r.notes,
        "aliases": __import__("json").loads(r.aliases_json or "[]"),
        "user_edited": r.user_edited, "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/costs/prices")
def list_prices(user: User = Depends(current_user)):
    with session() as s:
        _need(_can_view(s, user), 403, "not allowed")
        rows = s.exec(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model, ModelPrice.billing_mode)).all()
    return {"prices": [_price_out(r) for r in rows]}


class PricePatch(BaseModel):
    input_per_million: Optional[float] = None
    output_per_million: Optional[float] = None
    cached_per_million: Optional[float] = None
    cache_write_per_million: Optional[float] = None
    premium_multiplier: Optional[float] = None
    currency: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/costs/prices/{price_id}")
def patch_price(price_id: int, body: PricePatch, user: User = Depends(current_user)):
    with session() as s:
        _need(usage.user_admin_team_ids(s, user.id), 403, "only a project admin can edit prices")
        row = s.get(ModelPrice, price_id)
        _need(row, 404, "price row not found")
        data = body.model_dump(exclude_unset=True)
        if "currency" in data and data["currency"] not in (None, "USD", "EUR"):
            raise HTTPException(400, "currency: USD | EUR")
        for k, v in data.items():
            setattr(row, k, v)
        row.user_edited = True
        row.updated_at = now()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _price_out(row)


class PriceIn(BaseModel):
    provider: str
    model: str
    billing_mode: str = "token"
    currency: str = "USD"
    input_per_million: Optional[float] = None
    output_per_million: Optional[float] = None
    cached_per_million: Optional[float] = None
    cache_write_per_million: Optional[float] = None
    premium_multiplier: Optional[float] = None
    notes: str = ""
    source_url: str = ""
    source_date: str = ""


@router.post("/costs/prices")
def add_price(body: PriceIn, user: User = Depends(current_user)):
    _need(body.billing_mode in ("token", "premium_request"), 400, "billing_mode: token | premium_request")
    _need(body.currency in ("USD", "EUR"), 400, "currency: USD | EUR")
    _need(body.provider.strip() and body.model.strip(), 400, "provider and model are required")
    with session() as s:
        _need(usage.user_admin_team_ids(s, user.id), 403, "only a project admin can edit prices")
        row = ModelPrice(
            provider=body.provider.strip(), model=body.model.strip(),
            billing_mode=body.billing_mode, currency=body.currency,
            input_per_million=body.input_per_million, output_per_million=body.output_per_million,
            cached_per_million=body.cached_per_million, cache_write_per_million=body.cache_write_per_million,
            premium_multiplier=body.premium_multiplier,
            notes=body.notes or "Added in TeamBots",
            source_url=body.source_url, source_date=body.source_date,
            user_edited=True, updated_at=now(),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return _price_out(row)
