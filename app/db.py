from contextlib import contextmanager

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Candidate, Connection, Control, PolicyRevision

DEFAULT_CONTROLS = {
    "paused": False, "daily_cap": 0, "daily_budget_cents": 0, "monthly_budget_cents": 0,
    "daily_request_cap": 1000, "daily_token_cap": 0, "retention_days": 90,
    "followups_enabled": False, "timezone": "Africa/Casablanca",
}
DEFAULT_POLICY = {
    "mode": "draft_only", "approved": False, "allowed_countries": [],
    "allowed_tracks": [], "allowed_routes": [], "allowed_domains": [],
    "allowed_document_families": [], "approved_answers": [], "reviewed_packet_ids": [],
    "max_verification_minutes": 30,
}


class Database:
    def __init__(self, url: str):
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_settings(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")

    def initialize(self):
        self.migrate()
        with self.write() as session:
            if not session.get(Control, "global"):
                session.add(Control(id="global", data=DEFAULT_CONTROLS.copy()))
            if not session.get(Candidate, "owner"):
                session.add(Candidate(id="owner", name="Abdessamad Jaouad", data={"preferences": {
                    "excluded_countries": ["MA"], "preferred_countries": [],
                    "tracks": ["data_ai", "software", "data_bi", "academic"],
                    "unknown_sponsorship": "review", "relocation_required": True,
                }}))
            if not session.scalar(select(PolicyRevision).limit(1)):
                session.add(PolicyRevision(id="draft-initial", data=DEFAULT_POLICY.copy()))
            for name in ("gmail", "search", "openai", "browser"):
                if not session.get(Connection, name):
                    session.add(Connection(id=name, status="unconfigured", data={}))

    def migrate(self):
        from alembic import command
        from alembic.config import Config
        from app.config import ROOT
        config = Config(str(ROOT / "alembic.ini"))
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(7359051301)"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def read(self):
        with Session(self.engine, expire_on_commit=False) as session:
            yield session

    @contextmanager
    def write(self):
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    # Single-owner mutex covers policy/pause/quota/attempt/manual cancellation races.
                    session.execute(text("SELECT pg_advisory_xact_lock(7359051302)"))
                    session.execute(select(Control).where(Control.id == "global").with_for_update())
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
