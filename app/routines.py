"""Routines: a prompt (usually 'run skill X') that fires on a cron schedule."""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import agent, config
from .access import bot_role
from .db import Bot, Conversation, Routine, Team, now, select, session

log = logging.getLogger("teambots.routines")
scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))


async def fire(routine_id: int, *, force: bool = False) -> None:
    """Post the routine's prompt as its owner and let the bot work. Risky steps still wait for approval.

    Scheduled runs skip a paused routine. A manual "run now" passes force=True so it still runs once.
    """
    with session() as s:
        r = s.get(Routine, routine_id)
        if not r or (not r.enabled and not force):
            return
        bot = s.get(Bot, r.bot_id)
        if not bot or bot_role(s, r.owner_id, bot) is None:
            log.warning("routine %s skipped: owner lost access to the bot", routine_id)
            return
        r.last_run = now()
        s.add(r)
        s.commit()
    agent._post(r.conversation_id, role="user", content=f"[Routine: {r.name}] {r.prompt}", author_user_id=r.owner_id)
    agent.cancel_pending(r.conversation_id, r.bot_id)
    from . import usage
    with usage.bind(origin="routine", user_id=r.owner_id, team_id=bot.team_id, bot_id=r.bot_id,
                    conversation_id=r.conversation_id, endpoint_id=bot.endpoint_id):
        await agent.run_agent(r.conversation_id, r.bot_id, r.owner_id)


def sync(r: Routine) -> None:
    job_id = f"routine-{r.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if r.enabled:
        scheduler.add_job(fire, cron_trigger(r.cron),
                          args=[r.id], id=job_id, misfire_grace_time=300, coalesce=True)


def remove(routine_id: int) -> None:
    if scheduler.get_job(f"routine-{routine_id}"):
        scheduler.remove_job(f"routine-{routine_id}")


def start() -> None:
    if not scheduler.running:
        scheduler.start()
    with session() as s:
        for r in s.exec(select(Routine)).all():
            try:
                sync(r)
            except ValueError:
                log.warning("routine %s has an invalid cron expression", r.id)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

def _dow_std_to_aps(field: str) -> str:
    """Standard crontab weekday (0 and 7 = Sunday) → APScheduler 3.x (0 = Monday)."""
    if field in ("*", "?"):
        return field

    def std_num(n: str) -> int:
        v = int(n)
        if v == 7:
            v = 0
        if v < 0 or v > 6:
            raise ValueError(f"weekday {n} is out of range")
        return v

    def to_aps(std: int) -> int:
        return (std - 1) % 7  # Sun 0 → 6, Mon 1 → 0, … Sat 6 → 5

    def conv_token(tok: str) -> str:
        low = tok.lower()
        if any(c.isalpha() for c in low):
            return tok
        if tok.startswith("*/") and tok[2:].isdigit():
            step = int(tok[2:])
            if step <= 0:
                return tok
            return ",".join(str(to_aps(d)) for d in range(0, 7, step))
        if "-" in tok:
            a, b = tok.split("-", 1)
            if a.isdigit() and b.isdigit():
                ia, ib = std_num(a), std_num(b)
                sa, sb = to_aps(ia), to_aps(ib)
                if (ia <= ib and sa <= sb) or (ia > ib and sa > sb):
                    return f"{sa}-{sb}"
                days, cur = [], ia
                while True:
                    days.append(str(to_aps(cur)))
                    if cur == ib:
                        break
                    cur = (cur + 1) % 7
                return ",".join(days)
            return tok
        if tok.isdigit():
            return str(to_aps(std_num(tok)))
        return tok

    return ",".join(conv_token(t) for t in field.split(",") if t)


def cron_trigger(expr: str, timezone=None) -> CronTrigger:
    """Build a trigger from a standard 5-field crontab (0/7 = Sunday, 1-5 = weekdays)."""
    tz = timezone or ZoneInfo(config.TIMEZONE)
    parts = (expr or "").split()
    if len(parts) != 5:
        return CronTrigger.from_crontab(expr, timezone=tz)
    minute, hour, day, month, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=_dow_std_to_aps(dow), timezone=tz)


