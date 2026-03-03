from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class OpenBBProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openbb_base_url)

    async def get_ticker_intel(self, ticker: str) -> dict[str, Any]:
        if not self.enabled and not self.settings.alpha_vantage_api_key:
            return {}
        symbol = ticker.upper().strip()
        if not symbol:
            return {}

        if self.enabled:
            profile_task = self._fetch(
                "/api/v1/equity/profile",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            metrics_task = self._fetch(
                "/api/v1/equity/fundamental/metrics",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            ratios_task = self._fetch(
                "/api/v1/equity/fundamental/ratios",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            analyst_task = self._fetch(
                "/api/v1/equity/estimates/consensus",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            options_task = self._fetch(
                "/api/v1/derivatives/options/snapshots",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            shorts_task = self._fetch(
                "/api/v1/equity/shorts/short_interest",
                {"symbol": symbol, "provider": self.settings.openbb_provider},
            )
            profile, metrics, ratios, analyst, options, shorts = await _gather_safely(
                profile_task, metrics_task, ratios_task, analyst_task, options_task, shorts_task
            )
        else:
            profile, metrics, ratios, analyst, options, shorts = None, None, None, None, None, None

        alpha_overview = await self._fetch_alpha_vantage_overview(symbol) if self.settings.alpha_vantage_api_key else None

        row_profile = _pick_row(profile)
        row_metrics = _pick_row(metrics)
        row_ratios = _pick_row(ratios)
        row_analyst = _pick_row(analyst)
        row_shorts = _pick_row(shorts)
        option_rows = _rows(options)
        row_alpha = alpha_overview if isinstance(alpha_overview, dict) else {}

        pe = _coalesce_float(
            _first_value(row_metrics, ("pe_ratio", "pe", "price_earnings_ratio")),
            row_alpha.get("PERatio"),
        )
        pb = _coalesce_float(
            _first_value(row_metrics, ("pb_ratio", "price_to_book", "p_b")),
            row_alpha.get("PriceToBookRatio"),
        )
        ev_ebitda = _coalesce_float(
            _first_value(row_ratios, ("ev_to_ebitda", "enterprise_value_ebitda")),
            row_alpha.get("EVToEBITDA"),
        )
        fcf_yield = _coalesce_float(_first_value(row_ratios, ("fcf_yield", "free_cash_flow_yield")))
        market_cap = _float_or_none(row_alpha.get("MarketCapitalization"))
        free_cash_flow_ttm = _coalesce_float(row_alpha.get("FreeCashFlowTTM"), row_alpha.get("OperatingCashflowTTM"))
        if fcf_yield is None and isinstance(market_cap, float) and market_cap > 0 and isinstance(free_cash_flow_ttm, float):
            fcf_yield = free_cash_flow_ttm / market_cap
        roe = _coalesce_float(
            _first_value(row_ratios, ("roe", "return_on_equity")),
            row_alpha.get("ReturnOnEquityTTM"),
        )
        gross_margin = _coalesce_float(_first_value(row_ratios, ("gross_margin", "gross_margin_ratio")))
        gross_profit_ttm = _float_or_none(row_alpha.get("GrossProfitTTM"))
        revenue_ttm = _float_or_none(row_alpha.get("RevenueTTM"))
        if gross_margin is None and isinstance(gross_profit_ttm, float) and isinstance(revenue_ttm, float) and revenue_ttm > 0:
            gross_margin = gross_profit_ttm / revenue_ttm
        debt_to_equity = _coalesce_float(
            _first_value(row_ratios, ("debt_to_equity", "de_ratio")),
            row_alpha.get("DebtToEquity"),
        )
        target_price = _coalesce_float(
            _first_value(row_analyst, ("target_price", "price_target")),
            row_alpha.get("AnalystTargetPrice"),
        )
        recommendation = _float_or_none(_first_value(row_analyst, ("recommendation_mean", "rating")))
        short_interest = _float_or_none(
            _first_value(row_shorts, ("short_interest_percent", "short_percent_float", "short_interest_pct"))
        )
        eps_ttm = _coalesce_float(
            _first_value(row_metrics, ("eps", "eps_ttm", "earnings_per_share")),
            row_alpha.get("DilutedEPSTTM"),
        )
        book_value_per_share = _coalesce_float(
            _first_value(row_metrics, ("book_value_per_share", "bvps")),
            row_alpha.get("BookValue"),
        )
        revenue_growth = _coalesce_float(
            _first_value(row_ratios, ("revenue_growth", "sales_growth", "revenue_growth_yoy")),
            row_alpha.get("QuarterlyRevenueGrowthYOY"),
        )
        earnings_growth = _coalesce_float(
            _first_value(row_ratios, ("earnings_growth", "eps_growth", "net_income_growth")),
            row_alpha.get("QuarterlyEarningsGrowthYOY"),
        )

        option_skew = _options_skew(option_rows)
        put_call_ratio = _options_put_call(option_rows)
        iv_level = _options_iv_level(option_rows)

        return {
            "provider": "openbb",
            "sector": _first_value(row_profile, ("sector", "gics_sector")) or None,
            "industry": _first_value(row_profile, ("industry", "gics_industry")) or None,
            "valuation": {
                "pe": pe,
                "pb": pb,
                "evEbitda": ev_ebitda,
                "fcfYield": fcf_yield,
            },
            "quality": {
                "roe": roe,
                "grossMargin": gross_margin,
                "debtToEquity": debt_to_equity,
            },
            "analyst": {
                "targetPrice": target_price,
                "recommendationMean": recommendation,
            },
            "fundamental": {
                "epsTtm": eps_ttm,
                "bookValuePerShare": book_value_per_share,
                "revenueGrowth": revenue_growth,
                "earningsGrowth": earnings_growth,
            },
            "options": {
                "putCallRatio": put_call_ratio,
                "ivLevel": iv_level,
                "skew": option_skew,
            },
            "shorts": {
                "shortInterestPct": short_interest,
            },
            "fallbacks": {
                "alphaVantageOverview": bool(row_alpha),
            },
        }

    async def get_ticker_news(self, ticker: str, limit: int) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        payload = await self._fetch(
            "/api/v1/news/company",
            {"symbol": ticker.upper(), "limit": limit, "provider": self.settings.openbb_provider},
        )
        out: list[dict[str, Any]] = []
        for row in _rows(payload)[:limit]:
            title = str(_first_value(row, ("title", "headline", "text")) or "").strip()
            url = str(_first_value(row, ("url", "link")) or "").strip()
            if not title or not url:
                continue
            out.append(
                {
                    "source": str(_first_value(row, ("source", "publisher")) or "OpenBB"),
                    "title": title,
                    "url": url,
                    "published_at": _first_value(row, ("date", "published", "published_at", "datetime")),
                    "sentiment_hint": _first_value(row, ("sentiment", "sentiment_label")),
                }
            )
        return out

    async def get_macro_news(self, limit: int) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        payload = await self._fetch(
            "/api/v1/news/world",
            {"limit": limit, "provider": self.settings.openbb_provider},
        )
        out: list[dict[str, Any]] = []
        for row in _rows(payload)[:limit]:
            title = str(_first_value(row, ("title", "headline", "text")) or "").strip()
            url = str(_first_value(row, ("url", "link")) or "").strip()
            if not title or not url:
                continue
            out.append(
                {
                    "source": str(_first_value(row, ("source", "publisher")) or "OpenBB"),
                    "title": title,
                    "url": url,
                    "published_at": _first_value(row, ("date", "published", "published_at", "datetime")),
                    "sentiment_hint": _first_value(row, ("sentiment", "sentiment_label")),
                }
            )
        return out

    async def _fetch(self, path: str, params: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        base = self.settings.openbb_base_url.rstrip("/")
        url = f"{base}{path}"
        timeout = self.settings.request_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception:
            return None

    async def _fetch_alpha_vantage_overview(self, symbol: str) -> dict[str, Any] | None:
        if not self.settings.alpha_vantage_api_key:
            return None
        params = {"function": "OVERVIEW", "symbol": symbol, "apikey": self.settings.alpha_vantage_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get("https://www.alphavantage.co/query", params=params)
                resp.raise_for_status()
                payload = resp.json()
            if not isinstance(payload, dict):
                return None
            if payload.get("Note") or payload.get("Information") or payload.get("Error Message"):
                return None
            if not payload.get("Symbol"):
                return None
            return payload
        except Exception:
            return None


async def _gather_safely(*coros):
    import asyncio

    results = await asyncio.gather(*coros, return_exceptions=True)
    out = []
    for row in results:
        out.append(None if isinstance(row, Exception) else row)
    return out


def _rows(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return [payload]


def _pick_row(payload: Any) -> dict[str, Any]:
    rows = _rows(payload)
    return rows[0] if rows else {}


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coalesce_float(*values: Any) -> float | None:
    for value in values:
        parsed = _float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _options_put_call(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    puts = 0.0
    calls = 0.0
    for row in rows:
        side = str(_first_value(row, ("option_type", "type", "side")) or "").lower()
        oi = _float_or_none(_first_value(row, ("open_interest", "oi", "openInterest"))) or 0.0
        if "put" in side:
            puts += oi
        elif "call" in side:
            calls += oi
    if calls <= 0:
        return None
    return puts / calls


def _options_iv_level(rows: list[dict[str, Any]]) -> float | None:
    values: list[float] = []
    for row in rows:
        iv = _float_or_none(_first_value(row, ("implied_volatility", "iv", "impliedVolatility")))
        if iv is not None and iv > 0:
            values.append(iv)
    if not values:
        return None
    return sum(values) / len(values)


def _options_skew(rows: list[dict[str, Any]]) -> float | None:
    puts: list[float] = []
    calls: list[float] = []
    for row in rows:
        side = str(_first_value(row, ("option_type", "type", "side")) or "").lower()
        iv = _float_or_none(_first_value(row, ("implied_volatility", "iv", "impliedVolatility")))
        if iv is None:
            continue
        if "put" in side:
            puts.append(iv)
        elif "call" in side:
            calls.append(iv)
    if not puts or not calls:
        return None
    return (sum(puts) / len(puts)) - (sum(calls) / len(calls))
