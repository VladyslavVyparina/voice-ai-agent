import base64
import hashlib
import hmac
from urllib.parse import urlencode
import httpx
from app.config import settings


API_BASE = "https://api.zadarma.com"


def _build_signature(path: str, params: dict) -> str:
    # Official format from Zadarma support:
    # sign = base64_encode(hash_hmac('sha1', method . paramsStr . md5(paramsStr), secret))
    params_str = urlencode(sorted(params.items()))
    payload = f"{path}{params_str}{hashlib.md5(params_str.encode('utf-8')).hexdigest()}".encode("utf-8")
    secret = settings.zadarma_secret.encode("utf-8")
    digest_hex = hmac.new(secret, payload, hashlib.sha1).hexdigest()
    return base64.b64encode(digest_hex.encode("utf-8")).decode("utf-8")


def _request(method: str, path: str, params: dict) -> dict:
    if not settings.zadarma_key or not settings.zadarma_secret:
        raise RuntimeError("ZADARMA_KEY or ZADARMA_SECRET is missing")

    params = dict(params)
    params.setdefault("format", "json")
    params_str = urlencode(sorted(params.items()))
    url = f"{API_BASE}{path}"
    with httpx.Client(timeout=30.0) as client:
        signature = _build_signature(path, params)
        headers = {
            "Authorization": f"{settings.zadarma_key}:{signature}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if method.upper() == "POST":
            # Send exact body format from Zadarma support example: key=value&key2=value2
            resp = client.post(url, content=params_str, headers=headers)
        else:
            # Query string must match the one used for signature (official client behavior).
            resp = client.get(f"{url}?{params_str}", headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = exc.response.text
            except Exception:
                body = "<no body>"
            raise RuntimeError(
                f"Zadarma API error {exc.response.status_code} for {path}. "
                f"Request params={params}. Response={body}"
            ) from exc
        return resp.json()


def _normalize_phone(phone: str) -> str:
    # For this Zadarma account callback routing works with local UA format: 0XXXXXXXXX
    cleaned = "".join(ch for ch in (phone or "") if ch.isdigit())
    if cleaned.startswith("0") and len(cleaned) == 10:
        return cleaned
    if cleaned.startswith("380") and len(cleaned) == 12:
        return "0" + cleaned[3:]
    return cleaned


def start_callback_call(target_phone: str, first_prompt: str, lead_id: int) -> dict:
    to_value = _normalize_phone(target_phone)
    if len(to_value) != 10 or not to_value.startswith("0"):
        raise RuntimeError(
            f"Invalid lead phone for Zadarma callback: '{target_phone}' -> '{to_value}'. "
            "Expected local UA format 0XXXXXXXXX (10 digits)."
        )
    # For advanced PBX flow, scenario id should be used as from=0-<id>.
    if settings.zadarma_scenario_id:
        from_value = f"0-{settings.zadarma_scenario_id.strip()}"
    else:
        from_value = (settings.zadarma_from or "100").strip()
    payload = {"from": from_value, "to": to_value}
    if settings.zadarma_sip:
        payload["sip"] = settings.zadarma_sip.strip()
    return _request("GET", "/v1/request/callback/", payload)


def check_api_auth() -> dict:
    """Lightweight auth probe — balance endpoint, no side effects."""
    return _request("GET", "/v1/info/balance/", {})
