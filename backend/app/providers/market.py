from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.config import Settings
from app.providers.cache import TTLCache
from app.providers.types import Quote

_QUOTE_CACHE = TTLCache[Quote](max_size=8000)
_HISTORY_CACHE = TTLCache[list[float]](max_size=6000)
_MISS_CACHE = TTLCache[bool](max_size=12000)
_TECH_CACHE = TTLCache[dict[str, Any]](max_size=5000)
_logger = logging.getLogger("riskpulse.providers.market")


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
        pending = await self._merge_quotes(out, pending, self._fetch_openbb_quotes)
        # Preserve Alpha Vantage quota for news fallback on free-tier setups.
        pending = await self._merge_quotes(out, pending, self._fetch_yahoo_quotes)
        pending = await self._merge_quotes(out, pending, self._fetch_yahoo_chart_quotes)
        pending = await self._merge_quotes(out, pending, self._fetch_alpha_vantage_quotes)

        for ticker in pending:
            _MISS_CACHE.set(f"quote:{ticker}", True, ttl_seconds=self.settings.quote_miss_cache_ttl_seconds)
        return out

    async def get_history(self, ticker: str, days: int) -> list[float]:
        key = f"hist:{ticker}:{days}"
        cached = _HISTORY_CACHE.get(key)
        if cached is not None:
            return cached
        if _MISS_CACHE.get(key):
            return []

        fetchers = (
            ("polygon", self._fetch_polygon_history),
            ("fmp", self._fetch_fmp_history),
            ("openbb", self._fetch_openbb_history),
            ("yahoo", self._fetch_yahoo_history),
            ("alpha_vantage", self._fetch_alpha_vantage_history),
        )
        for source, fetcher in fetchers:
            history = await fetcher(ticker, days)
            if len(history) >= 2:
                _logger.info("market.history source=%s ticker=%s points=%d", source, ticker, len(history))
                _HISTORY_CACHE.set(key, history, ttl_seconds=self.settings.history_cache_ttl_seconds)
                return history

        _logger.warning("market.history_miss ticker=%s", ticker)
        _MISS_CACHE.set(key, True, ttl_seconds=self.settings.history_miss_cache_ttl_seconds)
        return []

    async def get_technical_snapshot(
        self,
        ticker: str,
        prices: list[float] | None = None,
        enrich_remote: bool = True,
    ) -> dict[str, Any]:
        symbol = ticker.upper().strip()
        if not symbol:
            return {}

        cache_key = f"tech:{symbol}:{len(prices) if prices else self.settings.history_days}:{int(enrich_remote)}"
        cached = _TECH_CACHE.get(cache_key)
        if cached is not None:
            return cached

        series = prices if prices else await self.get_history(symbol, self.settings.history_days)
        local = _local_technical_snapshot(series)
        source_map = {"local": True, "alphaVantage": False}

        remote_calls = (
            self.settings.alpha_vantage_technical_calls_per_ticker
            if enrich_remote and self.settings.alpha_vantage_api_key
            else 0
        )
        if remote_calls > 0:
            remote = await self._fetch_alpha_vantage_technical_snapshot(symbol, max(1, remote_calls))
            if remote:
                source_map["alphaVantage"] = True
                local.update({k: v for k, v in remote.items() if v is not None})

        merged = _finalize_technical_snapshot(local, source_map)
        ttl = self.settings.technical_cache_ttl_seconds if source_map["alphaVantage"] else min(
            self.settings.technical_cache_ttl_seconds,
            self.settings.history_cache_ttl_seconds,
        )
        _TECH_CACHE.set(cache_key, merged, ttl_seconds=ttl)
        return merged

    async def _merge_quotes(self, out: dict[str, Quote], pending: list[str], fetcher) -> list[str]:
        if not pending:
            return pending
        fetched = await fetcher(pending)
        if fetched:
            sources = {quote.source for quote in fetched.values()}
            _logger.info(
                "market.quotes source=%s served=%d pending_before=%d",
                ",".join(sorted(sources)) or "unknown",
                len(fetched),
                len(pending),
            )
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

    async def _fetch_openbb_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        if not self.settings.openbb_base_url:
            return {}
        url = f"{self.settings.openbb_base_url.rstrip('/')}/api/v1/equity/price/quote"
        params = {"symbol": ",".join(tickers), "provider": self.settings.openbb_provider}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
            out: dict[str, Quote] = {}
            for row in _openbb_rows(payload):
                symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
                price = _safe_float(row.get("last_price") or row.get("price") or row.get("close"))
                prev = _safe_float(row.get("prev_close") or row.get("previous_close") or row.get("close_prev"))
                if symbol and price:
                    out[symbol] = Quote(ticker=symbol, price=price, prev_close=prev, source="openbb")
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

    async def _fetch_yahoo_chart_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        headers = {"User-Agent": "Mozilla/5.0 RiskPulse/1.0", "Accept": "application/json"}
        out: dict[str, Quote] = {}
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            for ticker in tickers:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                params = {"range": "5d", "interval": "1d", "includePrePost": "false", "events": "history"}
                try:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                    result = (resp.json().get("chart", {}).get("result") or [{}])[0]
                    meta = result.get("meta", {}) or {}
                    price = meta.get("regularMarketPrice")
                    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
                    if not price:
                        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
                        valid = [float(c) for c in closes if c is not None]
                        if valid:
                            price = valid[-1]
                            prev = valid[-2] if len(valid) > 1 else prev
                    if price:
                        out[ticker] = Quote(
                            ticker=ticker,
                            price=float(price),
                            prev_close=float(prev) if prev else None,
                            source="yahoo",
                        )
                except Exception:
                    continue
        return out

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

    async def _fetch_openbb_history(self, ticker: str, days: int) -> list[float]:
        if not self.settings.openbb_base_url:
            return []
        end = date.today()
        start = end - timedelta(days=max(days * 2, 180))
        url = f"{self.settings.openbb_base_url.rstrip('/')}/api/v1/equity/price/historical"
        params = {
            "symbol": ticker,
            "provider": self.settings.openbb_provider,
            "interval": "1d",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
            rows = _openbb_rows(payload)
            closes = [_safe_float(row.get("close") or row.get("adj_close") or row.get("last_price")) for row in rows]
            out = [value for value in closes if value is not None]
            return out[-days:]
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

    async def _fetch_alpha_vantage_technical_snapshot(self, ticker: str, max_calls: int) -> dict[str, float]:
        if not self.settings.alpha_vantage_api_key or max_calls <= 0:
            return {}

        out: dict[str, float] = {}
        plan: list[tuple[str, dict[str, str], tuple[str, ...]]] = [
            ("RSI", {"interval": "daily", "time_period": "14", "series_type": "close"}, ("RSI",)),
            ("MACD", {"interval": "daily", "series_type": "close"}, ("MACD", "MACD_Signal", "MACD_Hist")),
            ("ADX", {"interval": "daily", "time_period": "14"}, ("ADX",)),
            ("STOCH", {"interval": "daily", "fastkperiod": "14", "slowkperiod": "3", "slowdperiod": "3"}, ("SlowK", "SlowD")),
            ("BBANDS", {"interval": "daily", "time_period": "20", "series_type": "close", "nbdevup": "2", "nbdevdn": "2"}, ("Real Upper Band", "Real Lower Band", "Real Middle Band")),
        ]
        for func, params, fields in plan[:max_calls]:
            row = await self._fetch_alpha_vantage_indicator_latest(ticker, func, params)
            if not row:
                continue
            for field in fields:
                value = _safe_float(row.get(field))
                if value is None:
                    continue
                if field == "MACD_Hist":
                    out["macdHist"] = value
                elif field == "MACD":
                    out["macdLine"] = value
                elif field == "MACD_Signal":
                    out["macdSignal"] = value
                elif field == "RSI":
                    out["rsi14"] = value
                elif field == "ADX":
                    out["adx14"] = value
                elif field == "SlowK":
                    out["stochK"] = value
                elif field == "SlowD":
                    out["stochD"] = value
                elif field == "Real Upper Band":
                    out["bbUpper"] = value
                elif field == "Real Lower Band":
                    out["bbLower"] = value
                elif field == "Real Middle Band":
                    out["bbMid"] = value
        return out

    async def _fetch_alpha_vantage_indicator_latest(
        self,
        ticker: str,
        function: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        query = {"function": function, "symbol": ticker, "apikey": self.settings.alpha_vantage_api_key}
        query.update(params)
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get("https://www.alphavantage.co/query", params=query)
                resp.raise_for_status()
                payload = resp.json()
            if _alpha_vantage_is_limited(payload):
                return {}
            return _alpha_vantage_latest_row(payload, function)
        except Exception:
            return {}


def _alpha_vantage_is_limited(payload: dict) -> bool:
    return bool(payload.get("Note") or payload.get("Information") or payload.get("Error Message"))


def _openbb_rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("results"), list):
        return [row for row in payload["results"] if isinstance(row, dict)]
    if isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    if isinstance(payload.get("items"), list):
        return [row for row in payload["items"] if isinstance(row, dict)]
    return []


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _alpha_vantage_latest_row(payload: dict[str, Any], function: str) -> dict[str, Any]:
    target_prefix = f"Technical Analysis: {function.upper()}"
    series = None
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        if key.lower().startswith("technical analysis"):
            if key.upper() == target_prefix:
                series = value
                break
            if series is None:
                series = value
    if not isinstance(series, dict):
        return {}
    # Alpha Vantage returns latest date first for these endpoints.
    for key in sorted(series.keys(), reverse=True):
        row = series.get(key)
        if isinstance(row, dict):
            return row
    return {}


def _local_technical_snapshot(prices: list[float]) -> dict[str, float]:
    if len(prices) < 20:
        return {}

    returns = _returns(prices)
    latest = prices[-1]
    sma20 = _sma(prices, 20)
    sma50 = _sma(prices, 50)
    ema20 = _ema(prices, 20)
    ema50 = _ema(prices, 50)
    rsi14 = _rsi(prices, 14)
    macd_line, macd_signal, macd_hist = _macd(prices, 12, 26, 9)
    stoch_k, stoch_d = _stoch(prices, 14, 3)
    bb_upper, bb_mid, bb_lower, bb_pos = _bbands(prices, 20, 2.0)
    atrp14 = _atr_proxy(returns, latest, 14)
    adx14 = _adx_proxy(returns, 14)
    mom5 = _momentum(prices, 5)
    mom20 = _momentum(prices, 20)
    roc20 = _roc(prices, 20)
    cci20 = _cci_like(prices, 20)
    obv_slope = _obv_proxy_slope(returns, 20)

    out: dict[str, float] = {}
    for k, v in (
        ("sma20", sma20),
        ("sma50", sma50),
        ("ema20", ema20),
        ("ema50", ema50),
        ("rsi14", rsi14),
        ("macdLine", macd_line),
        ("macdSignal", macd_signal),
        ("macdHist", macd_hist),
        ("stochK", stoch_k),
        ("stochD", stoch_d),
        ("bbUpper", bb_upper),
        ("bbMid", bb_mid),
        ("bbLower", bb_lower),
        ("bbPos", bb_pos),
        ("atrp14", atrp14),
        ("adx14", adx14),
        ("mom5", mom5),
        ("mom20", mom20),
        ("roc20", roc20),
        ("cci20", cci20),
        ("obvSlope20", obv_slope),
    ):
        if v is not None:
            out[k] = float(v)
    return out


def _finalize_technical_snapshot(raw: dict[str, Any], source_map: dict[str, bool]) -> dict[str, Any]:
    rsi = _safe_float(raw.get("rsi14"))
    stoch_k = _safe_float(raw.get("stochK"))
    macd_hist = _safe_float(raw.get("macdHist"))
    adx = _safe_float(raw.get("adx14"))
    bb_pos = _safe_float(raw.get("bbPos"))
    ema20 = _safe_float(raw.get("ema20"))
    ema50 = _safe_float(raw.get("ema50"))
    mom20 = _safe_float(raw.get("mom20"))
    obv_slope = _safe_float(raw.get("obvSlope20"))

    oversold = 0.0
    overbought = 0.0
    if rsi is not None:
        oversold += max(0.0, min(1.0, (35.0 - rsi) / 15.0)) * 0.45
        overbought += max(0.0, min(1.0, (rsi - 65.0) / 15.0)) * 0.45
    if stoch_k is not None:
        oversold += max(0.0, min(1.0, (25.0 - stoch_k) / 25.0)) * 0.25
        overbought += max(0.0, min(1.0, (stoch_k - 75.0) / 25.0)) * 0.25
    if bb_pos is not None:
        oversold += max(0.0, min(1.0, (0.22 - bb_pos) / 0.22)) * 0.3
        overbought += max(0.0, min(1.0, (bb_pos - 0.78) / 0.22)) * 0.3

    trend = 0.5
    trend_components = 0
    if ema20 is not None and ema50 is not None and ema50 > 0:
        trend += max(-0.25, min(0.25, ((ema20 / ema50) - 1.0) * 6.5))
        trend_components += 1
    if macd_hist is not None:
        trend += max(-0.15, min(0.15, macd_hist * 2.6))
        trend_components += 1
    if mom20 is not None:
        trend += max(-0.16, min(0.16, mom20 * 2.0))
        trend_components += 1
    if obv_slope is not None:
        trend += max(-0.08, min(0.08, obv_slope * 8.0))
        trend_components += 1
    trend = max(0.0, min(1.0, trend))

    trend_strength = 0.35
    if adx is not None:
        trend_strength += max(0.0, min(0.45, (adx - 18.0) / 35.0))
    if trend_components >= 3:
        trend_strength += 0.08
    trend_strength = max(0.0, min(1.0, trend_strength))

    reversal = max(oversold, overbought)
    state = "neutral"
    if overbought >= 0.62 and trend >= 0.58:
        state = "overbought-uptrend"
    elif oversold >= 0.62 and trend <= 0.5:
        state = "oversold-downtrend"
    elif trend >= 0.62 and overbought < 0.62:
        state = "bull-trend"
    elif trend <= 0.38 and oversold < 0.62:
        state = "bear-trend"

    score = max(0.0, min(1.0, 0.5 + ((trend - 0.5) * 0.52) + ((oversold - overbought) * 0.3)))
    snapshot: dict[str, Any] = {
        "signalState": state,
        "trendScore": round(trend, 3),
        "trendStrength": round(trend_strength, 3),
        "oversoldScore": round(max(0.0, min(1.0, oversold)), 3),
        "overboughtScore": round(max(0.0, min(1.0, overbought)), 3),
        "reversalScore": round(max(0.0, min(1.0, reversal)), 3),
        "technicalScore": round(score, 3),
        "providerSources": source_map,
    }
    for key, value in raw.items():
        as_float = _safe_float(value)
        if as_float is not None:
            snapshot[key] = round(as_float, 6)
    return snapshot


def _returns(prices: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        cur = prices[i]
        if prev > 0:
            out.append((cur / prev) - 1.0)
    return out


def _sma(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    window = prices[-period:]
    return sum(window) / len(window)


def _ema(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    alpha = 2.0 / (period + 1)
    value = sum(prices[:period]) / period
    for price in prices[period:]:
        value = (price * alpha) + (value * (1 - alpha))
    return value


def _rsi(prices: list[float], period: int) -> float | None:
    if len(prices) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses == 0:
        return 100.0
    rs = gains / max(losses, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(prices: list[float], fast: int, slow: int, signal: int) -> tuple[float | None, float | None, float | None]:
    if len(prices) < slow + signal:
        return None, None, None
    fast_values = _ema_series(prices, fast)
    slow_values = _ema_series(prices, slow)
    if not fast_values or not slow_values:
        return None, None, None
    length = min(len(fast_values), len(slow_values))
    macd_series = [fast_values[-length + i] - slow_values[-length + i] for i in range(length)]
    signal_line = _ema_series(macd_series, signal)
    if not signal_line:
        return None, None, None
    macd_line = macd_series[-1]
    signal_last = signal_line[-1]
    return macd_line, signal_last, macd_line - signal_last


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1)
    start = sum(values[:period]) / period
    out = [start]
    for value in values[period:]:
        out.append((value * alpha) + (out[-1] * (1 - alpha)))
    return out


def _stoch(prices: list[float], lookback: int, smooth: int) -> tuple[float | None, float | None]:
    if len(prices) < lookback + smooth:
        return None, None
    k_values: list[float] = []
    for i in range(lookback, len(prices) + 1):
        window = prices[i - lookback : i]
        low = min(window)
        high = max(window)
        if high <= low:
            k_values.append(50.0)
            continue
        k_values.append(((window[-1] - low) / (high - low)) * 100.0)
    if len(k_values) < smooth:
        return None, None
    d = sum(k_values[-smooth:]) / smooth
    return k_values[-1], d


def _bbands(prices: list[float], period: int, dev: float) -> tuple[float | None, float | None, float | None, float | None]:
    if len(prices) < period:
        return None, None, None, None
    window = prices[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    stdev = math.sqrt(max(variance, 0.0))
    upper = mid + dev * stdev
    lower = mid - dev * stdev
    pos = None
    if upper > lower:
        pos = (window[-1] - lower) / (upper - lower)
    return upper, mid, lower, pos


def _atr_proxy(returns: list[float], latest_price: float, period: int) -> float | None:
    if not returns or latest_price <= 0:
        return None
    tail = returns[-period:]
    if not tail:
        return None
    avg_abs = sum(abs(r) for r in tail) / len(tail)
    return avg_abs * latest_price


def _adx_proxy(returns: list[float], period: int) -> float | None:
    if len(returns) < period:
        return None
    tail = returns[-period:]
    trend = abs(sum(tail) / len(tail))
    noise = sum(abs(x) for x in tail) / len(tail)
    if noise <= 1e-9:
        return 20.0
    ratio = trend / noise
    return max(5.0, min(55.0, 12.0 + ratio * 45.0))


def _momentum(prices: list[float], period: int) -> float | None:
    if len(prices) <= period:
        return None
    return prices[-1] - prices[-(period + 1)]


def _roc(prices: list[float], period: int) -> float | None:
    if len(prices) <= period or prices[-(period + 1)] <= 0:
        return None
    return (prices[-1] / prices[-(period + 1)]) - 1.0


def _cci_like(prices: list[float], period: int) -> float | None:
    if len(prices) < period:
        return None
    window = prices[-period:]
    typical = sum(window) / len(window)
    mean_dev = sum(abs(x - typical) for x in window) / len(window)
    if mean_dev <= 1e-9:
        return 0.0
    return (window[-1] - typical) / (0.015 * mean_dev)


def _obv_proxy_slope(returns: list[float], period: int) -> float | None:
    if len(returns) < period:
        return None
    tail = returns[-period:]
    # No volume feed guaranteed. Use signed move accumulation as a volume-less OBV proxy.
    path = 0.0
    for move in tail:
        path += 1.0 if move > 0 else -1.0 if move < 0 else 0.0
    return path / period
