import hmac
import hashlib
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.models import StartCallRequest, TemplateIn
from app.supabase_client import supabase
from app.vapi_client import start_outbound_call, get_system_prompt, get_first_message, set_system_prompt, fill_agent_name, get_call as fetch_vapi_call, spend_since

app = FastAPI(title="Real Estate Call Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Triggering calls
# ---------------------------------------------------------------------------

@app.post("/calls/start")
async def start_call(body: StartCallRequest):
    vapi_response = await start_outbound_call(
        phone_number=body.phone_number,
        lead_name=body.lead_name,
        context=body.context,
    )
    vapi_call_id = vapi_response.get("id")
    if not vapi_call_id:
        raise HTTPException(status_code=502, detail=f"Vapi did not return a call id: {vapi_response}")

    row = {
        "vapi_call_id": vapi_call_id,
        "phone_number": body.phone_number,
        "lead_name": body.lead_name,
        "status": "queued",
        "raw_payload": vapi_response,
    }
    supabase.table("calls").insert(row).execute()

    return {"vapi_call_id": vapi_call_id, "status": "queued"}


# ---------------------------------------------------------------------------
# Vapi webhook — this is where call results land
# ---------------------------------------------------------------------------

def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Optional but recommended: verify the request actually came from Vapi.
    Skips verification if you haven't set VAPI_WEBHOOK_SECRET yet."""
    if not settings.vapi_webhook_secret:
        return
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing signature header")
    expected = hmac.new(
        settings.vapi_webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")


INTEREST_LEVELS = {"hot", "warm", "cold", "not-interested"}


def _extract_interest(message: dict) -> str | None:
    """Vapi puts the extracted interest level in one of two places depending on
    whether the old 'structured data' or the new 'Structured Outputs' feature is used."""
    analysis = message.get("analysis") or {}
    structured = analysis.get("structuredData")
    candidates = []
    if isinstance(structured, dict):
        candidates.append(structured.get("interest_level"))
    outputs = (message.get("artifact") or {}).get("structuredOutputs") or {}
    if isinstance(outputs, dict):
        for item in outputs.values():
            if isinstance(item, dict) and item.get("name") == "interest_level":
                candidates.append(item.get("result"))
    for value in candidates:
        if isinstance(value, str) and value.strip().lower() in INTEREST_LEVELS:
            return value.strip().lower()
    return None


def _extract_output(message: dict, name: str) -> str | None:
    """Read one Structured Output field (e.g. 'call_summary') from the end-of-call report."""
    outputs = (message.get("artifact") or {}).get("structuredOutputs") or {}
    if isinstance(outputs, dict):
        for item in outputs.values():
            if isinstance(item, dict) and item.get("name") == name:
                value = item.get("result")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


@app.post("/webhooks/vapi")
async def vapi_webhook(request: Request, x_vapi_signature: str | None = Header(default=None)):
    raw_body = await request.body()
    _verify_signature(raw_body, x_vapi_signature)

    payload = await request.json()
    message = payload.get("message", payload)  # Vapi nests the event under "message"
    msg_type = message.get("type")

    call = message.get("call", {})
    vapi_call_id = call.get("id")
    if not vapi_call_id:
        # Nothing we can key on — ack anyway so Vapi doesn't retry forever
        return {"received": True, "ignored": True}

    update: dict = {"vapi_call_id": vapi_call_id}
    customer = call.get("customer") or {}
    update["phone_number"] = customer.get("number") or "browser-test"
    if call.get("assistantOverrides", {}).get("variableValues", {}).get("lead_name"):
        update["lead_name"] = call["assistantOverrides"]["variableValues"]["lead_name"]

    if msg_type == "status-update":
        update["status"] = message.get("status", "in-progress")

    elif msg_type == "end-of-call-report":
        update["status"] = "completed"
        analysis = message.get("analysis") or {}
        update["summary"] = (
            message.get("summary")
            or analysis.get("summary")
            or _extract_output(message, "call_summary")
        )
        update["transcript"] = message.get("transcript")
        artifact = message.get("artifact", {}) or {}
        update["recording_url"] = (
            artifact.get("presignedMonoUrl")
            or artifact.get("presignedStereoUrl")
            or message.get("recordingUrl")
            or message.get("stereoRecordingUrl")
        )
        duration = message.get("durationSeconds")
        update["duration_seconds"] = round(duration) if duration is not None else None
        update["raw_payload"] = message
        interest = _extract_interest(message)
        if interest:
            update["interest_level"] = interest

    else:
        # Log anything else (function-call, transcript deltas, etc.) for now.
        update["raw_payload"] = message

    if update:
        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("calls").upsert(update, on_conflict="vapi_call_id").execute()

    return {"received": True}


# ---------------------------------------------------------------------------
# Dashboard read endpoints
# ---------------------------------------------------------------------------

@app.get("/assistant/prompt")
async def read_prompt():
    prompt = await get_system_prompt()
    return {"prompt": prompt}


@app.put("/assistant/prompt")
async def update_prompt(body: dict):
    new_prompt = body.get("prompt", "").strip()
    if not new_prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")
    await set_system_prompt(new_prompt)
    return {"updated": True}


# ---------------------------------------------------------------------------
# Call script templates (saved system prompts; one is "active" = the live script)
# ---------------------------------------------------------------------------

TEMPLATES_SETUP_HINT = "Templates are not set up yet. Run the script_templates SQL in Supabase."


def _templates():
    return supabase.table("script_templates")


def _db_failure():
    return HTTPException(status_code=503, detail=TEMPLATES_SETUP_HINT)


@app.get("/templates")
async def list_templates():
    try:
        rows = _templates().select("*").order("created_at").execute().data
        if not rows:
            # First time: save the agent's current live script as the default template.
            try:
                current = await get_system_prompt()
            except httpx.HTTPError:
                raise HTTPException(status_code=502, detail="Could not read the current script")
            try:
                first = await get_first_message()
            except httpx.HTTPError:
                first = ""
            _templates().insert({"name": "Default template", "prompt": current or " ", "first_message": first, "active": True}).execute()
            rows = _templates().select("*").order("created_at").execute().data
        return rows
    except HTTPException:
        raise
    except Exception:
        raise _db_failure()


@app.post("/templates")
async def create_template(body: TemplateIn):
    try:
        res = _templates().insert({
            "name": body.name.strip(),
            "prompt": body.prompt,
            "first_message": body.first_message.strip(),
            "agent_name": body.agent_name.strip() or "Noor",
            "active": False,
        }).execute()
        return res.data[0]
    except Exception:
        raise _db_failure()


@app.put("/templates/{template_id}")
async def update_template(template_id: str, body: TemplateIn):
    try:
        res = _templates().update({
            "name": body.name.strip(),
            "prompt": body.prompt,
            "first_message": body.first_message.strip(),
            "agent_name": body.agent_name.strip() or "Noor",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", template_id).execute()
    except Exception:
        raise _db_failure()
    if not res.data:
        raise HTTPException(status_code=404, detail="Template not found")
    row = res.data[0]
    if row.get("active"):
        # Editing the live template changes the live script too.
        try:
            await set_system_prompt(
                fill_agent_name(body.prompt, row["agent_name"]),
                fill_agent_name(row["first_message"], row["agent_name"]),
            )
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Saved, but could not update the live agent")
    return row


@app.post("/templates/{template_id}/activate")
async def activate_template(template_id: str):
    try:
        found = _templates().select("*").eq("id", template_id).execute().data
    except Exception:
        raise _db_failure()
    if not found:
        raise HTTPException(status_code=404, detail="Template not found")
    try:
        await set_system_prompt(
            fill_agent_name(found[0]["prompt"], found[0].get("agent_name")),
            fill_agent_name(found[0].get("first_message") or "", found[0].get("agent_name")),
        )      # update the agent first; only then flip the flags
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not update the agent")
    try:
        _templates().update({"active": False}).eq("active", True).execute()
        res = _templates().update({"active": True}).eq("id", template_id).execute()
        return res.data[0]
    except Exception:
        raise _db_failure()


@app.delete("/templates/{template_id}")
async def delete_template(template_id: str):
    try:
        found = _templates().select("*").eq("id", template_id).execute().data
        if not found:
            raise HTTPException(status_code=404, detail="Template not found")
        if found[0].get("active"):
            raise HTTPException(status_code=400, detail="Switch to another template before deleting the active one")
        _templates().delete().eq("id", template_id).execute()
        return {"deleted": True}
    except HTTPException:
        raise
    except Exception:
        raise _db_failure()


@app.get("/usage")
async def usage_summary():
    """Total spend across all calls so far, pulled from each call's raw_payload.
    Vapi doesn't expose account credit balance via the API, so this tracks
    what THIS prototype has cost, not your overall Vapi account balance."""
    result = supabase.table("calls").select("raw_payload").execute()
    total_cost = 0.0
    call_count = 0
    for row in result.data:
        raw = row.get("raw_payload") or {}
        cost = raw.get("cost")
        if isinstance(cost, (int, float)):
            total_cost += cost
            call_count += 1
    credit_remaining = None
    if settings.credit_balance is not None:
        spent_since = None
        if settings.credit_balance_at:
            try:
                spent_since = await spend_since(settings.credit_balance_at)
            except Exception:
                spent_since = None                # fall back to the stored-calls estimate below
        if spent_since is None:
            spent_since = max(0.0, total_cost - settings.credit_balance_spend_at)
        credit_remaining = round(max(0.0, settings.credit_balance - spent_since), 2)
    return {
        "total_cost": round(total_cost, 4),
        "calls_counted": call_count,
        "credit_remaining": credit_remaining,
    }


@app.get("/calls")
async def list_calls(limit: int = 50):
    result = (
        supabase.table("calls")
        .select("id,vapi_call_id,phone_number,lead_name,status,interest_level,summary,transcript,duration_seconds,created_at,updated_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data


@app.get("/calls/{vapi_call_id}/media")
async def call_media(vapi_call_id: str):
    """Fresh recording link + timestamped transcript lines, fetched live from Vapi
    (the link saved when the call ended expires after about 30 minutes)."""
    try:
        data = await fetch_vapi_call(vapi_call_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Could not load the call recording")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="The recording service is unreachable")

    artifact = data.get("artifact") or {}
    recording_url = (
        artifact.get("presignedMonoUrl")
        or artifact.get("presignedStereoUrl")
        or artifact.get("recordingUrl")
    )
    lines = []
    for m in artifact.get("messages") or []:
        role, text = m.get("role"), (m.get("message") or "").strip()
        if role in ("bot", "user") and text:
            lines.append({
                "who": "agent" if role == "bot" else "lead",
                "text": text,
                "start": m.get("secondsFromStart"),
            })
    return {"recording_url": recording_url, "lines": lines}


@app.get("/calls/{vapi_call_id}")
async def get_call(vapi_call_id: str):
    result = supabase.table("calls").select("*").eq("vapi_call_id", vapi_call_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Call not found")
    return result.data[0]


@app.delete("/calls/{vapi_call_id}")
async def delete_call(vapi_call_id: str):
    result = supabase.table("calls").delete().eq("vapi_call_id", vapi_call_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"deleted": True}
