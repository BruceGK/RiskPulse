from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from statistics import mean
from typing import Any

from app.config import Settings
from app.models import (
    AnalysisRequest,
    AnalysisResponse,
    Headline,
    MacroPoint,
    PositionAnalysis,
    ValuationPoint,
    ValuationResponse,
)
from app.providers.ai import AiProvider
from app.providers.macro import MacroProvider
from app.providers.market import MarketProvider
from app.providers.news import NewsProvider
from app.providers.openbb import OpenBBProvider
from app.providers.sec import SecProvider
from app.providers.types import SeriesPoint


class AnalysisService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = MarketProvider(settings)
        self.macro = MacroProvider(settings)
        self.news = NewsProvider(settings)
        self.openbb = OpenBBProvider(settings)
        self.sec = SecProvider(settings)
        self.ai = AiProvider(settings)

    def _provider_status(self, quote_sources: dict[str, str] | None = None) -> dict[str, bool]:
        """Truthful per-request provider status.

        Green = the provider actually contributed to this analysis run, OR is a
        passive provider whose configuration alone enables a capability (FRED/OpenAI/NewsAPI).
        Grey  = the provider was either unconfigured OR silently failed.

        Frontend renders one chip per key (after stripping any "_enabled" suffix).
        """
        served = self.openbb.fundamentals_served
        market_sources_used = set((quote_sources or {}).values())
        return {
            # Market quote sources: green only if they actually served a quote.
            "polygon": "polygon" in market_sources_used,
            "fmp": served.get("fmp", 0) > 0 or "fmp" in market_sources_used,
            "openbb": served.get("openbb", 0) > 0 or "openbb" in market_sources_used,
            "yahoo": served.get("yahoo", 0) > 0 or bool(market_sources_used & {"yahoo", "yahoo_chart"}),
            "alpha_vantage": served.get("alpha_vantage", 0) > 0,
            # Passive providers: configuration is the right signal.
            "fred": bool(self.settings.fred_api_key),
            "newsapi": bool(self.settings.newsapi_api_key),
            "tradingeconomics": bool(self.settings.trading_economics_api_key),
            "openai": bool(self.settings.openai_api_key),
        }

    async def analyze(self, req: AnalysisRequest, quick_mode: bool = False) -> AnalysisResponse:
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
        macro = self._build_macro_payload(macro_raw, quotes)
        if quick_mode:
            risk = {"vol60d": None, "vol120d": None, "maxDrawdown120d": None}
            news: dict[str, list[Headline]] = {"macro": [], "sec": []}
            behavioral = {
                "regime": {"state": "quick-pass"},
                "tickerIntel": [],
                "opportunities": [],
                "exitSignals": [],
                "predictions": {},
                "portfolioActions": [],
                "hedgePlan": [],
                "construction": {},
                "alphaBook": {},
                "submodels": {},
                "analystDesk": {},
            }
            notes: list[str] = []
            if top5_weight >= 0.7:
                notes.append(f"Concentration high: top5 {top5_weight:.0%}")
            if missing_quotes:
                ticker_list = ", ".join(sorted(set(missing_quotes)))
                notes.append(f"Missing market data for: {ticker_list}")
            notes.append("Quick pass loaded. Deep analysis, news, and AI signals are still loading.")
            data_quality = self._build_data_quality(req, position_rows, macro, news)
            signals = self._build_signals(position_rows, notes, news, risk, macro, behavioral, macro_events=[])
            meta_payload: dict = {
                "providers": self._provider_status(quote_sources),
                "fundamentalsAttribution": dict(self.openbb.fundamentals_attribution),
                "quoteSources": quote_sources,
                "dataQuality": data_quality,
                "signals": signals,
                "model": {
                    "name": "riskpulse-quick-pass",
                    "type": "incremental-load",
                    "components": ["quotes", "weights", "macro-snapshot"],
                    "openbbEnriched": self.openbb.enabled,
                },
                "progress": {"phase": "quick", "fullPending": True},
            }
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

        risk_task = asyncio.create_task(self._compute_risk(position_rows))
        news_task = asyncio.create_task(self._build_news_payload(position_rows))
        macro_events_task = asyncio.create_task(self.openbb.get_macro_calendar(limit=20, country="United States"))
        risk, news, macro_events = await asyncio.gather(risk_task, news_task, macro_events_task)
        if not isinstance(macro_events, list):
            macro_events = []
        try:
            behavioral = await self._build_behavioral_intel(position_rows, news, macro)
        except Exception:
            behavioral = {
                "regime": {"state": "insufficient-data"},
                "tickerIntel": [],
                "opportunities": [],
                "exitSignals": [],
                "predictions": {},
                "portfolioActions": [],
                "hedgePlan": [],
                "construction": {},
                "alphaBook": {},
                "submodels": {},
                "analystDesk": {},
            }
        notes = self._build_notes(top5_weight, risk, missing_quotes, news)
        if not behavioral.get("tickerIntel"):
            notes.append("Behavioral signal model unavailable for this run; using baseline analytics only.")
        if missing_quotes and self.settings.alpha_vantage_api_key and not (
            self.settings.polygon_api_key or self.settings.fmp_api_key
        ):
            notes.append("Free Alpha Vantage limits can cause partial quotes. Wait ~60s and refresh analysis.")
        if len(position_rows) > self.settings.max_positions_for_risk:
            notes.append(
                f"Risk metrics computed on top {self.settings.max_positions_for_risk} holdings by market value."
            )
        data_quality = self._build_data_quality(req, position_rows, macro, news)
        deterministic_signals = self._build_signals(position_rows, notes, news, risk, macro, behavioral, macro_events=macro_events)
        ai_signals = await self.ai.build_signals(
            {
                "asOf": date.today().isoformat(),
                "portfolioValue": round(portfolio_value, 2),
                "positions": [p.model_dump() for p in position_rows[:8]],
                "risk": risk,
                "macro": {k: v.model_dump() for k, v in macro.items()},
                "notes": notes[:6],
                "behavioral": behavioral,
                "macroNews": [h.model_dump() for h in news.get("macro", [])[:8]],
                "macroCalendar": macro_events[:8],
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
            "providers": self._provider_status(quote_sources),
            "fundamentalsAttribution": dict(self.openbb.fundamentals_attribution),
            "quoteSources": quote_sources,
            "dataQuality": data_quality,
            "signals": signals,
            "model": {
                "name": "riskpulse-behavioral-v3",
                "type": "multi-model-stacking",
                "components": ["regime", "event-shock", "crowding", "alpha", "construction", "allocation"],
                "openbbEnriched": self.openbb.enabled,
            },
            "progress": {"phase": "full", "fullPending": False},
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

    async def analyze_valuation(self, tickers: list[str]) -> ValuationResponse:
        symbols = list(dict.fromkeys(t.strip().upper() for t in tickers if isinstance(t, str) and t.strip()))
        if not symbols:
            return ValuationResponse(as_of=date.today(), items=[], notes=["No valid tickers provided."])

        quotes = await self.market.get_quotes(symbols)
        intel_rows = await asyncio.gather(*(self.openbb.get_ticker_intel(symbol) for symbol in symbols), return_exceptions=True)
        items: list[ValuationPoint] = []
        notes: list[str] = []
        for symbol, intel_raw in zip(symbols, intel_rows, strict=False):
            quote = quotes.get(symbol)
            if quote is None:
                notes.append(f"Missing quote for {symbol}.")
            intel = intel_raw if isinstance(intel_raw, dict) else {}
            valuation = _valuation_intel(intel, quote.price if quote else None)
            methods = valuation.get("methods") if isinstance(valuation.get("methods"), list) else []
            fallbacks = intel.get("fallbacks") if isinstance(intel.get("fallbacks"), dict) else {}
            coverage = intel.get("coverage") if isinstance(intel.get("coverage"), dict) else {}
            val_inputs = coverage.get("valuationInputs")
            items.append(
                ValuationPoint(
                    ticker=symbol,
                    price=quote.price if quote else None,
                    price_source=quote.source if quote else None,
                    fair_value=_as_float(valuation.get("fairValue")),
                    margin_safety=_as_float(valuation.get("marginSafety")),
                    verdict=str(valuation.get("verdict") or "unknown"),
                    confidence=_as_float(valuation.get("confidence")) or 0.0,
                    valuation_inputs=int(val_inputs) if isinstance(val_inputs, (int, float)) else 0,
                    methods=[m for m in methods if isinstance(m, dict)][:5],
                    providers={
                        "openbb": self.openbb.enabled,
                        "alpha_vantage": bool(fallbacks.get("alphaVantageOverview")),
                        "yahoo_quote_summary": bool(fallbacks.get("yahooOverview")),
                        "yahoo_quote": bool(fallbacks.get("yahooQuote")),
                    },
                )
            )

        return ValuationResponse(as_of=date.today(), items=items, notes=notes)

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

        top_tickers = [p.ticker for p in positions[: self.settings.max_ticker_news_symbols]]
        tasks = [self.news.get_ticker_news(ticker, limit=self.settings.ticker_news_per_symbol) for ticker in top_tickers]
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

    async def _build_behavioral_intel(
        self,
        positions: list[PositionAnalysis],
        news: dict[str, list[Headline]],
        macro: dict[str, MacroPoint],
    ) -> dict[str, Any]:
        tracked = positions[: self.settings.max_positions_for_intel]
        if not tracked:
            return {
                "regime": {"state": "insufficient-data"},
                "tickerIntel": [],
                "opportunities": [],
                "exitSignals": [],
                "predictions": {},
                "portfolioActions": [],
                "hedgePlan": [],
                "construction": {},
                "alphaBook": {},
                "submodels": {},
            }

        history_tasks = [self.market.get_history(p.ticker, self.settings.history_days) for p in tracked]
        spy_task = self.market.get_history("SPY", self.settings.history_days)
        # Always request ticker intel because provider fallbacks (Yahoo/Alpha Vantage)
        # are used even when OpenBB base URL is not configured.
        openbb_tasks = [self.openbb.get_ticker_intel(p.ticker) for p in tracked]
        histories, spy_history, openbb_rows = await asyncio.gather(
            asyncio.gather(*history_tasks),
            spy_task,
            asyncio.gather(*openbb_tasks, return_exceptions=True),
        )
        technical_tasks = [
            self.market.get_technical_snapshot(
                p.ticker,
                prices=series,
                enrich_remote=idx < max(0, self.settings.alpha_vantage_technical_enriched_tickers),
            )
            for idx, (p, series) in enumerate(zip(tracked, histories, strict=False))
        ]
        technical_rows = await asyncio.gather(*technical_tasks, return_exceptions=True) if technical_tasks else []
        spy_metrics = _price_metrics(spy_history)

        macro_panic, macro_relief = _macro_stress_scores(macro)
        macro_news = news.get("macro", [])[:10]
        macro_risk_up = 0
        for item in macro_news:
            impact, direction, _ = _classify_headline(item.title, sentiment_hint=item.sentiment_hint)
            if direction == "risk-up":
                macro_risk_up += 1 if impact == "high" else 0.6
        macro_shock = _clamp01(macro_risk_up / 4.0)
        macro_risk_down = _clamp01(sum(1 for item in macro_news if "cool" in item.title.lower() or "rebound" in item.title.lower()) / 4.0)

        ticker_intel: list[dict[str, Any]] = []
        opportunities: list[dict[str, Any]] = []
        exit_signals: list[dict[str, Any]] = []
        for p, series, openbb_raw, tech_raw in zip(tracked, histories, openbb_rows, technical_rows, strict=False):
            openbb = openbb_raw if isinstance(openbb_raw, dict) else {}
            technical = tech_raw if isinstance(tech_raw, dict) else {}
            metrics = _price_metrics(series)
            returns_count = int(metrics.get("returnsCount", 0))
            ticker_news = news.get(p.ticker, [])[: self.settings.ticker_news_per_symbol]
            news_stats = _ticker_news_stats(ticker_news)
            valuation_intel = _valuation_intel(openbb, p.price)
            openbb_scores = _openbb_scores(openbb, valuation_intel)
            valuation_margin = _as_float(valuation_intel.get("marginSafety"))

            ret20 = metrics.get("ret20")
            spy_ret20 = spy_metrics.get("ret20")
            relative_20 = (ret20 - spy_ret20) if ret20 is not None and spy_ret20 is not None else None
            location = metrics.get("rangeLoc")
            drawdown = metrics.get("drawdown120") or 0.0
            ret5 = metrics.get("ret5") or 0.0
            vol_ratio = metrics.get("volRatio") or 1.0
            technical_oversold = _as_float(technical.get("oversoldScore")) or 0.0
            technical_overbought = _as_float(technical.get("overboughtScore")) or 0.0
            technical_trend = _as_float(technical.get("trendScore")) or 0.5
            technical_reversal = _as_float(technical.get("reversalScore")) or 0.0
            technical_strength = _as_float(technical.get("trendStrength")) or 0.35
            technical_score = _as_float(technical.get("technicalScore")) or 0.5

            oversold = _clamp01(
                (max(0.0, -ret5 - 0.015) / 0.08) * 0.25
                + (max(0.0, -(ret20 or 0.0) - 0.04) / 0.16) * 0.25
                + (max(0.0, 0.35 - (location if location is not None else 0.5)) / 0.35) * 0.2
                + (max(0.0, drawdown - 0.1) / 0.2) * 0.15
                + (max(0.0, news_stats["riskUpShare"] - 0.55) / 0.45) * 0.15
                + (technical_oversold * 0.2)
            )
            overheated = _clamp01(
                (max(0.0, ret5 - 0.018) / 0.08) * 0.25
                + (max(0.0, (ret20 or 0.0) - 0.04) / 0.16) * 0.25
                + (max(0.0, (location if location is not None else 0.5) - 0.7) / 0.3) * 0.2
                + (max(0.0, news_stats["riskDownShare"] - 0.55) / 0.45) * 0.15
                + (max(0.0, news_stats["buzz"] - 0.5) / 0.5) * 0.15
                + (technical_overbought * 0.22)
            )

            vol_spike_factor = min(1.0, max(0.0, vol_ratio - 0.8))
            panic_score = _clamp01(
                (oversold * 0.4)
                + (vol_spike_factor * 0.16)
                + (macro_panic * 0.2)
                + (news_stats["eventRisk"] * 0.1)
                + (technical_reversal * 0.08)
                + (max(0.0, 0.48 - technical_trend) * 0.06)
            )
            crowding_score = _clamp01(
                (overheated * 0.46)
                + (news_stats["buzz"] * 0.15)
                + (p.weight * 0.14)
                + (openbb_scores["flow"] * 0.13)
                + (technical_overbought * 0.12)
            )
            opportunity_index = _clamp01(
                (panic_score * 0.37)
                + (max(0.0, 0.55 - crowding_score) * 0.25)
                + (valuation_intel["undervaluationScore"] * 0.2)
                + (openbb_scores["quality"] * 0.1)
                + (technical_oversold * 0.1)
                + (technical_trend * technical_strength * 0.05)
            )
            distribution_index = _clamp01(
                (crowding_score * 0.42)
                + (max(0.0, 0.45 - panic_score) * 0.2)
                + (openbb_scores["flow"] * 0.2)
                + (valuation_intel["overvaluationScore"] * 0.15)
                + (technical_overbought * 0.12)
                + (max(0.0, technical_trend - 0.65) * 0.1)
            )
            confidence = _signal_confidence(returns_count, len(ticker_news), relative_20)
            confidence = _clamp01(confidence + (0.08 if openbb_scores["coverage"] > 0 else 0.0))
            if technical:
                confidence = _clamp01(confidence + 0.06)

            action_bias = _action_bias(
                opportunity_index,
                distribution_index,
                macro_panic,
                news_stats["eventRisk"],
                valuation_margin,
            )
            confirmation_state = _confirmation_state(
                ret5=ret5,
                ret20=ret20,
                location=location,
                technical_trend=technical_trend,
                technical_reversal=technical_reversal,
                technical_strength=technical_strength,
                risk_up_share=news_stats["riskUpShare"],
            )
            entry_discipline = _entry_discipline(
                action_bias=action_bias,
                ret5=ret5,
                ret20=ret20,
                drawdown=drawdown,
                panic_score=panic_score,
                event_risk=news_stats["eventRisk"],
                technical_trend=technical_trend,
                technical_reversal=technical_reversal,
                margin_safety=valuation_margin,
            )
            analyst_triggers = _analyst_triggers(
                ticker=p.ticker,
                action_bias=action_bias,
                confirmation_state=confirmation_state,
                entry_discipline=entry_discipline,
                location=location,
                relative_20=relative_20,
                macro_panic=macro_panic,
                event_risk=news_stats["eventRisk"],
                valuation_margin=valuation_margin,
            )
            macro_gate = _macro_gate_from_scores(macro_panic, macro_shock, macro_relief)
            layer_scores = _layer_scores(
                regime_panic=macro_panic,
                macro_gate=macro_gate,
                technical_trend=technical_trend,
                technical_oversold=technical_oversold,
                technical_overbought=technical_overbought,
                action_bias=action_bias,
                confirmation_state=confirmation_state,
                valuation_margin=valuation_margin,
                event_risk=news_stats["eventRisk"],
                risk_up_share=news_stats["riskUpShare"],
                risk_down_share=news_stats["riskDownShare"],
                headline_count=len(ticker_news),
            )
            confluence_score = _confluence_score(layer_scores, macro_gate)
            rationale = _action_rationale(
                action_bias=action_bias,
                ret5=ret5,
                ret20=ret20,
                drawdown=drawdown,
                location=location,
                risk_up_share=news_stats["riskUpShare"],
                risk_down_share=news_stats["riskDownShare"],
                relative_20=relative_20,
                margin_safety=valuation_margin,
            )

            intel_row = {
                "ticker": p.ticker,
                "weight": round(p.weight, 4),
                "panicScore": round(panic_score, 3),
                "crowdingScore": round(crowding_score, 3),
                "opportunityIndex": round(opportunity_index, 3),
                "distributionIndex": round(distribution_index, 3),
                "eventRisk": round(news_stats["eventRisk"], 3),
                "actionBias": action_bias,
                "confidence": round(confidence, 3),
                "themes": news_stats["themes"][:4],
                "headlineCount": len(ticker_news),
                "rationale": rationale,
                "alphaScore": round((opportunity_index - distribution_index) * confidence, 3),
                "confirmationState": confirmation_state,
                "entryDiscipline": entry_discipline,
                "macroGate": macro_gate,
                "layerScores": layer_scores,
                "confluenceScore": confluence_score,
                "analystRead": analyst_triggers,
                "technical": technical,
                "openbb": openbb,
                "valuation": valuation_intel,
                "features": {
                    "ret5d": round(ret5, 4) if ret5 is not None else None,
                    "ret20d": round(ret20, 4) if ret20 is not None else None,
                    "relative20dVsSPY": round(relative_20, 4) if relative_20 is not None else None,
                    "drawdown120d": round(drawdown, 4) if drawdown is not None else None,
                    "rangeLocation120d": round(location, 4) if location is not None else None,
                    "volatilityRatio": round(vol_ratio, 3) if vol_ratio is not None else None,
                    "valuationScore": round(openbb_scores["valuation"], 3),
                    "qualityScore": round(openbb_scores["quality"], 3),
                    "flowScore": round(openbb_scores["flow"], 3),
                    "fairValue": valuation_intel["fairValue"],
                    "marginSafety": valuation_intel["marginSafety"],
                    "valuationVerdict": valuation_intel["verdict"],
                    "valuationConfidence": valuation_intel["confidence"],
                    "technicalScore": round(technical_score, 3),
                    "technicalTrendScore": round(technical_trend, 3),
                    "technicalOversold": round(technical_oversold, 3),
                    "technicalOverbought": round(technical_overbought, 3),
                    "technicalState": str(technical.get("signalState") or "unknown"),
                },
            }
            ticker_intel.append(intel_row)

            if (
                (opportunity_index >= 0.67 and confidence >= 0.45)
                or (
                    valuation_margin is not None
                    and valuation_margin >= 0.12
                    and valuation_intel["confidence"] >= 0.4
                    and news_stats["eventRisk"] < 0.75
                )
            ):
                opp_reason = rationale
                fair_value = valuation_intel.get("fairValue")
                if isinstance(fair_value, (int, float)):
                    opp_reason = (
                        f"{rationale} Intrinsic blend fair value ${float(fair_value):,.2f} vs spot ${p.price:,.2f}."
                    )
                opportunities.append(
                    {
                        "ticker": p.ticker,
                        "score": round(max(opportunity_index, valuation_intel["undervaluationScore"]), 3),
                        "confidence": round(confidence, 3),
                        "signal": "intrinsic-undervaluation" if valuation_margin is not None and valuation_margin >= 0.12 else "undervaluation-window",
                        "reason": opp_reason,
                    }
                )
            if (
                (distribution_index >= 0.67 and confidence >= 0.45)
                or (valuation_margin is not None and valuation_margin <= -0.12 and valuation_intel["confidence"] >= 0.4)
            ):
                exit_reason = rationale
                fair_value = valuation_intel.get("fairValue")
                if isinstance(fair_value, (int, float)):
                    exit_reason = (
                        f"{rationale} Intrinsic blend fair value ${float(fair_value):,.2f} vs spot ${p.price:,.2f}."
                    )
                exit_signals.append(
                    {
                        "ticker": p.ticker,
                        "score": round(max(distribution_index, valuation_intel["overvaluationScore"]), 3),
                        "confidence": round(confidence, 3),
                        "signal": "intrinsic-overvaluation" if valuation_margin is not None and valuation_margin <= -0.12 else "crowded-upside",
                        "reason": exit_reason,
                    }
                )

        weighted_panic = _weighted_signal(ticker_intel, "panicScore")
        weighted_crowding = _weighted_signal(ticker_intel, "crowdingScore")
        weighted_event_risk = _weighted_signal(ticker_intel, "eventRisk")
        weighted_opportunity = _weighted_signal(ticker_intel, "opportunityIndex")
        weighted_distribution = _weighted_signal(ticker_intel, "distributionIndex")
        weighted_alpha = _weighted_signal(ticker_intel, "alphaScore")
        regime_panic = _clamp01((weighted_panic * 0.6) + (macro_panic * 0.25) + (macro_shock * 0.15))
        regime_crowding = _clamp01((weighted_crowding * 0.7) + (macro_relief * 0.2) + (1 - macro_shock) * 0.1)
        regime_state = _regime_label(regime_panic, regime_crowding)
        regime_probabilities = _regime_probabilities(regime_panic, regime_crowding, macro_shock, macro_risk_down)

        downside_5d = _clamp01(0.18 + (regime_panic * 0.36) + (weighted_event_risk * 0.22) + (weighted_distribution * 0.18) - (weighted_opportunity * 0.14))
        upside_5d = _clamp01(0.14 + ((1 - regime_panic) * 0.2) + (weighted_opportunity * 0.32) + (macro_risk_down * 0.12) - (weighted_distribution * 0.12))
        expected_5d = _expected_return_5d(weighted_opportunity, weighted_distribution, regime_panic, weighted_event_risk)
        expected_20d = _expected_return_20d(weighted_opportunity, weighted_distribution, regime_panic, weighted_event_risk, macro_relief)
        prediction_conf = _clamp01((sum(row.get("confidence", 0.0) for row in ticker_intel if isinstance(row.get("confidence"), (int, float))) / max(len(ticker_intel), 1)) * 0.9 + 0.05)

        construction = _construct_portfolio_targets(ticker_intel, regime_state)
        portfolio_actions = _action_book_from_targets(construction.get("targets", []), ticker_intel)
        hedge_plan = _hedge_plan(regime_state, regime_panic, weighted_event_risk)
        alpha_book = _alpha_book(ticker_intel)
        submodels = {
            "regime": {"score": round(regime_panic, 3), "confidence": round(_clamp01(0.5 + prediction_conf * 0.5), 3)},
            "alpha": {"score": round(weighted_alpha, 3), "confidence": round(_clamp01(0.45 + weighted_opportunity * 0.35 + (1 - weighted_distribution) * 0.2), 3)},
            "event": {"score": round(weighted_event_risk, 3), "confidence": round(_clamp01(0.4 + (len(macro_news) / 20.0) + (weighted_event_risk * 0.3)), 3)},
            "crowding": {"score": round(weighted_crowding, 3), "confidence": round(_clamp01(0.4 + weighted_crowding * 0.4 + weighted_distribution * 0.2), 3)},
        }

        opportunities.sort(key=lambda row: (row["score"], row["confidence"]), reverse=True)
        exit_signals.sort(key=lambda row: (row["score"], row["confidence"]), reverse=True)
        ticker_intel.sort(key=lambda row: row.get("weight", 0), reverse=True)
        return {
            "regime": {
                "state": regime_state,
                "panicScore": round(regime_panic, 3),
                "crowdingScore": round(regime_crowding, 3),
                "macroShock": round(macro_shock, 3),
                "probabilities": regime_probabilities,
            },
            "tickerIntel": ticker_intel,
            "opportunities": opportunities[:4],
            "exitSignals": exit_signals[:4],
            "predictions": {
                "horizon5d": {
                    "downsideProb": round(downside_5d, 3),
                    "upsideProb": round(upside_5d, 3),
                    "expectedReturn": round(expected_5d, 4),
                },
                "horizon20d": {
                    "expectedReturn": round(expected_20d, 4),
                    "downsideProb": round(_clamp01(downside_5d * 0.88), 3),
                    "upsideProb": round(_clamp01(upside_5d * 0.93), 3),
                },
                "confidence": round(prediction_conf, 3),
            },
            "portfolioActions": portfolio_actions[:8],
            "hedgePlan": hedge_plan[:4],
            "construction": construction,
            "alphaBook": alpha_book,
            "submodels": submodels,
            "technicalSummary": _technical_summary(ticker_intel, tracked_count=len(tracked)),
            "analystDesk": _analyst_desk_summary(
                ticker_intel=ticker_intel,
                regime_state=regime_state,
                macro_gate=_macro_gate_from_scores(macro_panic, macro_shock, macro_relief),
                weighted_opportunity=weighted_opportunity,
                weighted_distribution=weighted_distribution,
                weighted_event_risk=weighted_event_risk,
            ),
        }

    def _build_signals(
        self,
        positions: list[PositionAnalysis],
        notes: list[str],
        news: dict[str, list[Headline]],
        risk: dict[str, float | None],
        macro: dict[str, MacroPoint],
        behavioral: dict[str, Any],
        macro_events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        top_tickers = [p.ticker for p in positions[:5]]
        radar = self._build_headline_radar(news, top_tickers)
        watchouts = self._build_watchouts(positions, radar, behavioral)
        scenarios = self._build_scenarios(positions)
        warnings = self._build_warnings(notes, risk, macro, radar, scenarios, behavioral)
        pulse = _build_pulse(
            warnings=warnings,
            scenarios=scenarios,
            behavioral=behavioral,
            radar=radar,
            macro=macro,
            risk=risk,
        )
        macro_context = _build_macro_context(macro, news.get("macro", []), positions, macro_events)
        theme_board = _build_theme_board(radar, behavioral)
        return {
            "pulse": pulse,
            "warnings": warnings,
            "watchouts": watchouts,
            "radar": radar,
            "scenarios": scenarios,
            "themes": theme_board,
            "regime": behavioral.get("regime", {}),
            "tickerIntel": behavioral.get("tickerIntel", []),
            "opportunities": behavioral.get("opportunities", []),
            "exitSignals": behavioral.get("exitSignals", []),
            "predictions": behavioral.get("predictions", {}),
            "portfolioActions": behavioral.get("portfolioActions", []),
            "hedgePlan": behavioral.get("hedgePlan", []),
            "construction": behavioral.get("construction", {}),
            "alphaBook": behavioral.get("alphaBook", {}),
            "submodels": behavioral.get("submodels", {}),
            "technicalSummary": behavioral.get("technicalSummary", {}),
            "analystDesk": behavioral.get("analystDesk", {}),
            "macroContext": macro_context,
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

    def _build_watchouts(
        self,
        positions: list[PositionAnalysis],
        radar: list[dict[str, Any]],
        behavioral: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ticker_intel = {
            row.get("ticker"): row
            for row in behavioral.get("tickerIntel", [])
            if isinstance(row, dict) and isinstance(row.get("ticker"), str)
        }
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

            intel = ticker_intel.get(p.ticker)
            if isinstance(intel, dict):
                action_bias = str(intel.get("actionBias") or "")
                if action_bias == "accumulate-on-weakness":
                    severity_rank = max(severity_rank, 1)
                    opp_score = intel.get("opportunityIndex") if isinstance(intel.get("opportunityIndex"), (int, float)) else 0.0
                    reasons.append(f"Dislocation setup score {opp_score:.2f}.")
                elif action_bias == "trim-into-strength":
                    severity_rank = max(severity_rank, 2)
                    dist_score = intel.get("distributionIndex") if isinstance(intel.get("distributionIndex"), (int, float)) else 0.0
                    reasons.append(f"Crowding setup score {dist_score:.2f}.")
                elif action_bias == "de-risk-hedge":
                    severity_rank = max(severity_rank, 2)
                    reasons.append("Panic and event-risk scores are elevated; hedge posture is favored.")

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
        behavioral: dict[str, Any],
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

        regime = behavioral.get("regime", {}) if isinstance(behavioral, dict) else {}
        regime_state = regime.get("state")
        regime_panic = regime.get("panicScore")
        if regime_state == "stress" and isinstance(regime_panic, (int, float)):
            out.append(
                {
                    "title": "Regime Stress",
                    "severity": "high",
                    "reason": f"Panic regime score is {regime_panic:.2f}; prioritize capital preservation.",
                }
            )

        opportunities = behavioral.get("opportunities", []) if isinstance(behavioral, dict) else []
        if isinstance(opportunities, list) and opportunities:
            top = opportunities[0] if isinstance(opportunities[0], dict) else None
            if top and isinstance(top.get("ticker"), str):
                signal_name = str(top.get("signal") or "multi-factor dislocation")
                out.append(
                    {
                        "title": "Dislocation Opportunity",
                        "severity": "medium",
                        "reason": f"{top['ticker']} screens as {signal_name} with valuation and event confirmation.",
                    }
                )

        exit_signals = behavioral.get("exitSignals", []) if isinstance(behavioral, dict) else []
        if isinstance(exit_signals, list) and exit_signals:
            top = exit_signals[0] if isinstance(exit_signals[0], dict) else None
            if top and isinstance(top.get("ticker"), str):
                out.append(
                    {
                        "title": "Crowding Distribution Risk",
                        "severity": "medium",
                        "reason": f"{top['ticker']} shows a crowded-upside pattern where trimming into strength is favored.",
                    }
                )

        predictions = behavioral.get("predictions", {}) if isinstance(behavioral, dict) else {}
        if isinstance(predictions, dict):
            h5 = predictions.get("horizon5d")
            if isinstance(h5, dict):
                downside = h5.get("downsideProb")
                if isinstance(downside, (int, float)) and downside >= 0.55:
                    out.append(
                        {
                            "title": "Short-Horizon Downside Risk",
                            "severity": "high" if downside >= 0.65 else "medium",
                            "reason": f"Model downside probability for next 5d is {downside:.0%}.",
                        }
                    )

        technical_summary = behavioral.get("technicalSummary", {}) if isinstance(behavioral, dict) else {}
        if isinstance(technical_summary, dict):
            bearish = technical_summary.get("bearishShare")
            overbought = technical_summary.get("overboughtShare")
            oversold = technical_summary.get("oversoldShare")
            if isinstance(bearish, (int, float)) and isinstance(overbought, (int, float)) and bearish >= 0.58 and overbought >= 0.5:
                out.append(
                    {
                        "title": "Technical Exhaustion",
                        "severity": "medium",
                        "reason": f"Technical stack shows bearish breadth {bearish:.0%} with overbought pressure {overbought:.0%}.",
                    }
                )
            elif isinstance(oversold, (int, float)) and oversold >= 0.56:
                out.append(
                    {
                        "title": "Washout Setup",
                        "severity": "low",
                        "reason": f"Oversold breadth is elevated at {oversold:.0%}, increasing mean-reversion potential.",
                    }
                )

        construction = behavioral.get("construction", {}) if isinstance(behavioral, dict) else {}
        if isinstance(construction, dict):
            projected_top1 = construction.get("projectedTop1")
            turnover = construction.get("projectedTurnover")
            if isinstance(projected_top1, (int, float)) and projected_top1 >= 0.42:
                out.append(
                    {
                        "title": "Projected Concentration",
                        "severity": "medium" if projected_top1 < 0.5 else "high",
                        "reason": f"Model target book projects top holding at {projected_top1:.0%}.",
                    }
                )
            if isinstance(turnover, (int, float)) and turnover >= 0.28:
                out.append(
                    {
                        "title": "Turnover Pressure",
                        "severity": "medium",
                        "reason": f"Model target turnover is {turnover:.0%}; execution costs may rise.",
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
            "themes": list(base.get("themes", [])),
            "regime": base.get("regime", {}),
            "tickerIntel": list(base.get("tickerIntel", [])),
            "opportunities": list(base.get("opportunities", [])),
            "exitSignals": list(base.get("exitSignals", [])),
            "predictions": base.get("predictions", {}),
            "portfolioActions": list(base.get("portfolioActions", [])),
            "hedgePlan": list(base.get("hedgePlan", [])),
            "construction": base.get("construction", {}),
            "alphaBook": base.get("alphaBook", {}),
            "submodels": base.get("submodels", {}),
            "technicalSummary": base.get("technicalSummary", {}),
            "analystDesk": base.get("analystDesk", {}),
            "macroContext": base.get("macroContext", {}),
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
        merged["themes"] = merged["themes"][:6]
        merged["tickerIntel"] = merged["tickerIntel"][:10]
        merged["opportunities"] = merged["opportunities"][:4]
        merged["exitSignals"] = merged["exitSignals"][:4]
        merged["portfolioActions"] = merged["portfolioActions"][:8]
        merged["hedgePlan"] = merged["hedgePlan"][:4]
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
_SENTIMENT_POSITIVE = (
    "beats",
    "beat",
    "upgrade",
    "buyback",
    "partnership",
    "record",
    "growth",
    "strong demand",
    "rebound",
    "rally",
)
_SENTIMENT_NEGATIVE = (
    "misses",
    "miss",
    "downgrade",
    "investigation",
    "lawsuit",
    "probe",
    "warning",
    "cut guidance",
    "layoffs",
    "fraud",
)
_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earnings": ("earnings", "guidance", "eps", "revenue", "quarter"),
    "rates": ("fed", "rate", "yield", "treasury", "inflation"),
    "regulation": ("regulation", "antitrust", "doj", "sec", "fda", "ban"),
    "deals": ("merger", "acquisition", "deal", "takeover", "partnership"),
    "product": ("launch", "product", "iphone", "chip", "ai", "cloud"),
    "geopolitics": ("war", "conflict", "tariff", "sanction", "iran", "china"),
}

_MACRO_EVENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "inflation": ("cpi", "pce", "inflation", "ppi", "prices"),
    "labor": ("jobs", "payroll", "nfp", "unemployment", "adp", "claims", "labor"),
    "growth": ("gdp", "pmi", "ism", "retail sales", "durable goods", "manufacturing", "services"),
    "policy": ("fed", "fomc", "rate decision", "rate hike", "rate cut", "dot plot", "minutes"),
    "energy": ("oil", "crude", "eia", "inventory", "gasoline", "brent", "wti"),
}


def _macro_stress_scores(macro: dict[str, MacroPoint]) -> tuple[float, float]:
    vix_change = ((macro.get("VIX") or MacroPoint()).chg_pct_1d or 0.0)
    rates_change = ((macro.get("US10Y") or MacroPoint()).chg_bp_1d or 0.0)
    dollar_change = ((macro.get("DXY") or MacroPoint()).chg_pct_1d or 0.0)
    spy_change = ((macro.get("SPY") or MacroPoint()).chg_pct_1d or 0.0)

    vix_jump = max(0.0, vix_change)
    rates_jump = max(0.0, rates_change)
    dollar_jump = max(0.0, dollar_change)
    spy_drop = max(0.0, -spy_change)

    panic = _clamp01((vix_jump / 0.12) * 0.45 + (rates_jump / 12.0) * 0.2 + (dollar_jump / 0.012) * 0.15 + (spy_drop / 0.02) * 0.2)
    relief = _clamp01((max(0.0, -vix_change) / 0.08) * 0.5 + (max(0.0, spy_change) / 0.015) * 0.3 + (max(0.0, -rates_change) / 10.0) * 0.2)
    return panic, relief


def _price_metrics(prices: list[float]) -> dict[str, float | int | None]:
    if len(prices) < 3:
        return {
            "ret5": None,
            "ret20": None,
            "ret60": None,
            "drawdown120": None,
            "vol20": None,
            "vol60": None,
            "volRatio": None,
            "rangeLoc": None,
            "returnsCount": 0,
        }
    returns = _daily_returns(prices)
    vol20 = _annualized_vol(returns[-20:]) if len(returns) >= 20 else None
    vol60 = _annualized_vol(returns[-60:]) if len(returns) >= 60 else None

    window = prices[-120:] if len(prices) >= 120 else prices
    min_px = min(window)
    max_px = max(window)
    loc = None
    if max_px > min_px:
        loc = (window[-1] - min_px) / (max_px - min_px)

    return {
        "ret5": _window_return(prices, 5),
        "ret20": _window_return(prices, 20),
        "ret60": _window_return(prices, 60),
        "drawdown120": _max_drawdown(window),
        "vol20": vol20,
        "vol60": vol60,
        "volRatio": (vol20 / vol60) if vol20 and vol60 and vol60 > 0 else None,
        "rangeLoc": loc,
        "returnsCount": len(returns),
    }


def _ticker_news_stats(items: list[Headline]) -> dict[str, Any]:
    if not items:
        return {"riskUpShare": 0.0, "riskDownShare": 0.0, "buzz": 0.0, "eventRisk": 0.0, "themes": []}

    risk_up = 0.0
    risk_down = 0.0
    high_impact = 0.0
    sentiment_sum = 0.0
    theme_counts: dict[str, int] = {}
    weight_sum = 0.0
    for item in items:
        impact, direction, _ = _classify_headline(item.title, sentiment_hint=item.sentiment_hint)
        recency_weight = _recency_weight(item.published_at)
        w = recency_weight * (1.35 if impact == "high" else 1.0)
        weight_sum += w
        if direction == "risk-up":
            risk_up += w
        elif direction == "risk-down":
            risk_down += w
        if impact == "high":
            high_impact += w
        sentiment_sum += _headline_sentiment(item.title, item.sentiment_hint) * w
        for theme in _headline_themes(item.title):
            theme_counts[theme] = theme_counts.get(theme, 0) + 1

    normalized = weight_sum or 1.0
    risk_up_share = _clamp01(risk_up / normalized)
    risk_down_share = _clamp01(risk_down / normalized)
    buzz = _clamp01((len(items) / 6.0) * 0.55 + (high_impact / normalized) * 0.45)
    event_risk = _clamp01((risk_up_share * 0.65) + ((high_impact / normalized) * 0.2) + (max(0.0, -sentiment_sum / normalized) * 0.15))
    themes = sorted(theme_counts, key=theme_counts.get, reverse=True)
    return {
        "riskUpShare": risk_up_share,
        "riskDownShare": risk_down_share,
        "buzz": buzz,
        "eventRisk": event_risk,
        "themes": themes,
    }


def _headline_sentiment(title: str, sentiment_hint: str | None = None) -> float:
    lower = title.lower()
    score = 0.0
    score += sum(1 for token in _SENTIMENT_POSITIVE if token in lower) * 0.3
    score -= sum(1 for token in _SENTIMENT_NEGATIVE if token in lower) * 0.35
    if sentiment_hint:
        hint = sentiment_hint.lower()
        if "positive" in hint or "bull" in hint:
            score += 0.35
        elif "negative" in hint or "bear" in hint:
            score -= 0.35
    return max(-1.0, min(1.0, score))


def _headline_themes(title: str) -> list[str]:
    lower = title.lower()
    out: list[str] = []
    for theme, keywords in _THEME_KEYWORDS.items():
        if any(token in lower for token in keywords):
            out.append(theme)
    return out


def _recency_weight(published_at: str | None) -> float:
    dt = _parse_datetime(published_at)
    if dt is None:
        return 0.7
    age_hours = max(0.0, (datetime.now(tz=UTC) - dt).total_seconds() / 3600.0)
    if age_hours <= 12:
        return 1.0
    if age_hours <= 48:
        return 0.82
    if age_hours <= 120:
        return 0.64
    return 0.45


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    compact = _alpha_timestamp_to_iso(text)
    if compact:
        return compact
    try:
        dt = parsedate_to_datetime(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


def _alpha_timestamp_to_iso(text: str) -> datetime | None:
    if not re.fullmatch(r"\d{8}T\d{6}", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _window_return(prices: list[float], days: int) -> float | None:
    if len(prices) <= days:
        return None
    start = prices[-(days + 1)]
    end = prices[-1]
    if start <= 0:
        return None
    return (end / start) - 1


def _weighted_signal(rows: list[dict[str, Any]], key: str) -> float:
    total_weight = 0.0
    value_weighted = 0.0
    for row in rows:
        weight = row.get("weight")
        value = row.get(key)
        if not isinstance(weight, (int, float)) or not isinstance(value, (int, float)):
            continue
        total_weight += float(weight)
        value_weighted += float(weight) * float(value)
    if total_weight <= 0:
        return 0.0
    return _clamp01(value_weighted / total_weight)


def _signal_confidence(history_points: int, headline_count: int, relative_20: float | None) -> float:
    base = 0.25
    base += min(0.45, history_points / 180.0)
    base += min(0.2, headline_count / 12.0)
    if relative_20 is not None:
        base += 0.1
    return _clamp01(base)


def _action_bias(opportunity: float, distribution: float, macro_panic: float, event_risk: float, margin_safety: float | None) -> str:
    if margin_safety is not None and margin_safety >= 0.15 and macro_panic <= 0.9 and event_risk <= 0.82:
        return "accumulate-on-weakness"
    if margin_safety is not None and margin_safety <= -0.12 and distribution >= 0.55:
        return "trim-into-strength"
    if opportunity >= 0.68 and macro_panic <= 0.82 and event_risk <= 0.8:
        return "accumulate-on-weakness"
    if distribution >= 0.68 and macro_panic <= 0.88:
        return "trim-into-strength"
    if macro_panic >= 0.72 or event_risk >= 0.72:
        return "de-risk-hedge"
    return "watch-hold"


def _action_rationale(
    action_bias: str,
    ret5: float,
    ret20: float | None,
    drawdown: float,
    location: float | None,
    risk_up_share: float,
    risk_down_share: float,
    relative_20: float | None,
    margin_safety: float | None,
) -> str:
    rel = f"{relative_20:+.1%}" if relative_20 is not None else "n/a"
    loc = f"{location:.0%}" if location is not None else "n/a"
    ret20_txt = f"{ret20:+.1%}" if ret20 is not None else "n/a"
    mos_txt = f"{margin_safety:+.1%}" if margin_safety is not None else "n/a"
    if action_bias == "accumulate-on-weakness":
        return f"Deep pullback setup: 5d {ret5:+.1%}, 20d {ret20_txt}, drawdown {drawdown:.1%}, range location {loc}, vs SPY {rel}, margin of safety {mos_txt}."
    if action_bias == "trim-into-strength":
        return f"Crowded upside setup: 5d {ret5:+.1%}, 20d {ret20_txt}, range location {loc}, positive headline pressure {risk_down_share:.0%}, margin of safety {mos_txt}."
    if action_bias == "de-risk-hedge":
        return f"Risk pressure setup: drawdown {drawdown:.1%}, risk-up headline share {risk_up_share:.0%}, relative performance {rel}, margin of safety {mos_txt}."
    return f"Mixed setup: 5d {ret5:+.1%}, 20d {ret20_txt}, range location {loc}, risk-up headlines {risk_up_share:.0%}, margin of safety {mos_txt}."


def _confirmation_state(
    *,
    ret5: float,
    ret20: float | None,
    location: float | None,
    technical_trend: float,
    technical_reversal: float,
    technical_strength: float,
    risk_up_share: float,
) -> str:
    loc = location if location is not None else 0.5
    ret20_value = ret20 if ret20 is not None else 0.0
    if ret5 <= -0.035 and ret20_value <= -0.06 and technical_trend <= 0.38 and risk_up_share >= 0.45:
        return "confirmed-breakdown"
    if ret5 <= -0.02 and loc <= 0.3 and technical_reversal >= 0.45:
        return "failed-breakdown-watch"
    if ret5 >= 0.025 and technical_trend >= 0.58 and technical_strength >= 0.45:
        return "reclaiming-resistance"
    if loc <= 0.35 or technical_trend <= 0.45:
        return "watching-support"
    if loc >= 0.72 and technical_trend >= 0.55:
        return "watching-exhaustion"
    return "unconfirmed-mixed"


def _entry_discipline(
    *,
    action_bias: str,
    ret5: float,
    ret20: float | None,
    drawdown: float,
    panic_score: float,
    event_risk: float,
    technical_trend: float,
    technical_reversal: float,
    margin_safety: float | None,
) -> str:
    ret20_value = ret20 if ret20 is not None else 0.0
    if ret5 <= -0.045 and ret20_value <= -0.08 and event_risk >= 0.45 and technical_reversal < 0.35:
        return "no-catch"
    if margin_safety is not None and margin_safety >= 0.1 and technical_trend < 0.45:
        return "cheap-but-not-ready"
    if action_bias == "accumulate-on-weakness" and technical_reversal >= 0.4 and panic_score <= 0.72:
        return "starter-size-ok"
    if drawdown >= 0.18 and technical_reversal >= 0.5 and event_risk < 0.6:
        return "confirmed-reversal"
    if action_bias == "trim-into-strength":
        return "trim-discipline"
    return "wait-for-confirmation"


def _analyst_triggers(
    *,
    ticker: str,
    action_bias: str,
    confirmation_state: str,
    entry_discipline: str,
    location: float | None,
    relative_20: float | None,
    macro_panic: float,
    event_risk: float,
    valuation_margin: float | None,
) -> dict[str, Any]:
    loc_txt = f"{location:.0%}" if location is not None else "unknown range location"
    rel_txt = f"{relative_20:+.1%} vs SPY" if relative_20 is not None else "relative strength unavailable"
    mos_txt = f"{valuation_margin:+.1%} margin of safety" if valuation_margin is not None else "valuation not confirmed"

    if entry_discipline in {"no-catch", "cheap-but-not-ready"}:
        thesis = f"{ticker} may be interesting, but the tape has not earned an entry yet."
        confirms = "Wait for a close/open reclaim, improving relative strength, and lower headline event risk."
        invalidates = "Avoid adding if price keeps making lower opens/closes while macro stress stays elevated."
    elif action_bias == "accumulate-on-weakness":
        thesis = f"{ticker} screens as a dislocation candidate, but position size should follow confirmation."
        confirms = "A support hold plus improving relative strength validates scaling into weakness."
        invalidates = "A confirmed support break with risk-up headlines turns the setup into capital preservation."
    elif action_bias == "trim-into-strength":
        thesis = f"{ticker} shows crowded upside; strength is more useful for discipline than chasing."
        confirms = "Failure to extend after a high-range move supports trimming into liquidity."
        invalidates = "A clean breakout with improving breadth reduces immediate distribution risk."
    elif action_bias == "de-risk-hedge":
        thesis = f"{ticker} is being dominated by event or macro risk."
        confirms = "Hedge posture remains favored while macro panic or event risk stays high."
        invalidates = "Risk can be relaxed after volatility, macro drivers, and headlines cool together."
    else:
        thesis = f"{ticker} is a conditional hold; the next signal matters more than the current score."
        confirms = "Upgrade only after price confirmation and cleaner catalyst flow."
        invalidates = "Downgrade if support fails or event risk rises without offsetting valuation support."

    return {
        "thesis": thesis,
        "confirmsIf": confirms,
        "invalidatesIf": invalidates,
        "whyNow": f"State={confirmation_state}; entry={entry_discipline}; location={loc_txt}; relative={rel_txt}; {mos_txt}; macro panic {macro_panic:.0%}; event risk {event_risk:.0%}.",
    }


def _macro_gate_from_scores(macro_panic: float, macro_shock: float, macro_relief: float) -> dict[str, Any]:
    if macro_panic >= 0.65 or macro_shock >= 0.55:
        return {
            "state": "open-risk-gate",
            "driver": "volatility/rates/headline shock",
            "condition": "Require stronger technical confirmation before adding exposure.",
            "bias": "risk-up",
            "factor": 0.5,
        }
    if macro_relief >= 0.45 and macro_panic <= 0.42:
        return {
            "state": "relief-gate",
            "driver": "volatility/rates relief",
            "condition": "Opportunity setups can be scaled if price confirmation appears.",
            "bias": "risk-down",
            "factor": 1.0,
        }
    return {
        "state": "neutral-gate",
        "driver": "mixed macro tape",
        "condition": "Let single-name confirmation and catalyst quality drive decisions.",
        "bias": "balanced",
        "factor": 1.0,
    }


def _layer_scores(
    *,
    regime_panic: float,
    macro_gate: dict[str, Any],
    technical_trend: float,
    technical_oversold: float,
    technical_overbought: float,
    action_bias: str,
    confirmation_state: str,
    valuation_margin: float | None,
    event_risk: float,
    risk_up_share: float,
    risk_down_share: float,
    headline_count: int,
) -> dict[str, Any]:
    regime = 0
    if macro_gate.get("bias") == "risk-up" or regime_panic >= 0.62:
        regime = -1
    elif macro_gate.get("bias") == "risk-down" or regime_panic <= 0.32:
        regime = 1

    technical = 0
    if confirmation_state == "confirmed-breakdown" or (technical_trend <= 0.38 and technical_overbought < 0.25):
        technical = -1
    elif confirmation_state in {"reclaiming-resistance", "failed-breakdown-watch"} or (
        technical_trend >= 0.58 and technical_oversold < 0.35
    ):
        technical = 1
    elif technical_oversold >= 0.58 and technical_trend >= 0.42:
        technical = 1
    elif technical_overbought >= 0.62 and technical_trend <= 0.52:
        technical = -1

    event = 0
    if event_risk >= 0.62 or risk_up_share >= 0.62:
        event = -1
    elif risk_down_share >= 0.55 and event_risk <= 0.45 and headline_count > 0:
        event = 1

    if action_bias == "accumulate-on-weakness" and valuation_margin is not None and valuation_margin >= 0.12:
        event = max(event, 1)
    elif action_bias == "trim-into-strength" and valuation_margin is not None and valuation_margin <= -0.1:
        event = min(event, -1)

    return {
        "regime": regime,
        "technical": technical,
        "event": event,
        "explanation": _layer_score_explanation(regime, technical, event, macro_gate, confirmation_state),
    }


def _confluence_score(layer_scores: dict[str, Any], macro_gate: dict[str, Any]) -> dict[str, Any]:
    regime = int(layer_scores.get("regime") or 0)
    technical = int(layer_scores.get("technical") or 0)
    event = int(layer_scores.get("event") or 0)
    raw = regime + technical + event
    factor = _as_float(macro_gate.get("factor")) or 1.0
    final = raw * factor
    if abs(final) >= 2:
        state = "actionable"
    elif abs(final) >= 1:
        state = "watch"
    else:
        state = "mixed"
    return {
        "raw": round(raw, 3),
        "final": round(final, 3),
        "macroGateFactor": round(factor, 3),
        "state": state,
    }


def _layer_score_explanation(
    regime: int,
    technical: int,
    event: int,
    macro_gate: dict[str, Any],
    confirmation_state: str,
) -> str:
    regime_txt = "supportive" if regime > 0 else "hostile" if regime < 0 else "neutral"
    technical_txt = "constructive" if technical > 0 else "broken" if technical < 0 else "unconfirmed"
    event_txt = "supportive" if event > 0 else "risk-up" if event < 0 else "neutral"
    gate_txt = str(macro_gate.get("state") or "neutral-gate")
    return f"Regime {regime_txt}; technical {technical_txt} ({confirmation_state}); event layer {event_txt}; macro gate {gate_txt}."


def _analyst_desk_summary(
    *,
    ticker_intel: list[dict[str, Any]],
    regime_state: str,
    macro_gate: dict[str, Any],
    weighted_opportunity: float,
    weighted_distribution: float,
    weighted_event_risk: float,
) -> dict[str, Any]:
    no_catch = [
        str(row.get("ticker"))
        for row in ticker_intel
        if isinstance(row, dict) and row.get("entryDiscipline") in {"no-catch", "cheap-but-not-ready"}
    ][:4]
    confirm_candidates = [
        str(row.get("ticker"))
        for row in ticker_intel
        if isinstance(row, dict) and row.get("confirmationState") in {"failed-breakdown-watch", "reclaiming-resistance"}
    ][:4]
    market_read = "Stock-picker tape; broad beta is not enough."
    if regime_state == "stress":
        market_read = "Stress tape; confirmation matters more than valuation."
    elif regime_state == "overheated":
        market_read = "Crowded tape; strength should be treated as distribution liquidity."
    elif weighted_opportunity > weighted_distribution + 0.12:
        market_read = "Dislocation tape; opportunities exist, but only after support behavior confirms."

    next_watch = "Watch open/close behavior around support, not intraday noise."
    if weighted_event_risk >= 0.55:
        next_watch = "Watch whether headline risk cools before adding exposure."
    elif confirm_candidates:
        next_watch = f"Watch confirmation in {', '.join(confirm_candidates)}."

    return {
        "marketRead": market_read,
        "macroGate": macro_gate,
        "mainRisk": "Adding too early before technical confirmation." if no_catch else "Chasing signals without catalyst confirmation.",
        "mainOpportunity": "Forced or mechanical selling can create entries if support holds.",
        "nextThingToWatch": next_watch,
        "noCatchTickers": no_catch,
        "confirmationCandidates": confirm_candidates,
    }


def _regime_label(panic: float, crowding: float) -> str:
    if panic >= 0.68:
        return "stress"
    if crowding >= 0.68 and panic < 0.5:
        return "overheated"
    if panic <= 0.35 and crowding <= 0.45:
        return "calm"
    return "mixed"


def _regime_probabilities(panic: float, crowding: float, macro_shock: float, macro_relief: float) -> dict[str, float]:
    stress = _clamp01((panic * 0.72) + (macro_shock * 0.28))
    overheated = _clamp01((crowding * 0.7) + ((1 - panic) * 0.2) + ((1 - macro_shock) * 0.1))
    calm = _clamp01(((1 - panic) * 0.45) + ((1 - crowding) * 0.4) + (macro_relief * 0.15))
    transition = _clamp01(1.0 - max(stress, overheated, calm) + 0.12)
    values = {"stress": stress, "overheated": overheated, "calm": calm, "transition": transition}
    total = sum(values.values()) or 1.0
    return {k: round(v / total, 3) for k, v in values.items()}


def _expected_return_5d(opportunity: float, distribution: float, panic: float, event_risk: float) -> float:
    return (opportunity * 0.032) - (distribution * 0.024) - (panic * 0.014) - (event_risk * 0.01) + 0.002


def _expected_return_20d(opportunity: float, distribution: float, panic: float, event_risk: float, relief: float) -> float:
    return (opportunity * 0.085) - (distribution * 0.056) - (panic * 0.03) - (event_risk * 0.018) + (relief * 0.011) + 0.006


def _construct_portfolio_targets(ticker_intel: list[dict[str, Any]], regime_state: str) -> dict[str, Any]:
    if not ticker_intel:
        return {"targets": [], "projectedTop1": 0.0, "projectedTurnover": 0.0, "cashBuffer": 0.0}

    cash_buffer = 0.0
    if regime_state == "stress":
        cash_buffer = 0.15
    elif regime_state == "mixed":
        cash_buffer = 0.08
    elif regime_state == "overheated":
        cash_buffer = 0.05

    raw_rows: list[dict[str, float | str]] = []
    margins: list[float] = []
    for row in ticker_intel:
        ticker = row.get("ticker")
        weight = _as_float(row.get("weight"))
        alpha = _as_float(row.get("alphaScore"))
        opp = _as_float(row.get("opportunityIndex"))
        dist = _as_float(row.get("distributionIndex"))
        event = _as_float(row.get("eventRisk"))
        valuation = row.get("valuation")
        margin_safety = _as_float(valuation.get("marginSafety")) if isinstance(valuation, dict) else None
        features = row.get("features")
        quality = _as_float(features.get("qualityScore")) if isinstance(features, dict) else None
        if not isinstance(ticker, str) or weight is None:
            continue
        alpha = alpha or 0.0
        opp = opp or 0.0
        dist = dist or 0.0
        event = event or 0.0
        quality = quality or 0.5
        value_tilt = max(-0.2, min(0.35, margin_safety or 0.0))
        tilt = (alpha * 0.5) + ((opp - dist) * 0.26) + ((quality - 0.5) * 0.14) - (event * 0.2) + (value_tilt * 0.24)
        base = max(0.0, weight + (tilt * 0.14))
        if margin_safety is not None:
            margins.append(margin_safety)
        raw_rows.append({"ticker": ticker, "current": weight, "raw": base})

    if not raw_rows:
        return {"targets": [], "projectedTop1": 0.0, "projectedTurnover": 0.0, "cashBuffer": cash_buffer}

    budget = max(0.0, 1.0 - cash_buffer)
    raw_total = sum(float(row["raw"]) for row in raw_rows) or 1.0
    capped: list[dict[str, float | str]] = []
    if len(raw_rows) <= 2:
        cap = 0.62 if regime_state != "stress" else 0.5
    elif len(raw_rows) == 3:
        cap = 0.5 if regime_state != "stress" else 0.42
    else:
        cap = 0.38 if regime_state != "stress" else 0.32
    avg_margin = sum(margins) / len(margins) if margins else 0.0
    if regime_state in {"calm", "mixed"} and avg_margin >= 0.1:
        cap = min(0.7, cap + 0.05)
    for row in raw_rows:
        target = budget * (float(row["raw"]) / raw_total)
        capped.append({"ticker": row["ticker"], "current": row["current"], "target": min(cap, max(0.0, target))})

    capped_total = sum(float(row["target"]) for row in capped)
    if capped_total > 0:
        scale = budget / capped_total
        for row in capped:
            row["target"] = min(cap, float(row["target"]) * scale)

    turnover = 0.0
    targets: list[dict[str, Any]] = []
    for row in capped:
        current = float(row["current"])
        target = float(row["target"])
        delta = target - current
        turnover += abs(delta)
        targets.append(
            {
                "ticker": row["ticker"],
                "currentWeight": round(current, 4),
                "targetWeight": round(target, 4),
                "delta": round(delta, 4),
            }
        )
    targets.sort(key=lambda item: abs(float(item["delta"])), reverse=True)
    projected_top1 = max((float(row["targetWeight"]) for row in targets), default=0.0)
    return {
        "targets": targets,
        "projectedTop1": round(projected_top1, 4),
        "projectedTurnover": round(turnover, 4),
        "cashBuffer": round(cash_buffer, 4),
    }


def _action_book_from_targets(targets: list[dict[str, Any]], ticker_intel: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not targets:
        return []
    rationale_by_ticker = {
        str(row.get("ticker")).upper(): str(row.get("rationale") or "Model target adjustment.")
        for row in ticker_intel
        if isinstance(row, dict) and isinstance(row.get("ticker"), str)
    }
    out: list[dict[str, Any]] = []
    for row in targets:
        ticker = row.get("ticker")
        delta = _as_float(row.get("delta"))
        if not isinstance(ticker, str) or delta is None:
            continue
        if abs(delta) < 0.005:
            continue
        if delta >= 0.018:
            action = "accumulate"
            urgency = "high"
        elif delta > 0:
            action = "add"
            urgency = "medium"
        elif delta <= -0.02:
            action = "trim"
            urgency = "high"
        else:
            action = "reduce"
            urgency = "medium"
        out.append(
            {
                "ticker": ticker,
                "action": action,
                "targetWeightDelta": round(delta, 4),
                "urgency": urgency,
                "confidence": 0.62 if urgency == "medium" else 0.74,
                "reason": rationale_by_ticker.get(ticker.upper(), "Model target adjustment."),
            }
        )
    out.sort(key=lambda item: (item["urgency"] == "high", abs(item["targetWeightDelta"])), reverse=True)
    return out


def _alpha_book(ticker_intel: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    scored: list[dict[str, Any]] = []
    for row in ticker_intel:
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        score = _as_float(row.get("alphaScore"))
        conf = _as_float(row.get("confidence"))
        if not isinstance(ticker, str) or score is None:
            continue
        scored.append({"ticker": ticker, "score": round(score, 3), "confidence": round(conf or 0.5, 3)})
    scored.sort(key=lambda item: item["score"], reverse=True)
    longs = [row for row in scored if row["score"] > 0][:5]
    under = [row for row in reversed(scored) if row["score"] < 0][:5]
    return {"longBias": longs, "underweightBias": under}


def _portfolio_actions(ticker_intel: list[dict[str, Any]], regime_state: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in ticker_intel:
        ticker = row.get("ticker")
        if not isinstance(ticker, str):
            continue
        opp = row.get("opportunityIndex")
        dist = row.get("distributionIndex")
        conf = row.get("confidence")
        weight = row.get("weight")
        if not all(isinstance(v, (int, float)) for v in (opp, dist, conf, weight)):
            continue

        action = "hold"
        delta = 0.0
        urgency = "low"
        if opp >= 0.67 and conf >= 0.45 and dist <= 0.58:
            action = "accumulate"
            delta = min(0.035, max(0.008, (opp - dist) * 0.06))
            urgency = "medium" if opp < 0.78 else "high"
        elif dist >= 0.67 and conf >= 0.45:
            action = "trim"
            delta = -min(0.04, max(0.01, (dist - opp) * 0.065))
            urgency = "medium" if dist < 0.78 else "high"
        elif regime_state == "stress" and weight >= 0.18:
            action = "de-risk"
            delta = -min(0.03, max(0.008, weight * 0.08))
            urgency = "high"
        if action == "hold":
            continue
        reason = row.get("rationale") if isinstance(row.get("rationale"), str) else "No additional rationale."
        actions.append(
            {
                "ticker": ticker,
                "action": action,
                "targetWeightDelta": round(delta, 4),
                "urgency": urgency,
                "confidence": round(float(conf), 3),
                "reason": reason,
            }
        )

    actions.sort(key=lambda item: (item["urgency"] == "high", abs(item["targetWeightDelta"]), item["confidence"]), reverse=True)
    return actions


def _hedge_plan(regime_state: str, panic: float, event_risk: float) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if regime_state == "stress" or panic >= 0.72:
        out.append({"name": "Index Put Overlay", "priority": "high", "reason": "Downside regime probability is elevated."})
        out.append({"name": "Raise Cash Buffer", "priority": "high", "reason": "Reduce forced selling risk under shock moves."})
    if event_risk >= 0.62:
        out.append({"name": "Volatility Hedge", "priority": "medium", "reason": "Headline shock clustering is active."})
    if regime_state == "overheated":
        out.append({"name": "Call Overwrite", "priority": "medium", "reason": "Crowding regime suggests harvesting upside convexity."})
    if not out:
        out.append({"name": "No Hedge Adjustment", "priority": "low", "reason": "Regime and event risk are within normal range."})
    return out


def _valuation_intel(openbb: dict[str, Any], current_price: float | None) -> dict[str, Any]:
    if not isinstance(openbb, dict) or not isinstance(current_price, (int, float)) or current_price <= 0:
        return {
            "fairValue": None,
            "marginSafety": 0.0,
            "verdict": "unknown",
            "confidence": 0.0,
            "undervaluationScore": 0.5,
            "overvaluationScore": 0.5,
            "methods": [],
        }

    price = float(current_price)
    valuation = openbb.get("valuation", {}) if isinstance(openbb.get("valuation"), dict) else {}
    quality = openbb.get("quality", {}) if isinstance(openbb.get("quality"), dict) else {}
    analyst = openbb.get("analyst", {}) if isinstance(openbb.get("analyst"), dict) else {}
    fundamental = openbb.get("fundamental", {}) if isinstance(openbb.get("fundamental"), dict) else {}
    asset = openbb.get("asset", {}) if isinstance(openbb.get("asset"), dict) else {}

    if bool(asset.get("isEtf")) or bool(asset.get("isFund")):
        return {
            "fairValue": None,
            "marginSafety": None,
            "verdict": "unknown",
            "confidence": 0.0,
            "undervaluationScore": 0.5,
            "overvaluationScore": 0.5,
            "methods": [
                {
                    "name": "etf_nav_not_modeled",
                    "value": None,
                    "weight": 0.0,
                    "impliedUpside": None,
                }
            ],
        }

    pe = _as_float(valuation.get("pe"))
    pb = _as_float(valuation.get("pb"))
    ev_ebitda = _as_float(valuation.get("evEbitda"))
    fcf_yield = _as_float(valuation.get("fcfYield"))
    roe = _as_float(quality.get("roe"))
    gm = _as_float(quality.get("grossMargin"))
    d2e = _as_float(quality.get("debtToEquity"))
    target_price = _as_float(analyst.get("targetPrice"))
    recommendation = _as_float(analyst.get("recommendationMean"))
    eps_ttm = _as_float(fundamental.get("epsTtm"))
    book_value_per_share = _as_float(fundamental.get("bookValuePerShare"))
    eps_growth = _as_float(fundamental.get("earningsGrowth"))
    rev_growth = _as_float(fundamental.get("revenueGrowth"))
    growth = eps_growth if isinstance(eps_growth, (int, float)) else rev_growth
    growth = max(-0.08, min(0.35, float(growth))) if isinstance(growth, (int, float)) else None

    methods: list[dict[str, Any]] = []
    method_weights = 0.0
    weighted_value = 0.0
    valuation_inputs = sum(
        1
        for item in (pe, pb, ev_ebitda, fcf_yield, roe, gm, d2e, target_price, eps_ttm, book_value_per_share, growth)
        if isinstance(item, (int, float))
    )

    def add_method(name: str, value: float, weight: float, **extra: Any) -> None:
        nonlocal weighted_value, method_weights
        rel = value / price
        if not (0.25 <= rel <= 4.0):
            return
        methods.append(
            {
                "name": name,
                "value": round(value, 2),
                "weight": round(weight, 3),
                "impliedUpside": round(rel - 1, 4),
                **extra,
            }
        )
        weighted_value += value * weight
        method_weights += weight

    if target_price and target_price > 0:
        rel = target_price / price
        if 0.55 <= rel <= 1.8:
            add_method("analyst_consensus", target_price, 0.24)

    if fcf_yield and fcf_yield > 0:
        quality_adj = 0.0
        if isinstance(roe, (int, float)):
            quality_adj += max(-0.01, min(0.012, (float(roe) - 0.12) * 0.08))
        if isinstance(gm, (int, float)):
            quality_adj += max(-0.008, min(0.01, (float(gm) - 0.4) * 0.04))
        if isinstance(d2e, (int, float)):
            quality_adj -= max(-0.008, min(0.012, (float(d2e) - 1.0) * 0.02))
        fair_yield = max(0.035, min(0.085, 0.055 - quality_adj))
        fcf_value = price * (float(fcf_yield) / fair_yield)
        add_method("fcf_cap_model", fcf_value, 0.32, assumedFairYield=round(fair_yield, 4))

    if pe and pe > 0:
        fair_pe = 16.0 + ((growth if growth is not None else 0.08) * 38.0)
        if isinstance(roe, (int, float)):
            fair_pe += max(-2.0, min(4.0, (float(roe) - 0.12) * 22.0))
        fair_pe = max(11.0, min(42.0, fair_pe))
        pe_value = price * (fair_pe / float(pe))
        add_method("pe_relative", pe_value, 0.18, fairMultiple=round(fair_pe, 2))

    if ev_ebitda and ev_ebitda > 0:
        fair_ev = 11.0 + ((growth if growth is not None else 0.06) * 22.0)
        if isinstance(gm, (int, float)):
            fair_ev += max(-1.5, min(3.5, (float(gm) - 0.35) * 10.0))
        if isinstance(d2e, (int, float)):
            fair_ev -= max(-1.0, min(2.0, (float(d2e) - 1.0) * 1.6))
        fair_ev = max(7.0, min(28.0, fair_ev))
        ev_value = price * (fair_ev / float(ev_ebitda))
        add_method("ev_ebitda_relative", ev_value, 0.16, fairMultiple=round(fair_ev, 2))

    if pb and pb > 0 and isinstance(roe, (int, float)):
        fair_pb = max(1.2, min(12.0, float(roe) * 13.0))
        pb_value = price * (fair_pb / float(pb))
        add_method("pb_relative", pb_value, 0.1, fairMultiple=round(fair_pb, 2))

    if eps_ttm and eps_ttm > 0 and book_value_per_share and book_value_per_share > 0:
        graham_value = math.sqrt(max(0.0, 22.5 * float(eps_ttm) * float(book_value_per_share)))
        add_method("graham_number", graham_value, 0.08)

    if method_weights <= 0 or len(methods) < 2 or valuation_inputs < 3:
        return {
            "fairValue": None,
            "marginSafety": None,
            "verdict": "unknown",
            "confidence": 0.0,
            "undervaluationScore": 0.5,
            "overvaluationScore": 0.5,
            "methods": methods[:4] if methods else [{"name": "insufficient_valuation_inputs", "value": None, "weight": 0.0, "impliedUpside": None}],
        }

    method_values = sorted(float(method["value"]) for method in methods if isinstance(method.get("value"), (int, float)))
    weighted_fair_value = weighted_value / method_weights
    median_fair_value = method_values[len(method_values) // 2]
    fair_value = (weighted_fair_value * 0.65) + (median_fair_value * 0.35)
    margin_safety = (fair_value / price) - 1.0
    confidence = _clamp01(
        0.18
        + min(0.45, len(methods) * 0.12)
        + min(0.22, valuation_inputs * 0.03)
        + (0.05 if recommendation is not None else 0.0)
    )
    if margin_safety >= 0.15:
        verdict = "undervalued"
    elif margin_safety >= 0.08:
        verdict = "slightly-undervalued"
    elif margin_safety <= -0.15:
        verdict = "overvalued"
    elif margin_safety <= -0.08:
        verdict = "slightly-overvalued"
    else:
        verdict = "fair"

    undervaluation_score = _clamp01(((margin_safety + 0.02) / 0.35) * confidence + (1 - confidence) * 0.5)
    overvaluation_score = _clamp01(((-margin_safety + 0.02) / 0.35) * confidence + (1 - confidence) * 0.5)
    return {
        "fairValue": round(fair_value, 2),
        "marginSafety": round(margin_safety, 4),
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "undervaluationScore": round(undervaluation_score, 3),
        "overvaluationScore": round(overvaluation_score, 3),
        "methods": methods[:4],
    }


def _openbb_scores(openbb: dict[str, Any], valuation_intel: dict[str, Any] | None = None) -> dict[str, float]:
    if not isinstance(openbb, dict):
        return {"valuation": 0.5, "quality": 0.5, "flow": 0.5, "coverage": 0.0}

    valuation = openbb.get("valuation", {}) if isinstance(openbb.get("valuation"), dict) else {}
    quality = openbb.get("quality", {}) if isinstance(openbb.get("quality"), dict) else {}
    options = openbb.get("options", {}) if isinstance(openbb.get("options"), dict) else {}
    shorts = openbb.get("shorts", {}) if isinstance(openbb.get("shorts"), dict) else {}

    pe = _as_float(valuation.get("pe"))
    pb = _as_float(valuation.get("pb"))
    ev = _as_float(valuation.get("evEbitda"))
    fcf_yield = _as_float(valuation.get("fcfYield"))

    roe = _as_float(quality.get("roe"))
    gm = _as_float(quality.get("grossMargin"))
    d2e = _as_float(quality.get("debtToEquity"))

    pcr = _as_float(options.get("putCallRatio"))
    skew = _as_float(options.get("skew"))
    iv = _as_float(options.get("ivLevel"))
    short_interest = _as_float(shorts.get("shortInterestPct"))

    base_valuation_score = _clamp01(
        (max(0.0, 28 - (pe or 28)) / 28.0) * 0.28
        + (max(0.0, 4.5 - (pb or 4.5)) / 4.5) * 0.22
        + (max(0.0, 18 - (ev or 18)) / 18.0) * 0.2
        + ((max(-0.01, min(0.06, fcf_yield or 0.0)) + 0.01) / 0.07) * 0.3
    )
    valuation_score = base_valuation_score
    if isinstance(valuation_intel, dict):
        intrinsic_score = _as_float(valuation_intel.get("undervaluationScore"))
        intrinsic_conf = _as_float(valuation_intel.get("confidence"))
        if intrinsic_score is not None and intrinsic_conf is not None:
            w = max(0.0, min(0.7, intrinsic_conf * 0.6))
            valuation_score = _clamp01((base_valuation_score * (1 - w)) + (intrinsic_score * w))
    quality_score = _clamp01(
        ((max(-0.05, min(0.35, roe or 0.1)) + 0.05) / 0.4) * 0.4
        + ((max(0.05, min(0.75, gm or 0.4)) - 0.05) / 0.7) * 0.3
        + (max(0.0, 2.5 - (d2e or 1.2)) / 2.5) * 0.3
    )
    flow_score = _clamp01(
        (max(0.0, (pcr or 0.9) - 0.8) / 1.2) * 0.4
        + (max(0.0, (short_interest or 2.5) - 2.5) / 18.0) * 0.2
        + (max(0.0, (skew or 0.0) + 0.02) / 0.2) * 0.2
        + (max(0.0, (iv or 0.25) - 0.2) / 0.8) * 0.2
    )
    coverage = 0.0
    for value in (pe, pb, ev, fcf_yield, roe, gm, d2e, pcr, skew, iv, short_interest):
        if value is not None:
            coverage += 1
    coverage = min(1.0, coverage / 7.0)
    return {
        "valuation": valuation_score,
        "quality": quality_score,
        "flow": flow_score,
        "coverage": coverage,
    }


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _build_macro_context(
    macro: dict[str, MacroPoint],
    macro_news: list[Headline],
    positions: list[PositionAnalysis],
    macro_events: list[dict[str, Any]],
) -> dict[str, Any]:
    drivers = _macro_driver_rows(macro)
    release_highlights = _macro_release_readthrough(macro_events)
    headline_readthrough = _macro_event_readthrough(macro_news)
    merged_event_lens = [*release_highlights, *headline_readthrough]
    implications = _macro_portfolio_implications(drivers, merged_event_lens, positions)
    summary, regime_bias = _macro_context_summary(drivers, merged_event_lens, implications)
    return {
        "summary": summary,
        "regimeBias": regime_bias,
        "drivers": drivers[:6],
        "releaseHighlights": release_highlights[:4],
        "eventReadthrough": headline_readthrough[:4],
        "portfolioImplications": implications[:4],
    }


def _macro_driver_rows(macro: dict[str, MacroPoint]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _add_row(driver: str, point: MacroPoint, signal: str, meaning: str, playbook: str, confidence: float) -> None:
        move = "-"
        if isinstance(point.chg_bp_1d, float):
            move = f"{point.chg_bp_1d:+.1f}bp"
        elif isinstance(point.chg_pct_1d, float):
            move = f"{point.chg_pct_1d:+.2%}"
        reading = "-"
        if isinstance(point.value, float):
            reading = f"{point.value:.3f}"
        rows.append(
            {
                "driver": driver,
                "reading": reading,
                "move": move,
                "signal": signal,
                "confidence": round(_clamp01(confidence), 3),
                "meaning": meaning,
                "playbook": playbook,
            }
        )

    vix = macro.get("VIX")
    if vix and isinstance(vix.chg_pct_1d, float):
        chg = vix.chg_pct_1d
        if chg >= 0.05:
            _add_row(
                "VIX",
                vix,
                "risk-up",
                "Volatility demand jumped, signaling stronger demand for protection.",
                "Reduce position size and avoid adding leverage into uncertainty spikes.",
                0.55 + min(0.4, chg / 0.12),
            )
        elif chg >= 0.02:
            _add_row(
                "VIX",
                vix,
                "risk-up",
                "Volatility is rising and can pressure broad equity multiples.",
                "Keep entries selective and prefer names with cleaner event profiles.",
                0.5 + min(0.3, chg / 0.1),
            )
        elif chg <= -0.04:
            _add_row(
                "VIX",
                vix,
                "risk-down",
                "Volatility is compressing, a setup that usually supports risk appetite.",
                "Opportunity setups can be scaled gradually if other signals confirm.",
                0.5 + min(0.3, abs(chg) / 0.1),
            )
        else:
            _add_row(
                "VIX",
                vix,
                "neutral",
                "Volatility is stable with no acute fear impulse.",
                "Let single-name signals drive execution rather than macro volatility.",
                0.44,
            )

    us10y = macro.get("US10Y")
    if us10y and isinstance(us10y.chg_bp_1d, float):
        chg = us10y.chg_bp_1d
        if chg >= 6:
            _add_row(
                "US10Y",
                us10y,
                "risk-up",
                "Real yields moved higher, which can tighten valuation pressure on duration-sensitive stocks.",
                "Trim stretched growth exposure unless valuation support is strong.",
                0.56 + min(0.35, chg / 18.0),
            )
        elif chg <= -6:
            _add_row(
                "US10Y",
                us10y,
                "risk-down",
                "Yields eased materially, often a tailwind for long-duration equities.",
                "Bias toward quality growth where crowding risk remains controlled.",
                0.56 + min(0.35, abs(chg) / 18.0),
            )
        else:
            _add_row(
                "US10Y",
                us10y,
                "neutral",
                "Rates are range-bound, so macro discount-rate pressure is limited.",
                "Focus on company-level signals and headline risk dispersion.",
                0.45,
            )

    dxy = macro.get("DXY")
    if dxy and isinstance(dxy.chg_pct_1d, float):
        chg = dxy.chg_pct_1d
        if chg >= 0.004:
            _add_row(
                "DXY",
                dxy,
                "risk-up",
                "Dollar strength points to tighter global financial conditions.",
                "Favor resilient cash-flow names over highly levered beta.",
                0.54 + min(0.33, chg / 0.015),
            )
        elif chg <= -0.004:
            _add_row(
                "DXY",
                dxy,
                "risk-down",
                "Dollar softness eases global liquidity pressure and supports risk assets.",
                "Higher-beta expressions can work better when event risk is contained.",
                0.54 + min(0.33, abs(chg) / 0.015),
            )
        else:
            _add_row(
                "DXY",
                dxy,
                "neutral",
                "Dollar movement is muted and not a dominant macro impulse.",
                "Do not over-weight FX-driven narratives in this run.",
                0.43,
            )

    spy = macro.get("SPY")
    if spy and isinstance(spy.chg_pct_1d, float):
        chg = spy.chg_pct_1d
        if chg <= -0.008:
            _add_row(
                "SPY",
                spy,
                "risk-up",
                "Index-level tape is weak, which increases short-horizon drawdown risk.",
                "Tighten stop discipline and avoid chasing weak intraday rebounds.",
                0.52 + min(0.3, abs(chg) / 0.03),
            )
        elif chg >= 0.008:
            _add_row(
                "SPY",
                spy,
                "risk-down",
                "Broad index strength confirms better near-term risk appetite.",
                "Rotate toward names with both technical and valuation confirmation.",
                0.52 + min(0.3, chg / 0.03),
            )
        else:
            _add_row(
                "SPY",
                spy,
                "neutral",
                "Broad index tape is mixed and not providing a strong directional signal.",
                "Keep gross exposure close to base risk budget.",
                0.42,
            )

    gld = macro.get("GLD")
    if gld and isinstance(gld.chg_pct_1d, float):
        chg = gld.chg_pct_1d
        if chg >= 0.01:
            _add_row(
                "GLD",
                gld,
                "risk-up",
                "Gold bid suggests demand for macro hedge assets is increasing.",
                "Respect tail-risk hedges and keep optionality in the book.",
                0.48 + min(0.3, chg / 0.03),
            )
        elif chg <= -0.01:
            _add_row(
                "GLD",
                gld,
                "risk-down",
                "Gold softness points to lower immediate stress demand.",
                "Hedge intensity can be lighter if event flow remains calm.",
                0.48 + min(0.3, abs(chg) / 0.03),
            )
        else:
            _add_row(
                "GLD",
                gld,
                "neutral",
                "Gold is not signaling a large shift in macro stress pricing.",
                "Use portfolio-specific risk signals as primary execution guide.",
                0.4,
            )

    rows.sort(key=lambda row: (row["signal"] == "risk-up", row["confidence"]), reverse=True)
    return rows


def _macro_release_readthrough(macro_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in macro_events[:60]:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or "").strip()
        if not event:
            continue
        theme = _macro_event_theme(event)
        if not theme:
            continue
        event_key = event.lower()
        if event_key in seen:
            continue
        seen.add(event_key)
        actual = _as_float(row.get("actual"))
        forecast = _as_float(row.get("forecast"))
        previous = _as_float(row.get("previous"))
        surprise = None
        surprise_pct = None
        signal = "neutral"
        if actual is not None and forecast is not None:
            surprise = actual - forecast
            denom = max(abs(forecast), abs(previous or 0.0), 1e-6)
            surprise_pct = surprise / denom
            polarity = _macro_release_polarity(theme, event)
            if polarity > 0:
                signal = "risk-up" if surprise > 0 else "risk-down" if surprise < 0 else "neutral"
            elif polarity < 0:
                signal = "risk-up" if surprise < 0 else "risk-down" if surprise > 0 else "neutral"
            else:
                signal = "risk-up" if (surprise_pct or 0.0) > 0.12 else "risk-down" if (surprise_pct or 0.0) < -0.12 else "neutral"
        importance = int(row.get("importance") or 2)
        out.append(
            {
                "kind": "release",
                "theme": theme.replace("-", " ").title(),
                "event": event,
                "country": str(row.get("country") or "Unknown"),
                "date": row.get("date"),
                "signal": signal,
                "impact": "high" if importance >= 3 else "medium" if importance == 2 else "low",
                "importance": importance,
                "actual": actual,
                "forecast": forecast,
                "previous": previous,
                "actualText": row.get("actualText"),
                "forecastText": row.get("forecastText"),
                "previousText": row.get("previousText"),
                "surprise": surprise,
                "surprisePct": surprise_pct,
                "meaning": _macro_release_meaning(theme, signal, event, surprise, surprise_pct),
            }
        )
    out.sort(
        key=lambda row: (
            bool(row.get("surprise") is not None),
            int(row.get("importance") or 1),
            _release_recency_score(row.get("date")),
        ),
        reverse=True,
    )
    return out


def _release_recency_score(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    dt = _parse_datetime(value)
    if dt is None:
        return 0.0
    now = datetime.now(tz=UTC)
    age_hours = (now - dt).total_seconds() / 3600.0
    if age_hours < -2:
        return 0.25  # upcoming release
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.8
    if age_hours <= 168:
        return 0.55
    return 0.3


def _macro_release_polarity(theme: str, event: str) -> int:
    lower = event.lower()
    if theme == "inflation":
        return 1
    if theme == "policy":
        return 1
    if theme == "growth":
        return -1
    if theme == "labor":
        if "unemployment" in lower or "jobless" in lower or "claims" in lower:
            return 1
        return -1
    if theme == "energy":
        return 0
    return 0


def _macro_release_meaning(theme: str, signal: str, event: str, surprise: float | None, surprise_pct: float | None) -> str:
    delta_text = ""
    if surprise is not None:
        delta_text = f" Surprise vs forecast: {surprise:+.3f}."
    elif surprise_pct is not None:
        delta_text = f" Surprise magnitude: {surprise_pct:+.1%}."

    if theme == "inflation":
        if signal == "risk-up":
            return f"{event} printed hotter than expected.{delta_text} This can keep policy restrictive and pressure equity multiples."
        if signal == "risk-down":
            return f"{event} printed cooler than expected.{delta_text} This can ease rate pressure and support duration-sensitive assets."
    if theme == "labor":
        if signal == "risk-up":
            return f"{event} skewed risk-up versus consensus.{delta_text} Labor surprise increases policy-path uncertainty."
        if signal == "risk-down":
            return f"{event} skewed risk-down versus consensus.{delta_text} Labor trend is less destabilizing for near-term risk."
    if theme == "growth":
        if signal == "risk-up":
            return f"{event} surprised weaker versus expectations.{delta_text} Growth-sensitive risk appetite may soften."
        if signal == "risk-down":
            return f"{event} surprised stronger versus expectations.{delta_text} Cyclical risk sentiment can improve."
    if theme == "policy":
        if signal == "risk-up":
            return f"{event} is interpreted as hawkish versus expectation.{delta_text} Liquidity conditions may tighten."
        if signal == "risk-down":
            return f"{event} is interpreted as dovish versus expectation.{delta_text} Discount-rate pressure may ease."
    if theme == "energy":
        if signal == "risk-up":
            return f"{event} points to inflation-sensitive energy pressure.{delta_text} Watch inflation pass-through."
        if signal == "risk-down":
            return f"{event} indicates softer energy pressure.{delta_text} Inflation impulse may moderate."
    return f"{event} is mixed against consensus.{delta_text} Wait for cross-asset confirmation before changing risk posture."


def _macro_event_readthrough(macro_news: list[Headline]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_themes: set[str] = set()
    for item in macro_news[:10]:
        theme = _macro_event_theme(item.title)
        if not theme or theme in seen_themes:
            continue
        impact, direction, _ = _classify_headline(item.title, sentiment_hint=item.sentiment_hint)
        seen_themes.add(theme)
        out.append(
            {
                "kind": "headline",
                "theme": theme.replace("-", " ").title(),
                "signal": direction,
                "impact": impact,
                "headline": item.title,
                "event": item.title,
                "meaning": _macro_event_meaning(theme, direction),
            }
        )
    out.sort(key=lambda row: (_impact_score(str(row.get("impact"))), row.get("signal") == "risk-up"), reverse=True)
    return out


def _macro_event_theme(title: str) -> str | None:
    lower = title.lower()
    for theme, keywords in _MACRO_EVENT_KEYWORDS.items():
        if any(token in lower for token in keywords):
            return theme
    return None


def _macro_event_meaning(theme: str, direction: str) -> str:
    if theme == "inflation":
        if direction == "risk-up":
            return "Hotter inflation narratives can keep policy tighter, lifting yields and pressuring high-duration equities."
        if direction == "risk-down":
            return "Cooling inflation narratives can support duration assets and ease discount-rate pressure."
        return "Inflation headlines are mixed; policy path remains data dependent."
    if theme == "labor":
        if direction == "risk-up":
            return "Labor surprises can shift rate-cut expectations and increase policy uncertainty."
        if direction == "risk-down":
            return "Balanced labor data can reduce policy shock risk and stabilize equity beta."
        return "Labor signals are mixed; monitor claims and payroll trend consistency."
    if theme == "growth":
        if direction == "risk-up":
            return "Growth slowdown signals can weaken cyclical sentiment and raise downside skew."
        if direction == "risk-down":
            return "Growth stabilization supports cyclical participation and risk-taking."
        return "Growth prints are mixed; cross-check with forward earnings revisions."
    if theme == "policy":
        if direction == "risk-up":
            return "Hawkish policy signals can tighten liquidity and compress valuation multiples."
        if direction == "risk-down":
            return "Dovish policy signals can ease liquidity stress and support multiple expansion."
        return "Policy communication is mixed; wait for confirmation from rates and dollar."
    if theme == "energy":
        if direction == "risk-up":
            return "Energy shocks can lift inflation expectations and push rates volatility higher."
        if direction == "risk-down":
            return "Energy easing can reduce inflation pressure and help risk sentiment."
        return "Energy signals are mixed; focus on pass-through into inflation and margins."
    return "Macro event impact is mixed; monitor cross-asset confirmation."


def _macro_portfolio_implications(
    drivers: list[dict[str, Any]],
    event_readthrough: list[dict[str, Any]],
    positions: list[PositionAnalysis],
) -> list[str]:
    by_driver = {str(row.get("driver")): row for row in drivers if isinstance(row.get("driver"), str)}
    weights = _portfolio_bucket_weights(positions)
    out: list[str] = []

    if positions:
        top = positions[0]
        if top.weight >= 0.45:
            out.append(f"Portfolio concentration is high in {top.ticker} ({top.weight:.0%}); macro shocks can transmit quickly.")

    vix_row = by_driver.get("VIX")
    if isinstance(vix_row, dict) and vix_row.get("signal") == "risk-up":
        out.append("Volatility regime is firming; prioritize incremental entries over full-size adds.")

    rates_row = by_driver.get("US10Y")
    if isinstance(rates_row, dict) and rates_row.get("signal") == "risk-up" and weights["tech"] >= 0.3:
        out.append("Rising yields with tech-heavy exposure raises multiple-compression risk for growth holdings.")
    elif isinstance(rates_row, dict) and rates_row.get("signal") == "risk-down" and weights["tech"] >= 0.3:
        out.append("Falling yields improve setup for quality growth exposure if headline risk stays contained.")

    dxy_row = by_driver.get("DXY")
    if isinstance(dxy_row, dict) and dxy_row.get("signal") == "risk-up":
        out.append("Dollar strength can tighten global liquidity; keep cyclical beta sized conservatively.")

    policy_or_inflation_risk = any(
        isinstance(row, dict)
        and row.get("signal") == "risk-up"
        and str(row.get("theme")).lower() in {"inflation", "policy"}
        for row in event_readthrough
    )
    if policy_or_inflation_risk:
        out.append("Inflation/policy event flow is still hot; rate-sensitive equities need stronger valuation support.")

    high_importance_release = next(
        (
            row
            for row in event_readthrough
            if isinstance(row, dict) and row.get("kind") == "release" and int(row.get("importance") or 0) >= 3 and row.get("signal") in {"risk-up", "risk-down"}
        ),
        None,
    )
    if isinstance(high_importance_release, dict):
        event_name = str(high_importance_release.get("event") or "High-impact release")
        event_signal = str(high_importance_release.get("signal") or "neutral")
        if event_signal == "risk-up":
            out.append(f"{event_name} was a high-impact risk-up release; keep gross risk tighter until follow-through confirms stability.")
        elif event_signal == "risk-down":
            out.append(f"{event_name} was a high-impact risk-down release; tactical adds can be considered if single-name setup aligns.")

    if not out:
        out.append("Macro impulse is balanced in this run; single-name alpha and valuation dispersion matter more than top-down risk.")
    return out


def _portfolio_bucket_weights(positions: list[PositionAnalysis]) -> dict[str, float]:
    tech = 0.0
    financials = 0.0
    defensive = 0.0
    broad = 0.0
    for row in positions:
        ticker = row.ticker.upper()
        if ticker in _TECH_TICKERS:
            tech += row.weight
        if ticker in _FINANCIAL_TICKERS:
            financials += row.weight
        if ticker in _DEFENSIVE_TICKERS:
            defensive += row.weight
        if ticker in {"SPY", "VOO", "IVV", "VTI", "QQQ"}:
            broad += row.weight
    return {
        "tech": _clamp01(tech),
        "financials": _clamp01(financials),
        "defensive": _clamp01(defensive),
        "broad": _clamp01(broad),
    }


def _macro_context_summary(
    drivers: list[dict[str, Any]],
    event_readthrough: list[dict[str, Any]],
    implications: list[str],
) -> tuple[str, str]:
    risk_up = sum(float(row.get("confidence") or 0.0) for row in drivers if row.get("signal") == "risk-up")
    risk_down = sum(float(row.get("confidence") or 0.0) for row in drivers if row.get("signal") == "risk-down")
    regime_bias = "balanced"
    if risk_up > risk_down + 0.25:
        regime_bias = "risk-up"
    elif risk_down > risk_up + 0.25:
        regime_bias = "risk-down"

    active_drivers = [str(row.get("driver")) for row in drivers if row.get("signal") != "neutral"][:3]
    active_themes = [str(row.get("theme")) for row in event_readthrough[:2]]
    active_releases = [
        str(row.get("event"))
        for row in event_readthrough
        if isinstance(row, dict) and row.get("kind") == "release" and isinstance(row.get("event"), str)
    ][:2]

    if regime_bias == "risk-up":
        summary = "Macro readthrough is risk-up with tighter cross-asset conditions."
    elif regime_bias == "risk-down":
        summary = "Macro readthrough is supportive with improving risk appetite signals."
    else:
        summary = "Macro readthrough is balanced; no single top-down regime dominates."

    if active_drivers:
        summary += f" Key drivers: {', '.join(active_drivers)}."
    if active_themes:
        summary += f" Event lens: {', '.join(active_themes)}."
    if active_releases:
        summary += f" Release lens: {', '.join(active_releases)}."
    if implications:
        summary += f" Portfolio focus: {implications[0]}"
    return summary[:420], regime_bias


def _build_pulse(
    warnings: list[dict[str, str]],
    scenarios: list[dict[str, Any]],
    behavioral: dict[str, Any],
    radar: list[dict[str, Any]],
    macro: dict[str, MacroPoint],
    risk: dict[str, float | None],
) -> dict[str, Any]:
    worst = min(scenarios, key=lambda row: row.get("portfolioImpactPct", 0.0)) if scenarios else None
    worst_impact = _as_float(worst.get("portfolioImpactPct")) if isinstance(worst, dict) else 0.0
    worst_impact = worst_impact if worst_impact is not None else 0.0
    high_alerts = sum(1 for w in warnings if w.get("severity") == "high")

    regime = behavioral.get("regime", {}) if isinstance(behavioral, dict) else {}
    regime_state = str(regime.get("state") or "mixed")
    regime_panic = _as_float(regime.get("panicScore")) or 0.0

    predictions = behavioral.get("predictions", {}) if isinstance(behavioral, dict) else {}
    horizon5d = predictions.get("horizon5d") if isinstance(predictions, dict) else {}
    downside_5d = _as_float(horizon5d.get("downsideProb")) if isinstance(horizon5d, dict) else None
    upside_5d = _as_float(horizon5d.get("upsideProb")) if isinstance(horizon5d, dict) else None
    confidence = _as_float(predictions.get("confidence")) if isinstance(predictions, dict) else None
    confidence = confidence if confidence is not None else 0.0

    technical_summary = behavioral.get("technicalSummary", {}) if isinstance(behavioral, dict) else {}
    tech_bearish = _as_float(technical_summary.get("bearishShare")) if isinstance(technical_summary, dict) else None
    tech_bullish = _as_float(technical_summary.get("bullishShare")) if isinstance(technical_summary, dict) else None
    tech_oversold = _as_float(technical_summary.get("oversoldShare")) if isinstance(technical_summary, dict) else None
    tech_overbought = _as_float(technical_summary.get("overboughtShare")) if isinstance(technical_summary, dict) else None

    risk_up_high = sum(1 for row in radar if row.get("direction") == "risk-up" and row.get("impact") == "high")
    risk_down_high = sum(1 for row in radar if row.get("direction") == "risk-down" and row.get("impact") == "high")

    stance = "balanced"
    if (
        high_alerts >= 2
        or regime_state == "stress"
        or (downside_5d is not None and downside_5d >= 0.58)
        or worst_impact <= -0.01
        or risk_up_high >= 2
    ):
        stance = "risk-off"
    elif (
        high_alerts == 0
        and regime_state in {"calm", "mixed"}
        and (upside_5d is not None and downside_5d is not None and upside_5d - downside_5d >= 0.08)
        and worst_impact >= -0.004
        and risk_down_high >= risk_up_high
    ):
        stance = "risk-on"

    macro_tape = _macro_tape_line(macro)
    event_tape = _event_tape_line(risk_up_high, risk_down_high, len(radar))
    positioning_tape = _positioning_tape_line(behavioral, tech_bullish, tech_bearish, tech_oversold, tech_overbought)

    if worst:
        scenario_name = str(worst.get("name") or "stress case")
        scenario_tape = f"Largest modeled stress remains {scenario_name} ({worst_impact:.2%})."
    else:
        scenario_tape = "Scenario engine is neutral with no dominant tail shock."

    thesis = f"{macro_tape} {scenario_tape}"
    if stance == "risk-off":
        thesis = f"{scenario_tape} {event_tape}"
    elif stance == "risk-on":
        thesis = f"{positioning_tape} {scenario_tape}"

    focus = [w["title"] for w in warnings if isinstance(w.get("title"), str)][:2]
    for theme in _dominant_themes_from_radar(radar):
        if theme not in focus:
            focus.append(theme)
        if len(focus) >= 3:
            break

    playbook = _pulse_playbook(behavioral, stance)
    drivers = _top_signal_drivers(warnings, behavioral, risk, macro)
    desk_note = " ".join(part for part in (macro_tape, event_tape, positioning_tape, scenario_tape) if part)
    return {
        "thesis": thesis[:220],
        "stance": stance,
        "focus": focus[:3],
        "deskNote": desk_note[:520],
        "macroTape": macro_tape,
        "eventTape": event_tape,
        "positioningTape": positioning_tape,
        "playbook": playbook,
        "signalDrivers": drivers,
        "confidence": round(_clamp01(confidence * 0.85 + 0.15), 3),
        "regimeState": regime_state,
        "regimePanic": round(regime_panic, 3),
    }


def _macro_tape_line(macro: dict[str, MacroPoint]) -> str:
    vix = macro.get("VIX")
    us10y = macro.get("US10Y")
    dxy = macro.get("DXY")
    spy = macro.get("SPY")
    segments: list[str] = []
    if vix and isinstance(vix.chg_pct_1d, float):
        segments.append(f"VIX {vix.chg_pct_1d:+.1%}")
    if us10y and isinstance(us10y.chg_bp_1d, float):
        segments.append(f"UST10Y {us10y.chg_bp_1d:+.1f}bp")
    if dxy and isinstance(dxy.chg_pct_1d, float):
        segments.append(f"DXY {dxy.chg_pct_1d:+.1%}")
    if spy and isinstance(spy.chg_pct_1d, float):
        segments.append(f"SPY {spy.chg_pct_1d:+.1%}")
    if not segments:
        return "Macro tape is mixed with incomplete cross-asset confirmation."
    return f"Macro tape: {' · '.join(segments[:4])}."


def _event_tape_line(risk_up_high: int, risk_down_high: int, radar_count: int) -> str:
    if radar_count == 0:
        return "Headline tape is thin across configured feeds."
    if risk_up_high >= 2:
        return f"Event tape is risk-up with {risk_up_high} high-impact negative catalysts."
    if risk_down_high >= 2:
        return f"Event tape is supportive with {risk_down_high} high-impact risk-down catalysts."
    return f"Event tape is balanced with {radar_count} scored headlines."


def _positioning_tape_line(
    behavioral: dict[str, Any],
    tech_bullish: float | None,
    tech_bearish: float | None,
    tech_oversold: float | None,
    tech_overbought: float | None,
) -> str:
    opportunities = behavioral.get("opportunities", []) if isinstance(behavioral, dict) else []
    exits = behavioral.get("exitSignals", []) if isinstance(behavioral, dict) else []
    top_opp = opportunities[0] if isinstance(opportunities, list) and opportunities and isinstance(opportunities[0], dict) else None
    top_exit = exits[0] if isinstance(exits, list) and exits and isinstance(exits[0], dict) else None

    if isinstance(top_opp, dict) and isinstance(top_opp.get("ticker"), str):
        ticker = str(top_opp["ticker"])
        reason = str(top_opp.get("signal") or "dislocation")
        base = f"Positioning favors selective accumulation in {ticker} ({reason})."
    elif isinstance(top_exit, dict) and isinstance(top_exit.get("ticker"), str):
        ticker = str(top_exit["ticker"])
        reason = str(top_exit.get("signal") or "distribution")
        base = f"Positioning favors trimming strength in {ticker} ({reason})."
    else:
        base = "Positioning is neutral with no dominant single-name signal."

    tech_tail = ""
    if tech_bullish is not None and tech_bearish is not None:
        tech_tail = f" Technical breadth bull/bear: {tech_bullish:.0%}/{tech_bearish:.0%}."
    if tech_oversold is not None and tech_overbought is not None:
        tech_tail += f" Oversold/overbought pressure: {tech_oversold:.0%}/{tech_overbought:.0%}."
    return f"{base}{tech_tail}"


def _pulse_playbook(behavioral: dict[str, Any], stance: str) -> list[str]:
    out: list[str] = []
    actions = behavioral.get("portfolioActions", []) if isinstance(behavioral, dict) else []
    if isinstance(actions, list):
        for row in actions[:4]:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            action = row.get("action")
            if isinstance(ticker, str) and isinstance(action, str):
                out.append(f"{action} {ticker}")
            if len(out) >= 3:
                break
    if not out:
        if stance == "risk-off":
            out.append("Reduce concentration and raise hedge coverage")
        elif stance == "risk-on":
            out.append("Scale into high-conviction names on controlled pullbacks")
        else:
            out.append("Hold neutral beta and rotate only where conviction is high")
    return out[:3]


def _top_signal_drivers(
    warnings: list[dict[str, str]],
    behavioral: dict[str, Any],
    risk: dict[str, float | None],
    macro: dict[str, MacroPoint],
) -> list[dict[str, Any]]:
    drivers: list[dict[str, Any]] = []
    for row in warnings[:3]:
        title = row.get("title")
        reason = row.get("reason")
        severity = row.get("severity")
        if isinstance(title, str) and title:
            drivers.append(
                {
                    "label": title,
                    "severity": severity if severity in {"low", "medium", "high"} else "medium",
                    "detail": reason if isinstance(reason, str) else "",
                }
            )

    predictions = behavioral.get("predictions", {}) if isinstance(behavioral, dict) else {}
    h5 = predictions.get("horizon5d") if isinstance(predictions, dict) else None
    downside = _as_float(h5.get("downsideProb")) if isinstance(h5, dict) else None
    if downside is not None:
        drivers.append(
            {
                "label": "5D Downside Probability",
                "severity": "high" if downside >= 0.62 else "medium" if downside >= 0.5 else "low",
                "detail": f"Model-implied downside probability is {downside:.0%}.",
            }
        )

    vol = risk.get("vol60d")
    if isinstance(vol, float):
        drivers.append(
            {
                "label": "Realized Volatility",
                "severity": "high" if vol >= 0.3 else "medium" if vol >= 0.22 else "low",
                "detail": f"Portfolio 60d annualized volatility is {vol:.0%}.",
            }
        )

    vix = macro.get("VIX")
    if vix and isinstance(vix.chg_pct_1d, float):
        drivers.append(
            {
                "label": "Volatility Regime Shift",
                "severity": "high" if vix.chg_pct_1d >= 0.06 else "medium" if vix.chg_pct_1d >= 0.03 else "low",
                "detail": f"VIX moved {vix.chg_pct_1d:+.1%} versus prior close.",
            }
        )
    return drivers[:5]


def _dominant_themes_from_radar(radar: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for row in radar[:12]:
        title = row.get("title")
        if not isinstance(title, str):
            continue
        for theme in _headline_themes(title):
            counts[theme] = counts.get(theme, 0) + 1
    return [theme.replace("-", " ").title() for theme, _ in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:3]]


def _build_theme_board(radar: list[dict[str, Any]], behavioral: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, float]] = {}
    for row in radar[:14]:
        title = row.get("title")
        if not isinstance(title, str):
            continue
        weight = 1.35 if row.get("impact") == "high" else 1.0
        direction = row.get("direction")
        for theme in _headline_themes(title):
            if theme not in counts:
                counts[theme] = {"weight": 0.0, "riskUp": 0.0, "riskDown": 0.0}
            counts[theme]["weight"] += weight
            if direction == "risk-up":
                counts[theme]["riskUp"] += weight
            elif direction == "risk-down":
                counts[theme]["riskDown"] += weight

    ticker_intel = behavioral.get("tickerIntel", []) if isinstance(behavioral, dict) else []
    if isinstance(ticker_intel, list):
        for row in ticker_intel:
            if not isinstance(row, dict):
                continue
            base_weight = _as_float(row.get("weight")) or 0.0
            themes = row.get("themes")
            if not isinstance(themes, list):
                continue
            for theme in themes:
                if not isinstance(theme, str):
                    continue
                if theme not in counts:
                    counts[theme] = {"weight": 0.0, "riskUp": 0.0, "riskDown": 0.0}
                counts[theme]["weight"] += 0.4 + base_weight

    board: list[dict[str, Any]] = []
    for theme, stats in counts.items():
        risk_up = stats["riskUp"]
        risk_down = stats["riskDown"]
        direction = "neutral"
        if risk_up > risk_down * 1.15:
            direction = "risk-up"
        elif risk_down > risk_up * 1.15:
            direction = "risk-down"
        board.append(
            {
                "theme": theme.replace("-", " ").title(),
                "intensity": round(_clamp01(stats["weight"] / 5.0), 3),
                "direction": direction,
                "confidence": round(_clamp01(0.25 + min(0.6, stats["weight"] / 7.5)), 3),
            }
        )
    board.sort(key=lambda row: (row["intensity"], row["confidence"]), reverse=True)
    return board[:6]


def _technical_summary(ticker_intel: list[dict[str, Any]], tracked_count: int) -> dict[str, Any]:
    if not ticker_intel or tracked_count <= 0:
        return {"coverage": 0.0, "bullishShare": 0.0, "bearishShare": 0.0, "oversoldShare": 0.0, "overboughtShare": 0.0}

    covered = 0
    bullish = 0.0
    bearish = 0.0
    oversold = 0.0
    overbought = 0.0
    for row in ticker_intel:
        technical = row.get("technical")
        if not isinstance(technical, dict):
            continue
        covered += 1
        trend = _as_float(technical.get("trendScore")) or 0.5
        bullish += max(0.0, trend - 0.5) * 2.0
        bearish += max(0.0, 0.5 - trend) * 2.0
        oversold += _as_float(technical.get("oversoldScore")) or 0.0
        overbought += _as_float(technical.get("overboughtScore")) or 0.0
    if covered <= 0:
        return {"coverage": 0.0, "bullishShare": 0.0, "bearishShare": 0.0, "oversoldShare": 0.0, "overboughtShare": 0.0}
    return {
        "coverage": round(covered / tracked_count, 3),
        "bullishShare": round(_clamp01(bullish / covered), 3),
        "bearishShare": round(_clamp01(bearish / covered), 3),
        "oversoldShare": round(_clamp01(oversold / covered), 3),
        "overboughtShare": round(_clamp01(overbought / covered), 3),
    }


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
