from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.config import Settings
from app.providers.cache import TTLCache
from app.providers.types import SeriesPoint

_MACRO_CACHE = TTLCache[SeriesPoint](max_size=200)


class MacroProvider:
    FRED_SERIES = {
        "VIX": "VIXCLS",
        "US10Y": "DGS10",
        "DXY": "DTWEXBGS",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_macro_snapshot(self) -> dict[str, SeriesPoint]:
        out: dict[str, SeriesPoint] = {}
        for label, series_id in self.FRED_SERIES.items():
            cache_key = f"macro:{label}"
            cached = _MACRO_CACHE.get(cache_key)
            if cached is not None:
                out[label] = cached
                continue

            point = await self._fetch_fred_series(series_id)
            if point:
                series_point = SeriesPoint(
                    symbol=label,
                    value=point["value"],
                    previous_value=point["previous_value"],
                    as_of=point["as_of"],
                    source="fred",
                )
                out[label] = series_point
                _MACRO_CACHE.set(cache_key, series_point, ttl_seconds=self.settings.macro_cache_ttl_seconds)
        return out

    async def _fetch_fred_series(self, series_id: str) -> dict[str, float | str | None] | None:
        end = date.today()
        start = end - timedelta(days=21)
        params = {
            "series_id": series_id,
            "file_type": "json",
            "sort_order": "asc",
            "observation_start": start.isoformat(),
            "observation_end": end.isoformat(),
        }
        if self.settings.fred_api_key:
            params["api_key"] = self.settings.fred_api_key
        url = "https://api.stlouisfed.org/fred/series/observations"

        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                observations = resp.json().get("observations", [])
        except Exception:
            return None

        parsed: list[tuple[str, float]] = []
        for row in observations:
            value = row.get("value")
            obs_date = row.get("date")
            if not value or value == "." or not obs_date:
                continue
            try:
                parsed.append((obs_date, float(value)))
            except (TypeError, ValueError):
                continue

        if not parsed:
            return None
        latest_date, latest_value = parsed[-1]
        prev_value = parsed[-2][1] if len(parsed) > 1 else None
        return {"value": latest_value, "previous_value": prev_value, "as_of": latest_date}
