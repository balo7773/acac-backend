"""Uniform httpx clients for DeepSeek and Gemma.

Both return the same shape:
    {"parsed": dict, "raw": dict, "usage": dict, "model_version": str}

Gemma quirk: Gemma 4 always emits a thought block first (even with
thinkingLevel=minimal). We take the last non-thought, non-empty part.
DeepSeek quirk: content is a JSON string; parse defensively.
"""
import asyncio
import json
import httpx

from app.core.config import settings


class AIClientError(Exception):
    def __init__(self, message: str, category: str = "UNKNOWN"):
        super().__init__(message)
        self.category = category


async def _post_with_retry(url: str, payload: dict, headers: dict, *, timeout: float, max_retries: int = 3) -> dict:
    """POST with exponential backoff on 5xx and network errors.
    Returns parsed JSON response. Raises AIClientError on final failure."""
    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_error = f"network error: {e}"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise AIClientError(f"network failure after {max_retries} attempts: {e}", category="NETWORK")

        if resp.status_code == 200:
            return resp.json()

        # Permanent — no retry
        if resp.status_code in (401, 403):
            raise AIClientError(
                f"HTTP {resp.status_code}: {resp.text[:300]}", category="AUTH"
            )
        if resp.status_code == 402:
            raise AIClientError(
                f"HTTP 402: {resp.text[:300]}", category="QUOTA"
            )

        # Transient — retry with backoff
        if resp.status_code in (429, 500, 502, 503, 504):
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            cat = "RATE_LIMIT" if resp.status_code == 429 else "SERVER"
            raise AIClientError(
                f"{last_error} (after {max_retries} attempts)", category=cat
            )

        # Other 4xx — permanent
        raise AIClientError(
            f"HTTP {resp.status_code}: {resp.text[:300]}", category="CLIENT"
        )

    raise AIClientError(f"unreachable: {last_error}", category="UNKNOWN")


def _parse_json_content(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        if len(parts) > 1:
            text = parts[1]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass

    raise AIClientError(f"Could not parse JSON from model output: {content[:200]}", category="PARSE")


async def call_deepseek(
    prompt: str,
    *,
    temperature: float = 1.0,
    max_tokens: int = 4000,
    timeout: float = 60.0,
) -> dict:
    if not settings.DEEPSEEK_API_KEY:
        raise AIClientError("DEEPSEEK_API_KEY is not configured")

    payload = {
        "model": settings.DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    raw = await _post_with_retry(settings.DEEPSEEK_API_URL, payload, headers, timeout=timeout)
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise AIClientError(f"DeepSeek response shape unexpected: {e}; raw={str(raw)[:300]}", category="SHAPE")

    parsed = _parse_json_content(content)
    return {
        "parsed": parsed,
        "raw": raw,
        "usage": raw.get("usage", {}),
        "model_version": raw.get("model", settings.DEEPSEEK_MODEL),
    }


async def call_gemma(
    prompt: str,
    *,
    temperature: float = 1.0,
    max_tokens: int = 4000,
    timeout: float = 180.0,
) -> dict:
    if not settings.GEMMA_API_KEY:
        raise AIClientError("GEMMA_API_KEY is not configured")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    headers = {"x-goog-api-key": settings.GEMMA_API_KEY}
    raw = await _post_with_retry(settings.GEMMA_API_URL, payload, headers, timeout=timeout)
    try:
        parts = raw["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        raise AIClientError(f"Gemma response shape unexpected: {e}; raw={str(raw)[:300]}", category="SHAPE")

    content = ""
    for part in reversed(parts):
        if not part.get("thought", False) and part.get("text", "").strip():
            content = part["text"]
            break

    if not content:
        raise AIClientError(f"Gemma returned no usable content: {str(raw)[:300]}", category="PARSE")

    parsed = _parse_json_content(content)
    return {
        "parsed": parsed,
        "raw": raw,
        "usage": raw.get("usageMetadata", {}),
        "model_version": raw.get("modelVersion", settings.GEMMA_MODEL),
    }
