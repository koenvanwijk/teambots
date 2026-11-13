"""Routines: a prompt (usually 'run skill X') that fires on a cron schedule."""
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import agent, config
from .access import bot_role
from .db import Bot, Routine, now, select, session

log = logging.getLogger("teambots.routines")
scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))


async def fire(routine_id: int) -> None:
    """Post the routine's prompt as its owner and let the bot work. Risky steps still wait for approval."""
    with session() as s:
        r = s.get(Routine, routine_id)
        if not r or not r.enabled:
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
    await agent.run_agent(r.conversation_id, r.bot_id, r.owner_id)


def sync(r: Routine) -> None:
    job_id = f"routine-{r.id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if r.enabled:
        scheduler.add_job(fire, CronTrigger.from_crontab(r.cron, timezone=ZoneInfo(config.TIMEZONE)),
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
