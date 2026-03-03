from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx

from app.config import Settings
from app.providers.cache import TTLCache
from app.providers.types import Quote

_QUOTE_CACHE = TTLCache[Quote](max_size=8000)
_HISTORY_CACHE = TTLCache[list[float]](max_size=6000)
_MISS_CACHE = TTLCache[bool](max_size=12000)


class MarketProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not tickers:
            return {}

        out: dict[str, Quote] = {}
        pending: list[str] = []
        for ticker in tickers:
            key = f"quote:{ticker}"
            cached = _QUOTE_CACHE.get(key)
            if cached is not None:
                out[ticker] = cached
                continue
            if _MISS_CACHE.get(key):
                continue
            pending.append(ticker)

        pending = await self._merge_quotes(out, pending, self._fetch_polygon_quotes)
        pending = await self._merge_quotes(out, pending, self._fetch_fmp_quotes)
        # Preserve Alpha Vantage quota for news fallback on free-tier setups.
        pending = await self._merge_quotes(out, pending, self._fetch_yahoo_quotes)
        pending = await self._merge_quotes(out, pending, self._fetch_alpha_vantage_quotes)

        for ticker in pending:
            _MISS_CACHE.set(f"quote:{ticker}", True, ttl_seconds=90)
        return out

    async def get_history(self, ticker: str, days: int) -> list[float]:
        key = f"hist:{ticker}:{days}"
        cached = _HISTORY_CACHE.get(key)
        if cached is not None:
            return cached
        if _MISS_CACHE.get(key):
            return []

        fetchers = (
            self._fetch_polygon_history,
            self._fetch_fmp_history,
            self._fetch_yahoo_history,
            self._fetch_alpha_vantage_history,
        )
        for fetcher in fetchers:
            history = await fetcher(ticker, days)
            if len(history) >= 2:
                _HISTORY_CACHE.set(key, history, ttl_seconds=self.settings.history_cache_ttl_seconds)
                return history

        _MISS_CACHE.set(key, True, ttl_seconds=300)
        return []

    async def _merge_quotes(self, out: dict[str, Quote], pending: list[str], fetcher) -> list[str]:
        if not pending:
            return pending
        fetched = await fetcher(pending)
        for symbol, quote in fetched.items():
            out[symbol] = quote
            _QUOTE_CACHE.set(f"quote:{symbol}", quote, ttl_seconds=self.settings.quote_cache_ttl_seconds)
        return [ticker for ticker in pending if ticker not in fetched]

    async def _fetch_polygon_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not self.settings.polygon_api_key:
            return {}

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            results: dict[str, Quote] = {}
            for ticker in tickers:
                url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
                params = {"apiKey": self.settings.polygon_api_key}
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    payload = resp.json().get("ticker", {})
                    current = payload.get("day", {}).get("c") or payload.get("min", {}).get("c")
                    prev = payload.get("prevDay", {}).get("c")
                    if current:
                        results[ticker] = Quote(
                            ticker=ticker,
                            price=float(current),
                            prev_close=float(prev) if prev else None,
                            source="polygon",
                        )
                except Exception:
                    continue
            return results

    async def _fetch_fmp_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not self.settings.fmp_api_key:
            return {}
        symbols = ",".join(tickers)
        url = f"https://financialmodelingprep.com/api/v3/quote/{symbols}"
        params = {"apikey": self.settings.fmp_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            out: dict[str, Quote] = {}
            for item in data:
                symbol = (item.get("symbol") or "").upper()
                price = item.get("price")
                prev = item.get("previousClose")
                if symbol and price:
                    out[symbol] = Quote(
                        ticker=symbol,
                        price=float(price),
                        prev_close=float(prev) if prev else None,
                        source="fmp",
                    )
            return out
        except Exception:
            return {}

    async def _fetch_yahoo_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        symbols = ",".join(tickers)
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        params = {"symbols": symbols}
        headers = {"User-Agent": "Mozilla/5.0 RiskPulse/1.0", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("quoteResponse", {}).get("result", [])
            out: dict[str, Quote] = {}
            for item in data:
                symbol = (item.get("symbol") or "").upper()
                price = item.get("regularMarketPrice")
                prev = item.get("regularMarketPreviousClose")
                if symbol and price:
                    out[symbol] = Quote(
                        ticker=symbol,
                        price=float(price),
                        prev_close=float(prev) if prev else None,
                        source="yahoo",
                    )
            return out
        except Exception:
            return {}

    async def _fetch_alpha_vantage_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not self.settings.alpha_vantage_api_key:
            return {}

        capped = tickers[: self.settings.alpha_vantage_max_calls_per_request]
        results: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            for ticker in capped:
                params = {
                    "function": "GLOBAL_QUOTE",
                    "symbol": ticker,
                    "apikey": self.settings.alpha_vantage_api_key,
                }
                try:
                    resp = await client.get("https://www.alphavantage.co/query", params=params)
                    resp.raise_for_status()
                    payload = resp.json()
                    if _alpha_vantage_is_limited(payload):
                        break
                    row = payload.get("Global Quote", {})
                    symbol = (row.get("01. symbol") or ticker).upper()
                    price = row.get("05. price")
                    prev = row.get("08. previous close")
                    if price:
                        results[symbol] = Quote(
                            ticker=symbol,
                            price=float(price),
                            prev_close=float(prev) if prev else None,
                            source="alpha_vantage",
                        )
                except Exception:
                    continue
        return results

    async def _fetch_polygon_history(self, ticker: str, days: int) -> list[float]:
        if not self.settings.polygon_api_key:
            return []
        end = date.today()
        start = end - timedelta(days=max(days * 2, 180))
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": self.settings.polygon_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json().get("results", [])
            closes = [float(item["c"]) for item in data if item.get("c")]
            return closes[-days:]
        except Exception:
            return []

    async def _fetch_fmp_history(self, ticker: str, days: int) -> list[float]:
        if not self.settings.fmp_api_key:
            return []
        url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{ticker}"
        params = {"timeseries": max(days * 2, 180), "apikey": self.settings.fmp_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json().get("historical", [])
            closes = [float(item["close"]) for item in reversed(data) if item.get("close")]
            return closes[-days:]
        except Exception:
            return []

    async def _fetch_yahoo_history(self, ticker: str, days: int) -> list[float]:
        end = int(datetime.now(tz=UTC).timestamp())
        start = int((datetime.now(tz=UTC) - timedelta(days=max(days * 2, 180))).timestamp())
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"period1": start, "period2": end, "interval": "1d", "events": "history"}
        headers = {"User-Agent": "Mozilla/5.0 RiskPulse/1.0", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                results = resp.json().get("chart", {}).get("result", [])
            if not results:
                return []
            closes = results[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
            out = [float(c) for c in closes if c is not None]
            return out[-days:]
        except Exception:
            return []

    async def _fetch_alpha_vantage_history(self, ticker: str, days: int) -> list[float]:
        if not self.settings.alpha_vantage_api_key:
            return []
        params = {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": ticker,
            "outputsize": "compact",
            "apikey": self.settings.alpha_vantage_api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get("https://www.alphavantage.co/query", params=params)
                resp.raise_for_status()
                payload = resp.json()
            if _alpha_vantage_is_limited(payload):
                return []
            series = payload.get("Time Series (Daily)", {})
            if not series:
                return []
            ordered_dates = sorted(series.keys())
            closes = [float(series[d]["4. close"]) for d in ordered_dates if series[d].get("4. close")]
            return closes[-days:]
        except Exception:
            return []


def _alpha_vantage_is_limited(payload: dict) -> bool:
    return bool(payload.get("Note") or payload.get("Information") or payload.get("Error Message"))