def describe_cron(expr: str) -> str:
    """Turn a 5-field crontab into a short English phrase. Falls back to the raw expression."""
    parts = (expr or "").split()
    if len(parts) != 5:
        return expr
    minute, hour, day, month, dow = parts
    dow_names = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
                 "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
                 "sun": "Sunday", "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                 "thu": "Thursday", "fri": "Friday", "sat": "Saturday"}
    mon_names = {"1": "January", "2": "February", "3": "March", "4": "April", "5": "May",
                 "6": "June", "7": "July", "8": "August", "9": "September", "10": "October",
                 "11": "November", "12": "December"}

    def _token(tok: str, names: dict[str, str]) -> str:
        low = tok.lower()
        if low in names:
            return names[low]
        if "-" in tok and not tok.startswith("*"):
            a, b = tok.split("-", 1)
            return f"{_token(a, names)}–{_token(b, names)}"
        return tok

    def _join(field: str, names: dict[str, str]) -> str:
        return ", ".join(_token(t, names) for t in field.split(",") if t)

    if hour == "*" and day == "*" and month == "*" and dow == "*":
        if minute == "*":
            return "every minute"
        if minute.startswith("*/") and minute[2:].isdigit():
            n = int(minute[2:])
            return f"every {n} minutes" if n != 1 else "every minute"
        if minute.isdigit():
            return f"every hour at minute {int(minute):02d}"
        return expr

    time = None
    if minute.isdigit() and hour.isdigit():
        time = f"{int(hour):02d}:{int(minute):02d}"
    elif hour.startswith("*/") and hour[2:].isdigit() and (minute == "0" or minute.isdigit()):
        n = int(hour[2:])
        mm = int(minute) if minute.isdigit() else 0
        when = f"every {n} hours"
        return f"{mm:02d} minutes past {when}" if mm else when

    bits = []
    if dow not in ("*", "?"):
        compact = dow.lower().replace(" ", "")
        if compact in ("1-5", "mon-fri", "1,2,3,4,5"):
            bits.append("on weekdays")
        elif compact in ("0,6", "6,0", "0,6,7", "0,7,6", "6,7,0", "sat,sun", "sun,sat"):
            bits.append("on weekends")
        else:
            bits.append("on " + _join(dow, dow_names))
    if day not in ("*", "?"):
        if day.isdigit():
            bits.append(f"on day {int(day)}")
        else:
            bits.append(f"on day {day}")
    if month != "*":
        bits.append("in " + _join(month, mon_names))

    if time and not bits:
        return f"every day at {time}"
    if time:
        return f"{time} {' '.join(bits)}"
    return expr


def next_run_at(cron: str, after: datetime | None = None):
    """Next fire time in the configured timezone, or None if the expression is invalid."""
    tz = ZoneInfo(config.TIMEZONE)
    try:
        trigger = cron_trigger(cron, timezone=tz)
    except ValueError:
        return None
    now = after or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    return trigger.get_next_fire_time(None, now)


def format_local(dt: datetime | None) -> str | None:
    """Format a timestamp in the server timezone, e.g. 'Mon 2026-11-16 08:30 CET'."""
    if dt is None:
        return None
    tz = ZoneInfo(config.TIMEZONE)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z")


def _conversation_name(bot: Bot | None, conv: Conversation | None) -> str:
    if not conv:
        return ""
    if conv.kind == "channel":
        return f"#{conv.name}"
    if conv.kind == "home":
        return "Overview"
    if conv.kind == "feed":
        return conv.name or "Activity"
    handle = f"@{bot.handle}" if bot else "the bot"
    return f"private chat with {handle}"


def view(s, r: Routine) -> dict:
    """JSON for the UI and the overview bot: schedule in words, next/last run, bot and project."""
    bot = s.get(Bot, r.bot_id)
    conv = s.get(Conversation, r.conversation_id)
    team = s.get(Team, bot.team_id) if bot and bot.team_id else None
    nxt = next_run_at(r.cron) if r.enabled else None
    out = r.model_dump(mode="json")
    out.update({
        "bot_name": bot.name if bot else "?",
        "bot_handle": bot.handle if bot else "",
        "is_home": bool(bot and bot.is_home),
        "project": "Overview" if (bot and bot.is_home) else (team.name if team else ""),
        "team_id": (bot.team_id if bot else 0) or 0,
        "conversation_name": _conversation_name(bot, conv),
        "conversation_kind": conv.kind if conv else "",
        "cron_text": describe_cron(r.cron),
        "next_run": nxt.isoformat() if nxt else None,
        "next_run_text": format_local(nxt),
        "last_run_text": format_local(r.last_run),
        "timezone": config.TIMEZONE,
    })
    return out
