from pydantic import BaseModel, Field
from typing import Any, Optional


class StartCallRequest(BaseModel):
    """What you send to trigger an outbound call from your dashboard/backend."""
    phone_number: str = Field(..., description="E.164 format, e.g. +971501234567")
    lead_name: Optional[str] = None
    # Anything you want the assistant to know about this specific lead at call time,
    # e.g. property address, price range, source of the lead.
    context: dict[str, Any] = Field(default_factory=dict)


class CallRecord(BaseModel):
    """Row shape stored in Supabase `calls` table."""
    id: Optional[str] = None
    vapi_call_id: str
    phone_number: str
    lead_name: Optional[str] = None
    status: str = "queued"  # queued | in-progress | completed | failed
    interest_level: Optional[str] = None  # extracted after the call: hot | warm | cold | not-interested
    summary: Optional[str] = None
    transcript: Optional[str] = None
    recording_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    raw_payload: Optional[dict[str, Any]] = None


class TemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    prompt: str = Field(..., min_length=1, max_length=20000)
    first_message: str = Field("", max_length=1000)
    agent_name: str = Field("Noor", max_length=40)

