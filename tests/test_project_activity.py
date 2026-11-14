"""Project-wide activity feed invariants without an HTTP test server."""
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest

os.environ.setdefault("TB_DATA_DIR", tempfile.mkdtemp(prefix="teambots-project-activity-"))
os.environ.setdefault("TB_SECRET_KEY", "test-secret")
os.environ.setdefault("TB_SANDBOX", "local")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app import agent, api  # noqa: E402
from app.db import Bot, Conversation, Endpoint, Message, Team, TeamMember, User, init_db, select, session  # noqa: E402


@pytest.fixture()
def project():
    """An isolated project with two published Team Bots and one non-member."""
    import app.db as db
    import app.home as home
    from sqlmodel import create_engine

    saved_db_engine, saved_home_engine = db.engine, home.engine
    db.engine = home.engine = create_engine(
        f"sqlite:///{tempfile.mkdtemp(prefix='teambots-project-activity-db-')}/test.db",
        connect_args={"check_same_thread": False},
    )
    init_db()
    with session() as s:
        member = User(username="member", display_name="Member", password_hash="x")
        outsider = User(username="outsider", display_name="Outsider", password_hash="x")
        s.add(member)
        s.add(outsider)
        s.commit()
        s.refresh(member)
        s.refresh(outsider)

        team = Team(name="Project", invite_code="project-activity-invite")
        s.add(team)
        s.commit()
        s.refresh(team)
        s.add(TeamMember(team_id=team.id, user_id=member.id, role="member"))

        endpoint = Endpoint(team_id=team.id, owner_id=member.id, name="test",
                            base_url="http://example.test/v1", default_model="test")
        s.add(endpoint)
        s.commit()
        s.refresh(endpoint)
        bots = [
            Bot(team_id=team.id, owner_id=member.id, name="Helper", handle="helper",
                endpoint_id=endpoint.id, is_team=True, published=True),
            Bot(team_id=team.id, owner_id=member.id, name="Researcher", handle="researcher",
                endpoint_id=endpoint.id, is_team=True, published=True),
        ]
        s.add_all(bots)
        s.commit()
        for bot in bots:
            s.refresh(bot)
        data = {"team_id": team.id, "member_id": member.id, "outsider_id": outsider.id,
                "bot_ids": [bot.id for bot in bots]}
    yield data
    db.engine = saved_db_engine
    home.engine = saved_home_engine


def test_activity_is_one_project_feed_and_appears_in_overview(project):
    feed = agent.project_feed(project["team_id"])
    assert feed.name == "Activity"
    assert feed.bot_id is None
    assert agent.project_feed(project["team_id"]).id == feed.id

    with session() as s:
        member = s.get(User, project["member_id"])
        outsider = s.get(User, project["outsider_id"])

    # The old per-bot URL remains a compatibility alias, not a second feed.
    assert api.team_feed(project["team_id"], member)["id"] == feed.id
    assert api.open_feed(project["bot_ids"][0], member)["id"] == feed.id
    with pytest.raises(HTTPException, match="project not found") as error:
        api.team_feed(project["team_id"], outsider)
    assert error.value.status_code == 404
    with session() as s:
        assert api._conv(s, feed.id, member).id == feed.id
        with pytest.raises(HTTPException, match="conversation not found") as error:
            api._conv(s, feed.id, outsider)
        assert error.value.status_code == 404

    with session() as s:
        s.add_all([
            Message(conversation_id=feed.id, role="assistant", content="**Helper** — done",
                    author_bot_id=project["bot_ids"][0]),
            Message(conversation_id=feed.id, role="assistant", content="**Researcher** — done",
                    author_bot_id=project["bot_ids"][1]),
        ])
        s.commit()

    overview = api.overview(member)
    assert {item["bot"] for item in overview[0]["activity"]} == {"Helper", "Researcher"}


def test_project_feed_is_unique_during_concurrent_first_access(project, monkeypatch):
    """Force two callers past the empty lookup before either may insert."""
    gate = Barrier(2)
    real_session = agent.session

    @contextmanager
    def racing_session():
        with real_session() as real:
            first_query = True

            class GatedSession:
                def exec(self, *args, **kwargs):
                    nonlocal first_query
                    result = real.exec(*args, **kwargs)
                    if first_query:
                        first_query = False
                        gate.wait(timeout=10)
                    return result

                def __getattr__(self, name):
                    return getattr(real, name)

            yield GatedSession()

    monkeypatch.setattr(agent, "session", racing_session)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: agent.project_feed(project["team_id"]).id, range(2)))

    assert len(set(ids)) == 1
    with session() as s:
        feeds = s.exec(select(Conversation).where(
            Conversation.team_id == project["team_id"], Conversation.kind == "feed",
            Conversation.bot_id.is_(None)
        )).all()
    assert [feed.id for feed in feeds] == [ids[0]]


def test_init_db_merges_duplicate_project_feeds_before_adding_index(project):
    """A database touched by the early WIP still becomes a one-feed project."""
    import app.db as db

    with db.engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX IF EXISTS uq_conversation_project_activity_feed")
    with session() as s:
        first = Conversation(team_id=project["team_id"], kind="feed", name="Activity")
        duplicate = Conversation(team_id=project["team_id"], kind="feed", name="Activity")
        s.add_all([first, duplicate])
        s.commit()
        s.refresh(first)
        s.refresh(duplicate)
        s.add_all([
            Message(conversation_id=first.id, role="assistant", content="first"),
            Message(conversation_id=duplicate.id, role="assistant", content="duplicate"),
        ])
        s.commit()

    init_db()
    with session() as s:
        feeds = s.exec(select(Conversation).where(
            Conversation.team_id == project["team_id"], Conversation.kind == "feed",
            Conversation.bot_id.is_(None)
        )).all()
        assert len(feeds) == 1
        messages = s.exec(select(Message).where(Message.conversation_id == feeds[0].id)).all()
        assert {message.content for message in messages} == {"first", "duplicate"}
        s.add(Conversation(team_id=project["team_id"], kind="feed", name="Activity"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
