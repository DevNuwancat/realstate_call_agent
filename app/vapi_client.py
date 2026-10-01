import httpx
from app.config import settings

VAPI_BASE_URL = "https://api.vapi.ai"


async def start_outbound_call(phone_number: str, lead_name: str | None, context: dict) -> dict:
    """
    Places an outbound call via Vapi using your pre-configured assistant
    (system prompt + ElevenLabs voice + Claude as the LLM — all set up in
    the Vapi dashboard or via the assistant_config.json in this repo).

    `context` values are injected into the assistant's system prompt as
    template variables, e.g. {{lead_name}}, {{property_address}}, so the
    same assistant can be reused for every lead without hardcoding anything.
    """
    payload = {
        "assistantId": settings.vapi_assistant_id,
        "phoneNumberId": settings.vapi_phone_number_id,
        "customer": {"number": phone_number, "name": lead_name or ""},
        "assistantOverrides": {
            "variableValues": {
                "lead_name": lead_name or "there",
                **context,
            }
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{VAPI_BASE_URL}/call",
            headers={
                "Authorization": f"Bearer {settings.vapi_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.vapi_api_key}",
        "Content-Type": "application/json",
    }


async def get_system_prompt() -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{VAPI_BASE_URL}/assistant/{settings.vapi_assistant_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        messages = data.get("model", {}).get("messages", [])
        for m in messages:
            if m.get("role") == "system":
                return m.get("content", "")
        return ""


async def get_first_message() -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{VAPI_BASE_URL}/assistant/{settings.vapi_assistant_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("firstMessage") or ""


def fill_agent_name(text: str, agent_name: str) -> str:
    """Swap the {{agent_name}} tag for the agent's real name."""
    name = (agent_name or "").strip() or "Noor"
    return text.replace("{{agent_name}}", name).replace("{{ agent_name }}", name)


async def set_system_prompt(new_prompt: str, first_message: str | None = None) -> dict:
    # `first_message` empty/None leaves the assistant's current first message alone.
    # Vapi expects the full `model` object on update, so we fetch the
    # current one first and only swap out the system message content —
    # this avoids accidentally wiping the model/provider settings.
    async with httpx.AsyncClient(timeout=15) as client:
        current = await client.get(
            f"{VAPI_BASE_URL}/assistant/{settings.vapi_assistant_id}",
            headers=_auth_headers(),
        )
        current.raise_for_status()
        model = current.json().get("model", {})
        messages = model.get("messages", [])

        found = False
        for m in messages:
            if m.get("role") == "system":
                m["content"] = new_prompt
                found = True
        if not found:
            messages.insert(0, {"role": "system", "content": new_prompt})
        model["messages"] = messages

        resp = await client.patch(
            f"{VAPI_BASE_URL}/assistant/{settings.vapi_assistant_id}",
            headers=_auth_headers(),
            json={"model": model, **({"firstMessage": first_message} if first_message else {})},
        )
        resp.raise_for_status()
        return resp.json()


async def get_call(vapi_call_id: str) -> dict:
    """Fetch one call from Vapi. Each fetch returns freshly signed recording
    URLs, so we use this instead of the (expiring) URL saved at call end."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{VAPI_BASE_URL}/call/{vapi_call_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def spend_since(iso_time: str) -> float:
    """Total cost of every call Vapi has recorded since `iso_time`, straight from Vapi.
    Cached for 30s so dashboard refreshes don't hammer the API."""
    import time
    cached = _SPEND_CACHE.get(iso_time)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    total, cursor = 0.0, iso_time
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(20):                       # at most 20 pages of 1000 calls
            resp = await client.get(
                f"{VAPI_BASE_URL}/call",
                headers=_auth_headers(),
                params={"createdAtGt": cursor, "limit": 1000},
            )
            resp.raise_for_status()
            calls = resp.json()
            if not calls:
                break
            total += sum((c.get("cost") or 0) for c in calls)
            if len(calls) < 1000:
                break
            cursor = max(c["createdAt"] for c in calls)
    _SPEND_CACHE[iso_time] = (time.time(), total)
    return total


_SPEND_CACHE: dict[str, tuple[float, float]] = {}
