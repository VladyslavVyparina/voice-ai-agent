import httpx
import time
from app.config import settings


def _is_transient_outbound_error(body: dict, raw_text: str) -> bool:
    message = str(body.get("message") or "")
    text = f"{message}\n{raw_text}"
    markers = [
        "Please retry your operation",
        "TransientTransactionError",
        "WriteConflict",
        "codeName': 'WriteConflict'",
    ]
    return any(marker in text for marker in markers)


def start_outbound_sip_call(
    to_number_e164: str,
    lead_id: int,
    lead_context: str,
    first_message: str,
    lead_name: str = "",
    lead_needs: str = "",
    call_briefing: str = "",
) -> dict:
    if not settings.elevenlabs_api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is missing")
    if not settings.elevenlabs_agent_id or not settings.elevenlabs_agent_phone_number_id:
        raise RuntimeError("ELEVENLABS_AGENT_ID or ELEVENLABS_AGENT_PHONE_NUMBER_ID is missing")

    briefing = (call_briefing or first_message or "").strip()
    url = "https://api.elevenlabs.io/v1/convai/sip-trunk/outbound-call"
    payload = {
        "agent_id": settings.elevenlabs_agent_id,
        "agent_phone_number_id": settings.elevenlabs_agent_phone_number_id,
        "to_number": to_number_e164,
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "lead_id": str(lead_id),
                "lead_context": lead_context,
                "lead_name": lead_name,
                "lead_needs": lead_needs,
                "call_briefing": briefing,
                # Extra aliases improve compatibility with different agent prompt templates.
                "customer_name": lead_name,
                "customer_need": lead_needs,
                "customer_context": lead_context,
                "telegram_context": lead_context,
                "briefing": briefing,
            },
            "conversation_config_override": {
                "agent": {
                    "first_message": first_message,
                }
            },
        },
    }
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "Content-Type": "application/json",
    }
    max_attempts = 4
    with httpx.Client(timeout=45.0) as client:
        for attempt in range(1, max_attempts + 1):
            resp = client.post(url, json=payload, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Retry only on transient server-side statuses.
                if exc.response.status_code in {500, 502, 503, 504} and attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue
                raise RuntimeError(
                    f"ElevenLabs outbound call failed {exc.response.status_code}: {exc.response.text}"
                ) from exc

            try:
                body = resp.json()
            except Exception:
                body = {}

            success = body.get("success")
            if success is False:
                if _is_transient_outbound_error(body, resp.text) and attempt < max_attempts:
                    time.sleep(0.8 * attempt)
                    continue
                raise RuntimeError(
                    "ElevenLabs outbound call rejected: "
                    f"{body.get('message') or resp.text}"
                )

            # Treat response without explicit success=False as accepted.
            if not body:
                raise RuntimeError("ElevenLabs outbound call returned empty response")
            return body

    raise RuntimeError("ElevenLabs outbound call failed after retries")
