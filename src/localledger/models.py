"""Database tables. SQLite locally; any SQLAlchemy URL (e.g. Postgres) via DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    ticker: str = Field(primary_key=True)
    name: str
    mkt_cap_musd: float | None = None
    archetype: str = Field(index=True)
    local_gov_driver: str | None = Field(default=None, sa_column=Column(Text))
    public_data: str | None = Field(default=None, sa_column=Column(Text))
    note: str | None = Field(default=None, sa_column=Column(Text))
    analyst_coverage: str | None = None
    local_gov_intensity: float | None = None
    data_edge: float | None = None
    tradable: int | None = None
    coverage_pts: float | None = None
    priority: float | None = Field(default=None, index=True)
    size_band: str | None = None
    sheet_rank: int | None = None
    cik: str | None = Field(default=None, index=True)
    sec_name: str | None = None
    state: str | None = None
    sic: str | None = None
    added_at: datetime = Field(default_factory=utcnow)


class Archetype(SQLModel, table=True):
    __tablename__ = "archetypes"

    name: str = Field(primary_key=True)
    driver: str | None = Field(default=None, sa_column=Column(Text))
    datasets: str | None = Field(default=None, sa_column=Column(Text))
    intensity: float | None = None
    data_edge: float | None = None


class Source(SQLModel, table=True):
    __tablename__ = "sources"

    id: str = Field(primary_key=True)
    name: str
    type: str
    access_method: str = Field(sa_column=Column(Text))
    cadence: str
    terms_note: str = Field(sa_column=Column(Text))
    status: str = "unknown"  # unknown | available | degraded | unavailable
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    last_run_at: datetime | None = None


class Document(SQLModel, table=True):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("url", "sha256", name="uq_document_url_sha"),)

    id: int | None = Field(default=None, primary_key=True)
    source_id: str = Field(foreign_key="sources.id", index=True)
    ticker: str | None = Field(default=None, index=True)
    url: str = Field(sa_column=Column(Text, nullable=False))
    title: str | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
    sha256: str = Field(index=True)
    path: str
    text_path: str | None = None
    content_type: str = "application/octet-stream"
    origin: str = "real"  # real | fixture
    meta_json: str | None = Field(default=None, sa_column=Column(Text))


class Signal(SQLModel, table=True):
    __tablename__ = "signals"

    id: int | None = Field(default=None, primary_key=True)
    dedupe_key: str = Field(unique=True)
    ticker: str = Field(foreign_key="companies.ticker", index=True)
    source_id: str = Field(foreign_key="sources.id", index=True)
    as_of: date = Field(index=True)
    period: str | None = None
    metric: str
    unit: str | None = None
    value: float | None = None
    prior_value: float | None = None
    delta: float | None = None
    significance: float = 0.0
    rank_score: float = 0.0
    summary: str = Field(sa_column=Column(Text, nullable=False))
    citation_url: str = Field(sa_column=Column(Text, nullable=False))
    document_id: int = Field(foreign_key="documents.id", nullable=False, index=True)
    detail_json: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class Note(SQLModel, table=True):
    __tablename__ = "notes"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str | None = Field(default=None, index=True)
    kind: str = "signal"  # signal | company | warroom | newsletter
    signal_id: int | None = Field(default=None, foreign_key="signals.id")
    session_id: int | None = None
    title: str
    body_md: str = Field(sa_column=Column(Text, nullable=False))
    claims_json: str = Field(default="[]", sa_column=Column(Text))
    factcheck_log: str = Field(default="[]", sa_column=Column(Text))
    factcheck_passed: bool = False
    status: str = "draft"  # draft | approved | published
    disclosure_flag: bool = False
    model: str | None = None
    approved_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class WarroomSession(SQLModel, table=True):
    __tablename__ = "warroom_sessions"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(foreign_key="companies.ticker", index=True)
    title: str
    status: str = "pending"  # pending | running | complete | failed
    created_by: str = "cli"
    inputs_json: str = Field(default="{}", sa_column=Column(Text))
    final_note_id: int | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class WarroomEvent(SQLModel, table=True):
    __tablename__ = "warroom_events"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="warroom_sessions.id", index=True)
    agent: str | None = None
    type: str  # status | output | redirect | claim | release | edit | approve | presence | error
    actor: str = "system"
    payload_json: str = Field(default="{}", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class AgentThread(SQLModel, table=True):
    __tablename__ = "agent_threads"
    __table_args__ = (UniqueConstraint("session_id", "agent", name="uq_thread_session_agent"),)

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="warroom_sessions.id", index=True)
    agent: str
    status: str = "idle"  # idle | running | done | claimed | error
    claimed_by: str | None = None
    version: int = 0
    output_json: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utcnow)


class Presence(SQLModel, table=True):
    __tablename__ = "presence"
    __table_args__ = (UniqueConstraint("session_id", "user_name", name="uq_presence"),)

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True)
    user_name: str
    last_seen: datetime = Field(default_factory=utcnow)


class Subscriber(SQLModel, table=True):
    __tablename__ = "subscribers"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True)
    name: str | None = None
    firm: str | None = None
    role: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PilotRequest(SQLModel, table=True):
    __tablename__ = "pilot_requests"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    email: str
    fund: str
    aum_band: str | None = None
    interest: str | None = None
    message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class Lead(SQLModel, table=True):
    __tablename__ = "leads"

    id: int | None = Field(default=None, primary_key=True)
    cik: str = Field(unique=True)
    fund_name: str
    city: str | None = None
    state: str | None = None
    report_period: str | None = None
    overlap_count: int = 0
    overlap_value_usd: float = 0.0
    overlap_tickers: str = ""
    total_13f_value_usd: float | None = None
    is_mega: bool = False
    rank: int | None = None
    contact_status: str = "not_contacted"  # not_contacted | contacted
    updated_at: datetime = Field(default_factory=utcnow)


class LlmCall(SQLModel, table=True):
    __tablename__ = "llm_calls"

    id: int | None = Field(default=None, primary_key=True)
    command: str
    prompt_name: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: float = 0.0
    cached: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class NewsletterIssue(SQLModel, table=True):
    __tablename__ = "newsletter_issues"

    id: int | None = Field(default=None, primary_key=True)
    week: str = Field(unique=True)
    title: str
    path_md: str
    path_html: str
    signal_ids: str = ""
    note_ids: str = ""
    factcheck_passed: bool = False
    created_at: datetime = Field(default_factory=utcnow)


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
