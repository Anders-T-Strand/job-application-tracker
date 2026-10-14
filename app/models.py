"""Shared domain types."""

from typing import Literal

from pydantic import BaseModel, Field

STATUSES: tuple[str, ...] = ("applied", "oa", "screen", "interview", "offer", "rejected", "ghosted")
OPEN_STATUSES: tuple[str, ...] = ("applied", "oa", "screen", "interview")  # still waiting to hear

EmailType = Literal[
    "application_confirmation",
    "rejection",
    "oa_invite",
    "interview_invite",
    "recruiter_outreach",
    "offer",
    "other",
]

# classification type -> application status it implies
STATUS_FOR_TYPE: dict[str, str] = {
    "oa_invite": "oa",
    "interview_invite": "interview",
    "rejection": "rejected",
    "offer": "offer",
}


class EmailClassification(BaseModel):
    is_job_related: bool
    type: EmailType
    company: str | None = None
    role_title: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class JobPosting(BaseModel):
    """Company/role extracted from a job posting's URL + title (quick-log flow)."""

    company: str | None = None
    role_title: str | None = None
