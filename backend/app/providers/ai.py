from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import Settings


class AiProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def build_intelligence(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.settings.openai_api_key:
            return None

        system_prompt = (
            "You are a portfolio risk analyst. Return ONLY valid JSON with keys: "
            "thesis (string <= 180 chars), stance (one of: risk-on, balanced, risk-off), "
            "focus (array of up to 3 short strings). Be factual and do not invent data."
        )
        user_prompt = (
            "Analyze this portfolio snapshot and distill the most relevant risk posture.\n"
            f"JSON input:\n{json.dumps(payload, default=str)}"
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
            text = data.get("output_text")
            if not isinstance(text, str) or not text.strip():
                return None
            parsed = _extract_json(text)
            if not parsed:
                return None
            thesis = parsed.get("thesis")
            stance = parsed.get("stance")
            focus = parsed.get("focus")
            if not isinstance(thesis, str) or not thesis.strip():
                return None
            if stance not in {"risk-on", "balanced", "risk-off"}:
                stance = "balanced"
            if not isinstance(focus, list):
                focus = []
            clean_focus = [item.strip() for item in focus if isinstance(item, str) and item.strip()][:3]
            return {"thesis": thesis.strip(), "stance": stance, "focus": clean_focus}
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
