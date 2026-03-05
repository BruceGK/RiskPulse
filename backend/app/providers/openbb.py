from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.providers.cache import TTLCache


_INTEL_CACHE = TTLCache[dict[str, Any]](max_size=4000)


class OpenBBProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openbb_base_url)

    async def get_ticker_intel(self, ticker: str) -> dict[str, Any]:
        symbol = ticker.upper().strip()
        if not symbol:
            return {}
        cache_key = f"intel:{symbol}"
        cached = _INTEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

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
        yahoo_overview = await self._fetch_yahoo_overview(symbol)
        yahoo_quote = await self._fetch_yahoo_quote(symbol)

        row_profile = _pick_row(profile)
        row_metrics = _pick_row(metrics)
        row_ratios = _pick_row(ratios)
        row_analyst = _pick_row(analyst)
        row_shorts = _pick_row(shorts)
        option_rows = _rows(options)
        row_alpha = alpha_overview if isinstance(alpha_overview, dict) else {}
        row_yahoo = yahoo_overview if isinstance(yahoo_overview, dict) else {}
        row_yahoo_quote = yahoo_quote if isinstance(yahoo_quote, dict) else {}

        pe = _coalesce_float(
            _first_value(row_metrics, ("pe_ratio", "pe", "price_earnings_ratio")),
            row_alpha.get("PERatio"),
            row_yahoo.get("PERatio"),
            row_yahoo_quote.get("PERatio"),
        )
        pb = _coalesce_float(
            _first_value(row_metrics, ("pb_ratio", "price_to_book", "p_b")),
            row_alpha.get("PriceToBookRatio"),
            row_yahoo.get("PriceToBookRatio"),
            row_yahoo_quote.get("PriceToBookRatio"),
        )
        ev_ebitda = _coalesce_float(
            _first_value(row_ratios, ("ev_to_ebitda", "enterprise_value_ebitda")),
            row_alpha.get("EVToEBITDA"),
            row_yahoo.get("EVToEBITDA"),
            row_yahoo_quote.get("EVToEBITDA"),
        )
        fcf_yield = _coalesce_float(_first_value(row_ratios, ("fcf_yield", "free_cash_flow_yield")))
        market_cap = _coalesce_float(row_alpha.get("MarketCapitalization"), row_yahoo.get("MarketCapitalization"))
        market_cap = _coalesce_float(market_cap, row_yahoo_quote.get("MarketCapitalization"))
        free_cash_flow_ttm = _coalesce_float(
            row_alpha.get("FreeCashFlowTTM"),
            row_alpha.get("OperatingCashflowTTM"),
            row_yahoo.get("FreeCashFlowTTM"),
            row_yahoo.get("OperatingCashflowTTM"),
        )
        if fcf_yield is None and isinstance(market_cap, float) and market_cap > 0 and isinstance(free_cash_flow_ttm, float):
            fcf_yield = free_cash_flow_ttm / market_cap
        roe = _coalesce_float(
            _first_value(row_ratios, ("roe", "return_on_equity")),
            row_alpha.get("ReturnOnEquityTTM"),
            row_yahoo.get("ReturnOnEquityTTM"),
            row_yahoo_quote.get("ReturnOnEquityTTM"),
        )
        gross_margin = _coalesce_float(_first_value(row_ratios, ("gross_margin", "gross_margin_ratio")))
        gross_profit_ttm = _coalesce_float(row_alpha.get("GrossProfitTTM"), row_yahoo.get("GrossProfitTTM"))
        revenue_ttm = _coalesce_float(row_alpha.get("RevenueTTM"), row_yahoo.get("RevenueTTM"))
        if gross_margin is None and isinstance(gross_profit_ttm, float) and isinstance(revenue_ttm, float) and revenue_ttm > 0:
            gross_margin = gross_profit_ttm / revenue_ttm
        debt_to_equity = _coalesce_float(
            _first_value(row_ratios, ("debt_to_equity", "de_ratio")),
            row_alpha.get("DebtToEquity"),
            row_yahoo.get("DebtToEquity"),
            row_yahoo_quote.get("DebtToEquity"),
        )
        target_price = _coalesce_float(
            _first_value(row_analyst, ("target_price", "price_target")),
            row_alpha.get("AnalystTargetPrice"),
            row_yahoo.get("AnalystTargetPrice"),
            row_yahoo_quote.get("AnalystTargetPrice"),
        )
        recommendation = _coalesce_float(
            _first_value(row_analyst, ("recommendation_mean", "rating")),
            row_yahoo.get("recommendationMean"),
            row_yahoo_quote.get("recommendationMean"),
        )
        short_interest = _coalesce_float(
            _first_value(row_shorts, ("short_interest_percent", "short_percent_float", "short_interest_pct")),
            row_yahoo.get("shortInterestPct"),
            row_yahoo_quote.get("shortInterestPct"),
        )
        eps_ttm = _coalesce_float(
            _first_value(row_metrics, ("eps", "eps_ttm", "earnings_per_share")),
            row_alpha.get("DilutedEPSTTM"),
            row_yahoo.get("DilutedEPSTTM"),
            row_yahoo_quote.get("DilutedEPSTTM"),
        )
        book_value_per_share = _coalesce_float(
            _first_value(row_metrics, ("book_value_per_share", "bvps")),
            row_alpha.get("BookValue"),
            row_yahoo.get("BookValue"),
            row_yahoo_quote.get("BookValue"),
        )
        revenue_growth = _coalesce_float(
            _first_value(row_ratios, ("revenue_growth", "sales_growth", "revenue_growth_yoy")),
            row_alpha.get("QuarterlyRevenueGrowthYOY"),
            row_yahoo.get("QuarterlyRevenueGrowthYOY"),
            row_yahoo_quote.get("QuarterlyRevenueGrowthYOY"),
        )
        earnings_growth = _coalesce_float(
            _first_value(row_ratios, ("earnings_growth", "eps_growth", "net_income_growth")),
            row_alpha.get("QuarterlyEarningsGrowthYOY"),
            row_yahoo.get("QuarterlyEarningsGrowthYOY"),
            row_yahoo_quote.get("QuarterlyEarningsGrowthYOY"),
        )

        option_skew = _options_skew(option_rows)
        put_call_ratio = _options_put_call(option_rows)
        iv_level = _options_iv_level(option_rows)

        valuation_inputs = [pe, pb, ev_ebitda, fcf_yield, target_price, roe, eps_ttm, book_value_per_share, revenue_growth, earnings_growth]
        coverage_count = sum(1 for item in valuation_inputs if isinstance(item, float))
        result = {
            "provider": "openbb",
            "sector": _first_value(row_profile, ("sector", "gics_sector")) or row_yahoo.get("sector") or None,
            "industry": _first_value(row_profile, ("industry", "gics_industry")) or row_yahoo.get("industry") or None,
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
                "yahooOverview": bool(row_yahoo),
                "yahooQuote": bool(row_yahoo_quote),
            },
            "coverage": {"valuationInputs": coverage_count},
        }
        ttl = 1800 if coverage_count >= 2 else 120
        _INTEL_CACHE.set(cache_key, result, ttl_seconds=ttl)
        return result

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

    async def get_macro_calendar(self, limit: int = 20, country: str = "United States") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self.enabled:
            payload = await self._fetch(
                "/api/v1/economy/calendar",
                {
                    "provider": self.settings.openbb_macro_provider or "tradingeconomics",
                    "country": country,
                    "limit": max(5, min(80, limit)),
                },
            )
            rows = [_normalize_macro_event_row(row, source="openbb") for row in _rows(payload)]
            rows = [row for row in rows if row]
        if rows:
            return rows[:limit]
        if self.settings.trading_economics_api_key:
            return await self._fetch_trading_economics_calendar(limit=limit, country=country)
        return []

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

    async def _fetch_trading_economics_calendar(self, limit: int, country: str) -> list[dict[str, Any]]:
        key = self.settings.trading_economics_api_key.strip()
        if not key:
            return []
        params = {"c": key, "f": "json"}
        if country:
            params["country"] = country
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get("https://api.tradingeconomics.com/calendar", params=params)
                resp.raise_for_status()
                payload = resp.json()
            rows = _rows(payload)
            out = [_normalize_macro_event_row(row, source="tradingeconomics") for row in rows]
            return [row for row in out if row][:limit]
        except Exception:
            return []

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

    async def _fetch_yahoo_overview(self, symbol: str) -> dict[str, Any] | None:
        modules = "financialData,defaultKeyStatistics,summaryDetail,assetProfile"
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
        params = {"modules": modules}
        headers = {"User-Agent": "Mozilla/5.0 RiskPulse/1.0", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
            result_rows = payload.get("quoteSummary", {}).get("result")
            if not isinstance(result_rows, list) or not result_rows:
                return None
            row = result_rows[0] if isinstance(result_rows[0], dict) else {}
            if not row:
                return None
            financial_data = row.get("financialData") if isinstance(row.get("financialData"), dict) else {}
            default_keys = row.get("defaultKeyStatistics") if isinstance(row.get("defaultKeyStatistics"), dict) else {}
            summary_detail = row.get("summaryDetail") if isinstance(row.get("summaryDetail"), dict) else {}
            asset_profile = row.get("assetProfile") if isinstance(row.get("assetProfile"), dict) else {}

            debt_to_equity = _yahoo_number(financial_data.get("debtToEquity"))
            if isinstance(debt_to_equity, float) and debt_to_equity > 20:
                debt_to_equity = debt_to_equity / 100.0

            return {
                "AnalystTargetPrice": _yahoo_number(financial_data.get("targetMeanPrice")),
                "PERatio": _coalesce_float(
                    _yahoo_number(summary_detail.get("trailingPE")),
                    _yahoo_number(default_keys.get("trailingPE")),
                ),
                "PriceToBookRatio": _coalesce_float(
                    _yahoo_number(default_keys.get("priceToBook")),
                    _yahoo_number(financial_data.get("priceToBook")),
                ),
                "EVToEBITDA": _yahoo_number(default_keys.get("enterpriseToEbitda")),
                "ReturnOnEquityTTM": _yahoo_number(financial_data.get("returnOnEquity")),
                "DebtToEquity": debt_to_equity,
                "GrossProfitTTM": _yahoo_number(financial_data.get("grossProfits")),
                "RevenueTTM": _coalesce_float(
                    _yahoo_number(financial_data.get("totalRevenue")),
                    _yahoo_number(default_keys.get("totalRevenue")),
                ),
                "OperatingCashflowTTM": _yahoo_number(financial_data.get("operatingCashflow")),
                "FreeCashFlowTTM": _yahoo_number(financial_data.get("freeCashflow")),
                "DilutedEPSTTM": _yahoo_number(default_keys.get("trailingEps")),
                "BookValue": _yahoo_number(default_keys.get("bookValue")),
                "QuarterlyRevenueGrowthYOY": _yahoo_number(financial_data.get("revenueGrowth")),
                "QuarterlyEarningsGrowthYOY": _yahoo_number(financial_data.get("earningsGrowth")),
                "MarketCapitalization": _coalesce_float(
                    _yahoo_number(summary_detail.get("marketCap")),
                    _yahoo_number(default_keys.get("marketCap")),
                ),
                "recommendationMean": _yahoo_number(financial_data.get("recommendationMean")),
                "shortInterestPct": _yahoo_number(default_keys.get("shortPercentOfFloat")),
                "sector": asset_profile.get("sector"),
                "industry": asset_profile.get("industry"),
            }
        except Exception:
            return None

    async def _fetch_yahoo_quote(self, symbol: str) -> dict[str, Any] | None:
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        params = {"symbols": symbol}
        headers = {"User-Agent": "Mozilla/5.0 RiskPulse/1.0", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
            rows = payload.get("quoteResponse", {}).get("result")
            if not isinstance(rows, list) or not rows:
                return None
            row = rows[0] if isinstance(rows[0], dict) else {}
            if not row:
                return None
            market_cap = _float_or_none(row.get("marketCap"))
            total_revenue = _float_or_none(row.get("totalRevenue"))
            return_on_equity = _float_or_none(row.get("returnOnEquity"))
            debt_to_equity = _float_or_none(row.get("debtToEquity"))
            if isinstance(debt_to_equity, float) and debt_to_equity > 20:
                debt_to_equity = debt_to_equity / 100.0
            return {
                "AnalystTargetPrice": _float_or_none(row.get("targetMeanPrice")),
                "PERatio": _float_or_none(row.get("trailingPE")),
                "PriceToBookRatio": _float_or_none(row.get("priceToBook")),
                "EVToEBITDA": _float_or_none(row.get("enterpriseToEbitda")),
                "ReturnOnEquityTTM": return_on_equity,
                "DebtToEquity": debt_to_equity,
                "RevenueTTM": total_revenue,
                "OperatingCashflowTTM": _float_or_none(row.get("operatingCashflow")),
                "FreeCashFlowTTM": _float_or_none(row.get("freeCashflow")),
                "DilutedEPSTTM": _float_or_none(row.get("epsTrailingTwelveMonths")),
                "BookValue": _float_or_none(row.get("bookValue")),
                "QuarterlyRevenueGrowthYOY": _float_or_none(row.get("revenueGrowth")),
                "QuarterlyEarningsGrowthYOY": _float_or_none(row.get("earningsGrowth")),
                "MarketCapitalization": market_cap,
                "recommendationMean": _floatish(row.get("averageAnalystRating")),
                "shortInterestPct": _float_or_none(row.get("shortPercentOfFloat")),
            }
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


def _yahoo_number(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("raw", "longFmt", "fmt"):
            if key in value:
                parsed = _float_or_none(value.get(key))
                if parsed is not None:
                    return parsed
        return None
    return _float_or_none(value)


def _floatish(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str):
        import re

        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return _float_or_none(match.group(0))
    return None


def _parse_importance(value: Any) -> int:
    if isinstance(value, (int, float)):
        n = int(value)
        return max(1, min(3, n))
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return 2
        if text in {"high", "3", "★★★", "***"}:
            return 3
        if text in {"medium", "med", "2", "★★", "**"}:
            return 2
        if text in {"low", "1", "★", "*"}:
            return 1
        parsed = _float_or_none(text)
        if parsed is not None:
            return max(1, min(3, int(parsed)))
    return 2


def _normalize_macro_event_row(row: dict[str, Any], source: str) -> dict[str, Any]:
    event = str(
        _first_value(
            row,
            (
                "event",
                "name",
                "category",
                "title",
                "indicator",
                "Calendar",
                "Category",
                "Event",
            ),
        )
        or ""
    ).strip()
    if not event:
        return {}
    country = str(_first_value(row, ("country", "Country", "region", "Region")) or "").strip() or "Unknown"
    date_value = _first_value(row, ("date", "Date", "release_date", "releaseDate", "datetime", "DateUtc", "time"))
    date_text = str(date_value).strip() if date_value not in (None, "") else None
    actual_raw = _first_value(row, ("actual", "Actual", "actual_value", "value"))
    forecast_raw = _first_value(row, ("forecast", "Forecast", "consensus", "expected"))
    previous_raw = _first_value(row, ("previous", "Previous", "prior"))
    return {
        "event": event,
        "country": country,
        "date": date_text,
        "actual": _floatish(actual_raw),
        "forecast": _floatish(forecast_raw),
        "previous": _floatish(previous_raw),
        "actualText": str(actual_raw).strip() if actual_raw not in (None, "") else None,
        "forecastText": str(forecast_raw).strip() if forecast_raw not in (None, "") else None,
        "previousText": str(previous_raw).strip() if previous_raw not in (None, "") else None,
        "importance": _parse_importance(_first_value(row, ("importance", "Importance", "importance_rating", "importanceLabel"))),
        "source": source,
    }


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
