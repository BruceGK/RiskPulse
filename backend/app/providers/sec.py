from __future__ import annotations

import httpx

from app.config import Settings


class SecProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._ticker_map_cache: dict[str, str] | None = None

    async def get_latest_filing(self, ticker: str) -> dict[str, str] | None:
        cik = await self._resolve_cik(ticker)
        if not cik:
            return None
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        headers = {"User-Agent": self.settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            filings = data.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            dates = filings.get("filingDate", [])
            accessions = filings.get("accessionNumber", [])
            if not forms:
                return None
            return {
                "form": forms[0],
                "filing_date": dates[0] if dates else "",
                "accession": accessions[0] if accessions else "",
            }
        except Exception:
            return None

    async def _resolve_cik(self, ticker: str) -> str | None:
        ticker = ticker.upper().strip()
        if self._ticker_map_cache is None:
            self._ticker_map_cache = await self._load_ticker_map()
        return self._ticker_map_cache.get(ticker)

    async def _load_ticker_map(self) -> dict[str, str]:
        url = "https://www.sec.gov/files/company_tickers.json"
        headers = {"User-Agent": self.settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            return {}

        out: dict[str, str] = {}
        for row in payload.values():
            t = (row.get("ticker") or "").upper()
            cik = row.get("cik_str")
            if t and cik:
                out[t] = f"{int(cik):010d}"
        return out
