from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from app.config import Settings
from app.providers.cache import TTLCache

_AI_CACHE = TTLCache[dict[str, Any]](max_size=1200)


class AiProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def build_intelligence(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.settings.openai_api_key:
            return None
        payload_str = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:24]
        cache_key = f"ai:intel:{self.settings.openai_model}:{payload_hash}"
        cached = _AI_CACHE.get(cache_key)
        if cached is not None:
            return cached

        system_prompt = (
            "You are a portfolio risk analyst. Return ONLY valid JSON with keys: "
            "thesis (string <= 180 chars), stance (one of: risk-on, balanced, risk-off), "
            "focus (array of up to 3 short strings). Be factual and do not invent data."
        )
        user_prompt = (
            "Analyze this portfolio snapshot and distill the most relevant risk posture.\n"
            f"JSON input:\n{payload_str}"
        )
        body = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_output_tokens": 160,
        }
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            text = _extract_output_text(data)
            if not text:
                return None
            parsed = _extract_json(text)
            if parsed:
                thesis = parsed.get("thesis")
                stance = parsed.get("stance")
                focus = parsed.get("focus")
            else:
                # Fallback: keep the interface usable even when model returns plain text.
                thesis = text.strip()
                stance = "balanced"
                focus = []
            if not isinstance(thesis, str) or not thesis.strip():
                return None
            if stance not in {"risk-on", "balanced", "risk-off"}:
                stance = "balanced"
            if not isinstance(focus, list):
                focus = []
            clean_focus = [item.strip() for item in focus if isinstance(item, str) and item.strip()][:3]
            out = {"thesis": thesis.strip(), "stance": stance, "focus": clean_focus}
            _AI_CACHE.set(cache_key, out, ttl_seconds=self.settings.ai_cache_ttl_seconds)
            return out
        except Exception:
            return None


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_output_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    out: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_text = block.get("text")
            if isinstance(block_text, str) and block_text.strip():
                out.append(block_text.strip())
    return "\n".join(out).strip()
