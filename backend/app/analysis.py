from __future__ import annotations

import asyncio
import math
import re
from datetime import date
from statistics import mean
from typing import Any

from app.config import Settings
from app.models import AnalysisRequest, AnalysisResponse, Headline, MacroPoint, PositionAnalysis
from app.providers.ai import AiProvider
from app.providers.macro import MacroProvider
from app.providers.market import MarketProvider
from app.providers.news import NewsProvider
from app.providers.sec import SecProvider
from app.providers.types import SeriesPoint


class AnalysisService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = MarketProvider(settings)
        self.macro = MacroProvider(settings)
        self.news = NewsProvider(settings)
        self.sec = SecProvider(settings)
        self.ai = AiProvider(settings)

    async def analyze(self, req: AnalysisRequest) -> AnalysisResponse:
        # Preserve user ticker priority so free-tier rate limits don't starve portfolio quotes.
        tickers = list(dict.fromkeys(p.ticker for p in req.positions))
        macro_overlays = [symbol for symbol in ("SPY", "GLD") if symbol not in tickers]
        quote_symbols = tickers + macro_overlays
        quotes_task = asyncio.create_task(self.market.get_quotes(quote_symbols))
        macro_task = asyncio.create_task(self.macro.get_macro_snapshot())
        quotes, macro_raw = await asyncio.gather(quotes_task, macro_task)

        position_rows: list[PositionAnalysis] = []
        missing_quotes: list[str] = []
        quote_sources: dict[str, str] = {}
        portfolio_value = 0.0
        for p in req.positions:
            quote = quotes.get(p.ticker)
            if not quote:
                missing_quotes.append(p.ticker)
                continue
            if quote.source:
                quote_sources[p.ticker] = quote.source
            value = p.qty * quote.price
            portfolio_value += value
            chg_pct_1d = None
            if quote.prev_close and quote.prev_close != 0:
                chg_pct_1d = (quote.price / quote.prev_close) - 1
            position_rows.append(
                PositionAnalysis(
                    ticker=p.ticker,
                    qty=p.qty,
                    price=quote.price,
                    value=value,
                    weight=0.0,
                    chg_pct_1d=chg_pct_1d,
                )
            )

        if portfolio_value > 0:
            for row in position_rows:
                row.weight = row.value / portfolio_value

        position_rows.sort(key=lambda x: x.value, reverse=True)
        top5_weight = sum(row.weight for row in position_rows[:5])
        risk = await self._compute_risk(position_rows)

        macro = self._build_macro_payload(macro_raw, quotes)
        news = await self._build_news_payload(position_rows)
        notes = self._build_notes(top5_weight, risk, missing_quotes, news)
        if missing_quotes and self.settings.alpha_vantage_api_key and not (
            self.settings.polygon_api_key or self.settings.fmp_api_key
        ):
            notes.append("Free Alpha Vantage limits can cause partial quotes. Wait ~60s and refresh analysis.")
        if len(position_rows) > self.settings.max_positions_for_risk:
            notes.append(
                f"Risk metrics computed on top {self.settings.max_positions_for_risk} holdings by market value."
            )
        data_quality = self._build_data_quality(req, position_rows, macro, news)
        deterministic_signals = self._build_signals(position_rows, notes, news, risk, macro)
        ai_signals = await self.ai.build_signals(
            {
                "asOf": date.today().isoformat(),
                "portfolioValue": round(portfolio_value, 2),
                "positions": [p.model_dump() for p in position_rows[:8]],
                "risk": risk,
                "macro": {k: v.model_dump() for k, v in macro.items()},
                "notes": notes[:6],
                "macroNews": [h.model_dump() for h in news.get("macro", [])[:8]],
                "tickerNews": {
                    ticker: [h.model_dump() for h in items[:3]]
                    for ticker, items in news.items()
                    if ticker not in {"macro", "sec"}
                },
                "signalsDraft": deterministic_signals,
            }
        )
        signals = self._merge_signals(deterministic_signals, ai_signals)
        meta_payload: dict = {
            "providers": {
                "polygon_enabled": bool(self.settings.polygon_api_key),
                "fred_enabled": bool(self.settings.fred_api_key),
                "newsapi_enabled": bool(self.settings.newsapi_api_key),
                "fmp_enabled": bool(self.settings.fmp_api_key),
                "alpha_vantage_enabled": bool(self.settings.alpha_vantage_api_key),
                "yahoo_enabled": True,
                "openai_enabled": bool(self.settings.openai_api_key),
            },
            "quoteSources": quote_sources,
            "dataQuality": data_quality,
            "signals": signals,
        }
        pulse = signals.get("pulse")
        if isinstance(pulse, dict):
            meta_payload["intelligence"] = pulse

        return AnalysisResponse(
            as_of=date.today(),
            portfolio_value=round(portfolio_value, 2),
            positions=position_rows,
            top_concentration={"top5Weight": round(top5_weight, 4)},
            risk=risk,
            macro=macro,
            news=news,
            notes=notes,
            meta=meta_payload,
        )

    async def _compute_risk(self, positions: list[PositionAnalysis]) -> dict[str, float | None]:
        if not positions:
            return {"vol60d": None, "vol120d": None, "maxDrawdown120d": None}

        risk_positions = positions[: self.settings.max_positions_for_risk]
        weight_total = sum(p.weight for p in risk_positions)
        if weight_total <= 0:
            return {"vol60d": None, "vol120d": None, "maxDrawdown120d": None}

        tasks = [self.market.get_history(p.ticker, self.settings.history_days) for p in risk_positions]
        histories = await asyncio.gather(*tasks)
        weighted_returns: list[list[float]] = []
        for idx, series in enumerate(histories):
            r = _daily_returns(series)
            if not r:
                continue
            w = risk_positions[idx].weight / weight_total
            weighted_returns.append([x * w for x in r])

        if not weighted_returns:
            return {"vol60d": None, "vol120d": None, "maxDrawdown120d": None}

        min_len = min(len(r) for r in weighted_returns)
        portfolio_returns = [sum(r[i] for r in weighted_returns) for i in range(min_len)]
        vol60 = _annualized_vol(portfolio_returns[-60:]) if len(portfolio_returns) >= 20 else None
        vol120 = _annualized_vol(portfolio_returns[-120:]) if len(portfolio_returns) >= 20 else None

        # Approximate portfolio NAV from daily returns for drawdown estimation.
        nav = [1.0]
        for ret in portfolio_returns[-120:]:
            nav.append(nav[-1] * (1 + ret))
        max_dd = _max_drawdown(nav)

        return {
            "vol60d": round(vol60, 4) if vol60 is not None else None,
            "vol120d": round(vol120, 4) if vol120 is not None else None,
            "maxDrawdown120d": round(max_dd, 4) if max_dd is not None else None,
        }

    def _build_macro_payload(self, macro_raw: dict[str, SeriesPoint], quotes: dict) -> dict[str, MacroPoint]:
        macro: dict[str, MacroPoint] = {}
        for label, point in macro_raw.items():
            if label == "US10Y":
                change_bp = (point.value - point.previous_value) * 100 if point.previous_value is not None else None
                macro[label] = MacroPoint(value=point.value, chg_bp_1d=change_bp, as_of=point.as_of)
            else:
                pct = (point.value / point.previous_value - 1) if point.previous_value else None
                macro[label] = MacroPoint(value=point.value, chg_pct_1d=pct, as_of=point.as_of)

        for equity_macro in ("SPY", "GLD"):
            q = quotes.get(equity_macro)
            if not q:
                continue
            pct = (q.price / q.prev_close - 1) if q.prev_close else None
            macro[equity_macro] = MacroPoint(value=q.price, chg_pct_1d=pct, as_of=date.today().isoformat())

        return macro

    async def _build_news_payload(self, positions: list[PositionAnalysis]) -> dict[str, list[Headline]]:
        result: dict[str, list[Headline]] = {}
        macro_items = await self.news.get_macro_news(limit=min(self.settings.news_limit, 10))
        result["macro"] = [Headline(**item.__dict__) for item in macro_items]

        top_tickers = [p.ticker for p in positions[:2]]
        tasks = [self.news.get_ticker_news(ticker, limit=5) for ticker in top_tickers]
        ticker_news_lists = await asyncio.gather(*tasks) if tasks else []
        for ticker, items in zip(top_tickers, ticker_news_lists, strict=False):
            result[ticker] = [Headline(**item.__dict__) for item in items]

        filing_tasks = [self.sec.get_latest_filing(ticker) for ticker in top_tickers]
        filing_rows = await asyncio.gather(*filing_tasks) if filing_tasks else []
        filing_headlines: list[Headline] = []
        for ticker, filing in zip(top_tickers, filing_rows, strict=False):
            if not filing:
                continue
            filing_headlines.append(
                Headline(
                    source="SEC",
                    title=f"{ticker} filed {filing['form']} on {filing['filing_date']}",
                    url="https://www.sec.gov/edgar/search/",
                    published_at=filing["filing_date"],
                    sentiment_hint="neutral",
                )
            )
        if filing_headlines:
            result["sec"] = filing_headlines

        return result

    def _build_signals(
        self,
        positions: list[PositionAnalysis],
        notes: list[str],
        news: dict[str, list[Headline]],
        risk: dict[str, float | None],
        macro: dict[str, MacroPoint],
    ) -> dict[str, Any]:
        top_tickers = [p.ticker for p in positions[:5]]
        radar = self._build_headline_radar(news, top_tickers)
        watchouts = self._build_watchouts(positions, radar)
        scenarios = self._build_scenarios(positions)
        warnings = self._build_warnings(notes, risk, macro, radar, scenarios)
        pulse = _build_pulse(warnings, scenarios)
        return {
            "pulse": pulse,
            "warnings": warnings,
            "watchouts": watchouts,
            "radar": radar,
            "scenarios": scenarios,
        }

    def _build_headline_radar(self, news: dict[str, list[Headline]], top_tickers: list[str]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        macro_headlines = news.get("macro", [])[:10]
        for item in macro_headlines:
            title_key = item.title.strip().lower()
            if not item.title or title_key in seen:
                continue
            seen.add(title_key)
            impact, direction, horizon = _classify_headline(item.title)
            entries.append(
                {
                    "title": item.title,
                    "source": item.source,
                    "url": item.url,
                    "publishedAt": item.published_at,
                    "impact": impact,
                    "direction": direction,
                    "horizon": horizon,
                    "relatedTickers": _extract_related_tickers(item.title, top_tickers),
                }
            )

        for ticker in top_tickers[:3]:
            for item in news.get(ticker, [])[:3]:
                title_key = item.title.strip().lower()
                if not item.title or title_key in seen:
                    continue
                seen.add(title_key)
                impact, direction, horizon = _classify_headline(item.title, sentiment_hint=item.sentiment_hint)
                entries.append(
                    {
                        "title": item.title,
                        "source": item.source,
                        "url": item.url,
                        "publishedAt": item.published_at,
                        "impact": impact,
                        "direction": direction,
                        "horizon": horizon,
                        "relatedTickers": [ticker],
                    }
                )

        entries.sort(key=lambda row: (_impact_score(row["impact"]), row["direction"] == "risk-up"), reverse=True)
        return entries[:14]

    def _build_watchouts(self, positions: list[PositionAnalysis], radar: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in positions[:10]:
            severity_rank = 0
            reasons: list[str] = []
            if p.weight >= 0.35:
                severity_rank = max(severity_rank, 2)
                reasons.append(f"Concentration is high at {p.weight:.0%}.")
            elif p.weight >= 0.2:
                severity_rank = max(severity_rank, 1)
                reasons.append(f"Portfolio weight is material at {p.weight:.0%}.")

            if p.chg_pct_1d is not None and abs(p.chg_pct_1d) >= 0.03:
                severity_rank = max(severity_rank, 1 if abs(p.chg_pct_1d) < 0.05 else 2)
                reasons.append(f"One-day move is {p.chg_pct_1d:+.2%}.")

            risk_up_hits = sum(
                1
                for row in radar
                if p.ticker in row.get("relatedTickers", [])
                and row.get("direction") == "risk-up"
                and row.get("impact") in {"medium", "high"}
            )
            if risk_up_hits >= 2:
                severity_rank = max(severity_rank, 2)
                reasons.append("Multiple risk-up headlines are linked to this ticker.")
            elif risk_up_hits == 1:
                severity_rank = max(severity_rank, 1)
                reasons.append("Recent headline flow adds short-term uncertainty.")

            severity = _severity_from_rank(severity_rank)
            text = " ".join(reasons[:2]) if reasons else "No acute pressure signals in this run."
            out.append(
                {
                    "ticker": p.ticker,
                    "severity": severity,
                    "text": text,
                }
            )
        return out

    def _build_scenarios(self, positions: list[PositionAnalysis]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for scenario in _SCENARIOS:
            exposures: list[dict[str, Any]] = []
            impact = 0.0
            for p in positions:
                sensitivity = _scenario_sensitivity(p.ticker, scenario["id"])
                contribution = p.weight * sensitivity
                impact += contribution
                exposures.append(
                    {
                        "ticker": p.ticker,
                        "weight": round(p.weight, 4),
                        "sensitivity": round(sensitivity, 3),
                        "contribution": round(contribution, 4),
                    }
                )

            impact_pct = impact * scenario["scalar"]
            exposures.sort(key=lambda row: abs(row["contribution"]), reverse=True)
            out.append(
                {
                    "id": scenario["id"],
                    "name": scenario["name"],
                    "shock": scenario["shock"],
                    "portfolioImpactPct": round(impact_pct, 4),
                    "direction": "risk-up" if impact_pct < 0 else "risk-down",
                    "exposed": exposures[:3],
                }
            )
        return out

    @staticmethod
    def _build_warnings(
        notes: list[str],
        risk: dict[str, float | None],
        macro: dict[str, MacroPoint],
        radar: list[dict[str, Any]],
        scenarios: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for note in notes:
            lower = note.lower()
            if "concentration high" in lower:
                out.append({"title": "Concentration Risk", "severity": "high", "reason": note})
            elif "drawdown elevated" in lower:
                out.append({"title": "Drawdown Pressure", "severity": "high", "reason": note})
            elif "volatility elevated" in lower:
                out.append({"title": "Volatility Regime", "severity": "medium", "reason": note})
            elif "missing market data" in lower:
                out.append({"title": "Data Gaps", "severity": "medium", "reason": note})

        vix = macro.get("VIX")
        if vix and vix.chg_pct_1d is not None and vix.chg_pct_1d >= 0.05:
            out.append(
                {
                    "title": "Volatility Spike",
                    "severity": "high",
                    "reason": f"VIX changed {vix.chg_pct_1d:.1%} in latest snapshot.",
                }
            )

        risk_up_high = sum(1 for row in radar if row["direction"] == "risk-up" and row["impact"] == "high")
        if risk_up_high >= 2:
            out.append(
                {
                    "title": "Headline Shock Cluster",
                    "severity": "high",
                    "reason": f"{risk_up_high} high-impact risk-up headlines are active.",
                }
            )

        if scenarios:
            worst = min(scenarios, key=lambda row: row["portfolioImpactPct"])
            if worst["portfolioImpactPct"] <= -0.007:
                out.append(
                    {
                        "title": f"Scenario Stress: {worst['name']}",
                        "severity": "medium",
                        "reason": f"Estimated portfolio sensitivity is {worst['portfolioImpactPct']:.2%} under this shock.",
                    }
                )

        if not out and risk.get("vol120d") is not None:
            out.append({"title": "No Immediate Alerts", "severity": "low", "reason": "Current signals are not flashing stress."})

        deduped: list[dict[str, str]] = []
        seen_titles: set[str] = set()
        for row in out:
            title_key = row["title"].lower()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            deduped.append(row)
        return deduped[:6]

    @staticmethod
    def _merge_signals(base: dict[str, Any], ai: dict[str, Any] | None) -> dict[str, Any]:
        if not ai:
            return base

        merged = {
            "pulse": base.get("pulse", {}),
            "warnings": list(base.get("warnings", [])),
            "watchouts": list(base.get("watchouts", [])),
            "radar": list(base.get("radar", [])),
            "scenarios": list(base.get("scenarios", [])),
        }

        ai_pulse = ai.get("pulse")
        if isinstance(ai_pulse, dict):
            merged["pulse"].update({k: v for k, v in ai_pulse.items() if v})

        ai_warnings = ai.get("warnings")
        if isinstance(ai_warnings, list):
            existing = {row["title"].lower() for row in merged["warnings"] if isinstance(row, dict) and "title" in row}
            for row in ai_warnings:
                if not isinstance(row, dict):
                    continue
                title = row.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                if title.lower() in existing:
                    continue
                existing.add(title.lower())
                merged["warnings"].append(
                    {
                        "title": title.strip(),
                        "severity": row.get("severity") if row.get("severity") in {"low", "medium", "high"} else "medium",
                        "reason": (row.get("reason") or "").strip(),
                    }
                )

        ai_watchouts = ai.get("watchouts")
        if isinstance(ai_watchouts, list):
            by_ticker: dict[str, dict[str, Any]] = {
                row["ticker"]: row for row in merged["watchouts"] if isinstance(row, dict) and isinstance(row.get("ticker"), str)
            }
            for row in ai_watchouts:
                if not isinstance(row, dict):
                    continue
                ticker = row.get("ticker")
                text = row.get("text")
                if not isinstance(ticker, str) or not ticker.strip() or not isinstance(text, str) or not text.strip():
                    continue
                ticker = ticker.strip().upper()
                severity = row.get("severity") if row.get("severity") in {"low", "medium", "high"} else "medium"
                by_ticker[ticker] = {"ticker": ticker, "severity": severity, "text": text.strip()}
            merged["watchouts"] = list(by_ticker.values())

        ai_radar = ai.get("radar")
        if isinstance(ai_radar, list):
            by_title: dict[str, dict[str, Any]] = {
                row["title"].strip().lower(): row for row in merged["radar"] if isinstance(row, dict) and isinstance(row.get("title"), str)
            }
            for row in ai_radar:
                if not isinstance(row, dict):
                    continue
                title = row.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                key = title.strip().lower()
                if key not in by_title:
                    continue
                target = by_title[key]
                if row.get("impact") in {"low", "medium", "high"}:
                    target["impact"] = row["impact"]
                if row.get("direction") in {"risk-up", "risk-down", "neutral"}:
                    target["direction"] = row["direction"]
                if row.get("horizon") in {"intraday", "1w", "1m"}:
                    target["horizon"] = row["horizon"]
                related = row.get("relatedTickers")
                if isinstance(related, list):
                    clean = [t.strip().upper() for t in related if isinstance(t, str) and t.strip()][:3]
                    if clean:
                        target["relatedTickers"] = clean

        merged["warnings"] = merged["warnings"][:6]
        merged["watchouts"] = merged["watchouts"][:10]
        merged["radar"] = merged["radar"][:14]
        return merged

    @staticmethod
    def _build_notes(
        top5_weight: float,
        risk: dict[str, float | None],
        missing_quotes: list[str],
        news: dict[str, list[Headline]],
    ) -> list[str]:
        notes: list[str] = []
        if top5_weight >= 0.7:
            notes.append(f"Concentration high: top5 {top5_weight:.0%}")
        vol120 = risk.get("vol120d")
        if vol120 is not None and vol120 > 0.3:
            notes.append(f"Volatility elevated: 120d annualized vol {vol120:.0%}")
        drawdown = risk.get("maxDrawdown120d")
        if drawdown is not None and drawdown > 0.12:
            notes.append(f"Drawdown elevated: 120d max drawdown {drawdown:.0%}")
        if missing_quotes:
            ticker_list = ", ".join(sorted(set(missing_quotes)))
            notes.append(f"Missing market data for: {ticker_list}")
        if not news.get("macro"):
            notes.append("Macro headlines unavailable from configured providers or request limits.")
        if not notes:
            notes.append("No immediate concentration or volatility alerts.")
        return notes

    @staticmethod
    def _build_data_quality(
        req: AnalysisRequest,
        positions: list[PositionAnalysis],
        macro: dict[str, MacroPoint],
        news: dict[str, list[Headline]],
    ) -> dict[str, float | str]:
        total_positions = len(req.positions)
        priced_positions = len(positions)
        price_coverage = (priced_positions / total_positions) if total_positions else 0.0
        macro_coverage = len(macro) / 5.0
        macro_news_count = len(news.get("macro", []))
        score = max(0.0, min(1.0, (price_coverage * 0.6) + (macro_coverage * 0.25) + (0.15 if macro_news_count else 0.0)))
        if score >= 0.85:
            label = "high"
        elif score >= 0.6:
            label = "medium"
        else:
            label = "low"
        return {
            "score": round(score, 3),
            "label": label,
            "priceCoverage": round(price_coverage, 3),
            "macroCoverage": round(macro_coverage, 3),
            "macroNewsCount": float(macro_news_count),
        }


_SCENARIOS = [
    {"id": "rates_up_50bp", "name": "Rates +50bp", "shock": "Treasury yields +50bp", "scalar": 0.007},
    {"id": "vix_up_20", "name": "VIX +20%", "shock": "Volatility shock", "scalar": 0.012},
    {"id": "usd_up_2", "name": "USD +2%", "shock": "Dollar squeeze", "scalar": 0.005},
]

_TECH_TICKERS = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "QQQ"}
_FINANCIAL_TICKERS = {"JPM", "BAC", "GS", "MS", "WFC", "XLF", "SCHW"}
_BOND_TICKERS = {"TLT", "IEF", "AGG", "BND"}
_GOLD_TICKERS = {"GLD", "IAU", "GDX"}
_DEFENSIVE_TICKERS = {"XLP", "XLU", "VPU", "KO", "PG"}

_HIGH_IMPACT_KEYWORDS = (
    "war",
    "attack",
    "invasion",
    "sanction",
    "recession",
    "crisis",
    "default",
    "inflation",
    "fed",
    "rate hike",
    "yield spike",
    "plunge",
    "surge",
)
_RISK_UP_KEYWORDS = (
    "selloff",
    "drop",
    "decline",
    "inflation",
    "tightening",
    "hike",
    "shock",
    "volatility",
    "geopolitical",
    "conflict",
    "war",
)
_RISK_DOWN_KEYWORDS = (
    "cooling",
    "disinflation",
    "pause",
    "cut",
    "rally",
    "rebound",
    "stabilize",
)
_INTRADAY_KEYWORDS = ("today", "now", "overnight")
_WEEK_KEYWORDS = ("this week", "week", "next week", "days")


def _build_pulse(warnings: list[dict[str, str]], scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    worst_impact = min((s["portfolioImpactPct"] for s in scenarios), default=0.0)
    high_alerts = sum(1 for w in warnings if w.get("severity") == "high")
    if high_alerts >= 1 or worst_impact <= -0.008:
        stance = "risk-off"
    elif worst_impact >= 0.004 and high_alerts == 0:
        stance = "risk-on"
    else:
        stance = "balanced"

    if scenarios:
        worst = min(scenarios, key=lambda row: row["portfolioImpactPct"])
        thesis = f"Largest modeled stress is {worst['name']} ({worst['portfolioImpactPct']:.2%}); prioritize exposure control."
    else:
        thesis = "Risk posture is balanced with no dominant scenario stress."

    focus = [w["title"] for w in warnings if isinstance(w.get("title"), str)][:3]
    return {"thesis": thesis, "stance": stance, "focus": focus}


def _severity_from_rank(rank: int) -> str:
    if rank >= 2:
        return "high"
    if rank == 1:
        return "medium"
    return "low"


def _impact_score(impact: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}.get(impact, 1)


def _classify_headline(title: str, sentiment_hint: str | None = None) -> tuple[str, str, str]:
    lower = title.lower()
    impact = "high" if any(k in lower for k in _HIGH_IMPACT_KEYWORDS) else "medium"
    if not any(k in lower for k in _HIGH_IMPACT_KEYWORDS) and len(title) < 85:
        impact = "low"

    direction = "neutral"
    risk_up_hits = sum(1 for k in _RISK_UP_KEYWORDS if k in lower)
    risk_down_hits = sum(1 for k in _RISK_DOWN_KEYWORDS if k in lower)
    if risk_up_hits > risk_down_hits:
        direction = "risk-up"
    elif risk_down_hits > risk_up_hits:
        direction = "risk-down"
    elif sentiment_hint:
        hint = sentiment_hint.lower()
        if "bear" in hint or "negative" in hint:
            direction = "risk-up"
        elif "bull" in hint or "positive" in hint:
            direction = "risk-down"

    if any(k in lower for k in _INTRADAY_KEYWORDS):
        horizon = "intraday"
    elif any(k in lower for k in _WEEK_KEYWORDS):
        horizon = "1w"
    else:
        horizon = "1m"

    return impact, direction, horizon


def _extract_related_tickers(title: str, top_tickers: list[str]) -> list[str]:
    related: list[str] = []
    upper_title = title.upper()
    for ticker in top_tickers:
        if re.search(rf"\b{re.escape(ticker)}\b", upper_title):
            related.append(ticker)
    if related:
        return related[:3]
    return top_tickers[:2]


def _scenario_sensitivity(ticker: str, scenario_id: str) -> float:
    symbol = ticker.upper()
    if scenario_id == "rates_up_50bp":
        if symbol in _TECH_TICKERS:
            return -0.65
        if symbol in _FINANCIAL_TICKERS:
            return 0.3
        if symbol in _BOND_TICKERS:
            return -0.9
        if symbol in _GOLD_TICKERS:
            return -0.2
        return -0.25
    if scenario_id == "vix_up_20":
        if symbol in _TECH_TICKERS:
            return -0.75
        if symbol in _DEFENSIVE_TICKERS:
            return -0.2
        if symbol in _BOND_TICKERS:
            return 0.2
        if symbol in _GOLD_TICKERS:
            return 0.45
        return -0.5
    if scenario_id == "usd_up_2":
        if symbol in _TECH_TICKERS:
            return -0.35
        if symbol in _FINANCIAL_TICKERS:
            return 0.1
        if symbol in _GOLD_TICKERS:
            return -0.4
        return -0.2
    return 0.0


def _daily_returns(prices: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(prices)):
        prev = prices[i - 1]
        cur = prices[i]
        if prev <= 0:
            continue
        out.append((cur / prev) - 1)
    return out


def _annualized_vol(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    avg = mean(returns)
    variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance * 252)


def _max_drawdown(nav_series: list[float]) -> float | None:
    if len(nav_series) < 2:
        return None
    peak = nav_series[0]
    max_dd = 0.0
    for value in nav_series:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            max_dd = max(max_dd, dd)
    return max_dd
