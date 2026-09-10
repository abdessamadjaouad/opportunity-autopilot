from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=1, max_length=1024)


class OpportunityInput(Input):
    url: str = Field(max_length=4000)
    title: str = Field(min_length=1, max_length=500)
    employer: str = Field(min_length=1, max_length=250)
    country: str = Field(default="", pattern=r"^([A-Z]{2})?$")
    track: Literal["data_ai", "software", "data_bi", "academic", "income_only"] = "data_ai"
    description: str = Field(default="", max_length=100000)
    language: Literal["en", "fr"] = "en"
    route: Literal["manual", "email", "browser", "candidate_api"] = "manual"


class FactInput(Input):
    value: Any
    status: Literal["confirmed"] = "confirmed"
    note: str = Field(default="", max_length=4000)


class AnswerInput(Input):
    key: str = Field(min_length=1, max_length=160)
    value: Any
    context: str = Field(default="global", max_length=200)
    sensitive: StrictBool = False
    consent: StrictBool = False


class PreferencesInput(Input):
    preferred_countries: list[str] | None = None
    excluded_countries: list[str] | None = None
    tracks: list[str] | None = None
    contract_types: list[str] | None = None
    salary_floor: str | None = None
    funding_floor: str | None = None
    citizenship: str | None = None
    work_authorizations: list[str] | None = None
    relocation_window: str | None = None
    start_window: str | None = None
    relocation_required: StrictBool | None = None
    relocation_assistance_required: StrictBool | None = None
    unknown_sponsorship: Literal["review", "research", "exclude"] | None = None
    sensitive_disclosure: str | None = None


class ResolutionInput(Input):
    resolution: str = Field(min_length=3, max_length=10000)
    answer_key: str | None = Field(default=None, max_length=160)
    answer_value: Any = None
    context: str | None = Field(default=None, max_length=200)


class ManualSubmissionInput(Input):
    evidence: str = Field(min_length=10, max_length=10000)
    reference: str = Field(default="", max_length=500)


class StateInput(Input):
    state: Literal["interview", "offer", "rejected", "withdrawn", "skipped"]
    note: str = Field(min_length=3, max_length=10000)


class PacketInput(Input):
    family: Literal["data_engineering", "ai_mlops", "software_devops", "data_bi", "academic"] | None = None
    language: Literal["en", "fr"] | None = None


class RequirementInput(Input):
    kind: str = Field(min_length=1, max_length=100)
    value: Any = None
    mandatory: StrictBool = True
    evidence: str = Field(default="", max_length=10000)
    max_words: int | None = Field(default=None, ge=1, le=100000, strict=True)
    max_pages: int | None = Field(default=None, ge=1, le=100, strict=True)
    max_bytes: int | None = Field(default=None, ge=1, le=10000000, strict=True)


class ReviewInput(Input):
    country: str | None = Field(default=None, pattern=r"^([A-Z]{2})?$")
    track: str | None = None
    requirements: list[RequirementInput] | None = Field(default=None, max_length=100)
    relocation_evidence: str | None = None
    route: Literal["manual", "email", "browser", "candidate_api"] | None = None
    destination: str | None = Field(default=None, max_length=500)
    evidence_url: str
    note: str = Field(min_length=3, max_length=10000)


class SourceInput(Input):
    name: str = Field(min_length=1, max_length=250)
    connector: Literal["greenhouse", "lever", "ashby", "cambridge", "rss", "html", "email_alert", "search"]
    url: str = Field(default="", max_length=4000)
    board_id: str = Field(default="", max_length=150, pattern=r"^[a-zA-Z0-9_-]*$")
    permission_note: str = Field(default="", max_length=5000)
    poll_interval_seconds: int = Field(default=900, ge=300, le=604800, strict=True)
    query: str = Field(default="", max_length=400)


class SourcePatch(Input):
    enabled: StrictBool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=300, le=604800, strict=True)


class ControlsInput(Input):
    paused: StrictBool | None = None
    daily_cap: int | None = Field(default=None, ge=0, le=100, strict=True)
    daily_budget_cents: int | None = Field(default=None, ge=0, le=100000, strict=True)
    monthly_budget_cents: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    daily_request_cap: int | None = Field(default=None, ge=0, le=100000, strict=True)
    daily_token_cap: int | None = Field(default=None, ge=0, le=10000000, strict=True)
    retention_days: int | None = Field(default=None, ge=7, le=3650, strict=True)


class PolicyInput(Input):
    mode: Literal["draft_only", "autopilot"] = "draft_only"
    allowed_countries: list[str] = Field(default_factory=list)
    allowed_tracks: list[str] = Field(default_factory=list)
    allowed_routes: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_document_families: list[str] = Field(default_factory=list)
    approved_answers: list[str] = Field(default_factory=list)
    reviewed_packet_ids: list[str] = Field(default_factory=list)
    daily_cap: int = Field(default=0, ge=0, le=100, strict=True)
    approve: StrictBool = False
    academic_two_pages_approved: StrictBool = False
    followup_enabled: StrictBool = False
    followup_max_count: int = Field(default=0, ge=0, le=1, strict=True)
    followup_after_days: int = Field(default=14, ge=7, le=60, strict=True)
    followup_answer_id: str = Field(default="", max_length=64)

    @field_validator("allowed_domains")
    @classmethod
    def exact_domains(cls, values):
        import re
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", x) or "." not in x for x in values):
            raise ValueError("Use exact lowercase domain names, without URLs or wildcards")
        return values


class EmailAlertInput(Input):
    raw_email: str = Field(min_length=1, max_length=200000)


class ReconciliationInput(Input):
    decision: Literal["confirm_receipt", "authorize_retry"]
    evidence: str = Field(min_length=20, max_length=10000)
    accept_duplicate_risk: StrictBool = False


class AssociationInput(Input):
    application_id: str
    evidence: str = Field(min_length=20, max_length=10000)
