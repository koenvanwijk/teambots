"""Who may do what. All permission decisions live here."""
from typing import Optional

from .db import Bot, BotAccess, ChannelMember, Conversation, Session, TeamMember, select


def team_role(s: Session, user_id: int, team_id: int) -> Optional[str]:
    m = s.exec(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id)).first()
    return m.role if m else None


def bot_role(s: Session, user_id: int, bot: Bot) -> Optional[str]:
    """owner | manager | collaborator | None. Drafts and personal bots are owner-only."""
    if bot.owner_id == user_id:
        return "owner"
    if not (bot.is_team and bot.published):
        return None
    if team_role(s, user_id, bot.team_id) is None:
        return None
    a = s.exec(select(BotAccess).where(BotAccess.bot_id == bot.id, BotAccess.user_id == user_id)).first()
    if a:
        return a.role
    return "collaborator" if bot.access == "team" else None


def can_manage(s: Session, user_id: int, bot: Bot) -> bool:
    return bot_role(s, user_id, bot) in ("owner", "manager")


def can_read_conversation(s: Session, user_id: int, conv: Conversation) -> bool:
    """DMs are private to their user: owners and managers of the bot cannot read them."""
    if conv.kind in ("dm", "home", "handoff"):
        # Handoff threads belong to the person the work is for (approvals land here).
        return conv.user_id == user_id
    if conv.kind == "feed":  # activity summaries
        if conv.bot_id is None:  # project-wide feed: any member of the project
            return team_role(s, user_id, conv.team_id) is not None
        bot = s.get(Bot, conv.bot_id)  # legacy per-bot feed: anyone who can use the bot
        return bot is not None and bot_role(s, user_id, bot) is not None
    return s.exec(select(ChannelMember).where(
        ChannelMember.conversation_id == conv.id, ChannelMember.user_id == user_id)).first() is not None
