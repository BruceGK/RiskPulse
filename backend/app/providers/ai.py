from __future__ import annotations

import json

import httpx

from app.config import Settings


class AiProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def summarize(self, payload: dict) -> str | None:
        if not self.settings.openai_api_key:
            return None

        system_prompt = (
            "You are a portfolio risk analyst. Return one concise paragraph with key risk drivers, "
            "macro context, and one actionable next check. Be factual and avoid guessing."
        )
        user_prompt = f"Analyze this portfolio snapshot JSON:\n{json.dumps(payload, default=str)}"
        body = {
            "model": self.settings.openai_model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_output_tokens": 220,
        }
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}", "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
            text = data.get("output_text")
            if isinstance(text, str) and text.strip():
                return text.strip()
            return None
        except Exception:
            return None
