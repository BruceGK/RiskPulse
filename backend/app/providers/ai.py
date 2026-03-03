from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from app.config import Settings
from app.providers.cache import TTLCache

_AI_CACHE = TTLCache[dict[str, Any]](max_size=1200)
_SEVERITIES = {"low", "medium", "high"}
_STANCES = {"risk-on", "balanced", "risk-off"}
_DIRECTIONS = {"risk-up", "risk-down", "neutral"}
_HORIZONS = {"intraday", "1w", "1m"}


class AiProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def build_signals(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.settings.openai_api_key:
            return None
        payload_str = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:24]
        cache_key = f"ai:signals:{self.settings.openai_model}:{payload_hash}"
        cached = _AI_CACHE.get(cache_key)
        if cached is not None:
            return cached

        system_prompt = (
            "You are a portfolio risk analyst. Return ONLY valid JSON with keys: "
            "pulse, warnings, watchouts, radar. Keep outputs concise, factual, and grounded in the provided data. "
            "Schema: pulse={thesis:string<=180, stance:risk-on|balanced|risk-off, focus:string[]<=3}; "
            "warnings=[{title, severity(low|medium|high), reason}]; "
            "watchouts=[{ticker, severity(low|medium|high), text}]; "
            "radar=[{title, impact(low|medium|high), direction(risk-up|risk-down|neutral), "
            "horizon(intraday|1w|1m), relatedTickers:string[]<=3}]."
        )
        user_prompt = (
            "Refine risk signals for this portfolio snapshot. Keep wording short and actionable.\n"
            f"JSON input:\n{payload_str}"
        )
        body = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_output_tokens": 380,
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
                out = _normalize_signals(parsed)
            else:
                # Do not pass raw malformed text through; caller already has deterministic signals.
                return None
            _AI_CACHE.set(cache_key, out, ttl_seconds=self.settings.ai_cache_ttl_seconds)
            return out
        except Exception:
            return None

    async def build_intelligence(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        # Backward-compatible helper.
        signals = await self.build_signals(payload)
        if not signals:
            return None
        pulse = signals.get("pulse")
        return pulse if isinstance(pulse, dict) else None


def _extract_json(text: str) -> dict[str, Any] | None:
    normalized = _strip_code_fences(text)
    try:
        data = json.loads(normalized)
        return data if isinstance(data, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", normalized, flags=re.DOTALL)
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


def _normalize_signals(data: dict[str, Any]) -> dict[str, Any]:
    pulse_in = data.get("pulse")
    pulse = {"thesis": "", "stance": "balanced", "focus": []}
    if isinstance(pulse_in, dict):
        thesis = pulse_in.get("thesis")
        stance = pulse_in.get("stance")
        focus = pulse_in.get("focus")
        if isinstance(thesis, str) and thesis.strip():
            clean = _sanitize_text(thesis)
            if clean:
                pulse["thesis"] = clean[:180]
        if stance in _STANCES:
            pulse["stance"] = stance
        if isinstance(focus, list):
            pulse["focus"] = [_sanitize_text(item) for item in focus if isinstance(item, str) and _sanitize_text(item)][:3]

    warnings: list[dict[str, str]] = []
    raw_warnings = data.get("warnings")
    if isinstance(raw_warnings, list):
        for row in raw_warnings[:6]:
            if not isinstance(row, dict):
                continue
            title = row.get("title")
            severity = row.get("severity")
            reason = row.get("reason")
            if not isinstance(title, str) or not title.strip():
                continue
            if severity not in _SEVERITIES:
                severity = "medium"
            if not isinstance(reason, str):
                reason = ""
            warnings.append(
                {
                    "title": _sanitize_text(title)[:90] or "Warning",
                    "severity": severity,
                    "reason": _sanitize_text(reason)[:220],
                }
            )

    watchouts: list[dict[str, str]] = []
    raw_watchouts = data.get("watchouts")
    if isinstance(raw_watchouts, list):
        for row in raw_watchouts[:12]:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            severity = row.get("severity")
            text = row.get("text")
            if not isinstance(ticker, str) or not ticker.strip():
                continue
            if severity not in _SEVERITIES:
                severity = "medium"
            if not isinstance(text, str) or not text.strip():
                continue
            clean_text = _sanitize_text(text)
            if not clean_text:
                continue
            watchouts.append({"ticker": ticker.strip().upper(), "severity": severity, "text": clean_text[:220]})

    radar: list[dict[str, Any]] = []
    raw_radar = data.get("radar")
    if isinstance(raw_radar, list):
        for row in raw_radar[:16]:
            if not isinstance(row, dict):
                continue
            title = row.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            clean_title = _sanitize_text(title)
            if not clean_title:
                continue
            impact = row.get("impact")
            direction = row.get("direction")
            horizon = row.get("horizon")
            related = row.get("relatedTickers")
            if impact not in _SEVERITIES:
                impact = "medium"
            if direction not in _DIRECTIONS:
                direction = "neutral"
            if horizon not in _HORIZONS:
                horizon = "1w"
            clean_related: list[str] = []
            if isinstance(related, list):
                clean_related = [x.strip().upper() for x in related if isinstance(x, str) and x.strip()][:3]
            radar.append(
                {
                    "title": clean_title[:140],
                    "impact": impact,
                    "direction": direction,
                    "horizon": horizon,
                    "relatedTickers": clean_related,
                }
            )

    return {
        "pulse": pulse,
        "warnings": warnings,
        "watchouts": watchouts,
        "radar": radar,
    }


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def _sanitize_text(text: str) -> str:
    cleaned = _strip_code_fences(text)
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip()
    if "{" in cleaned or "}" in cleaned:
        return ""
    return re.sub(r"\s{2,}", " ", cleaned)
