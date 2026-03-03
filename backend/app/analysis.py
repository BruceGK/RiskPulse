from __future__ import annotations

import asyncio
import math
import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from statistics import mean
from typing import Any

from app.config import Settings
from app.models import AnalysisRequest, AnalysisResponse, Headline, MacroPoint, PositionAnalysis
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
            }
            notes: list[str] = []
            if top5_weight >= 0.7:
                notes.append(f"Concentration high: top5 {top5_weight:.0%}")
            if missing_quotes:
                ticker_list = ", ".join(sorted(set(missing_quotes)))
                notes.append(f"Missing market data for: {ticker_list}")
            notes.append("Quick pass loaded. Deep analysis, news, and AI signals are still loading.")
            data_quality = self._build_data_quality(req, position_rows, macro, news)
            signals = self._build_signals(position_rows, notes, news, risk, macro, behavioral)
            meta_payload: dict = {
                "providers": {
                    "polygon_enabled": bool(self.settings.polygon_api_key),
                    "fred_enabled": bool(self.settings.fred_api_key),
                    "newsapi_enabled": bool(self.settings.newsapi_api_key),
                    "fmp_enabled": bool(self.settings.fmp_api_key),
                    "alpha_vantage_enabled": bool(self.settings.alpha_vantage_api_key),
                    "openbb_enabled": bool(self.settings.openbb_base_url),
                    "yahoo_enabled": True,
                    "openai_enabled": bool(self.settings.openai_api_key),
                },
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

        risk = await self._compute_risk(position_rows)
        news = await self._build_news_payload(position_rows)
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
        deterministic_signals = self._build_signals(position_rows, notes, news, risk, macro, behavioral)
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
                "openbb_enabled": bool(self.settings.openbb_base_url),
                "yahoo_enabled": True,
                "openai_enabled": bool(self.settings.openai_api_key),
            },
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
        for p, series, openbb_raw in zip(tracked, histories, openbb_rows, strict=False):
            openbb = openbb_raw if isinstance(openbb_raw, dict) else {}
            metrics = _price_metrics(series)
            returns_count = int(metrics.get("returnsCount", 0))
            ticker_news = news.get(p.ticker, [])[: self.settings.ticker_news_per_symbol]
            news_stats = _ticker_news_stats(ticker_news)
            valuation_intel = _valuation_intel(openbb, p.price)
            openbb_scores = _openbb_scores(openbb, valuation_intel)

            ret20 = metrics.get("ret20")
            spy_ret20 = spy_metrics.get("ret20")
            relative_20 = (ret20 - spy_ret20) if ret20 is not None and spy_ret20 is not None else None
            location = metrics.get("rangeLoc")
            drawdown = metrics.get("drawdown120") or 0.0
            ret5 = metrics.get("ret5") or 0.0
            vol_ratio = metrics.get("volRatio") or 1.0

            oversold = _clamp01(
                (max(0.0, -ret5 - 0.015) / 0.08) * 0.25
                + (max(0.0, -(ret20 or 0.0) - 0.04) / 0.16) * 0.25
                + (max(0.0, 0.35 - (location if location is not None else 0.5)) / 0.35) * 0.2
                + (max(0.0, drawdown - 0.1) / 0.2) * 0.15
                + (max(0.0, news_stats["riskUpShare"] - 0.55) / 0.45) * 0.15
            )
            overheated = _clamp01(
                (max(0.0, ret5 - 0.018) / 0.08) * 0.25
                + (max(0.0, (ret20 or 0.0) - 0.04) / 0.16) * 0.25
                + (max(0.0, (location if location is not None else 0.5) - 0.7) / 0.3) * 0.2
                + (max(0.0, news_stats["riskDownShare"] - 0.55) / 0.45) * 0.15
                + (max(0.0, news_stats["buzz"] - 0.5) / 0.5) * 0.15
            )

            vol_spike_factor = min(1.0, max(0.0, vol_ratio - 0.8))
            panic_score = _clamp01((oversold * 0.5) + (vol_spike_factor * 0.2) + (macro_panic * 0.2) + (news_stats["eventRisk"] * 0.1))
            crowding_score = _clamp01((overheated * 0.55) + (news_stats["buzz"] * 0.15) + (p.weight * 0.15) + (openbb_scores["flow"] * 0.15))
            opportunity_index = _clamp01(
                (panic_score * 0.45)
                + (max(0.0, 0.55 - crowding_score) * 0.25)
                + (valuation_intel["undervaluationScore"] * 0.2)
                + (openbb_scores["quality"] * 0.1)
            )
            distribution_index = _clamp01(
                (crowding_score * 0.45)
                + (max(0.0, 0.45 - panic_score) * 0.2)
                + (openbb_scores["flow"] * 0.2)
                + (valuation_intel["overvaluationScore"] * 0.15)
            )
            confidence = _signal_confidence(returns_count, len(ticker_news), relative_20)
            confidence = _clamp01(confidence + (0.08 if openbb_scores["coverage"] > 0 else 0.0))

            action_bias = _action_bias(
                opportunity_index,
                distribution_index,
                macro_panic,
                news_stats["eventRisk"],
                valuation_intel["marginSafety"],
            )
            rationale = _action_rationale(
                action_bias=action_bias,
                ret5=ret5,
                ret20=ret20,
                drawdown=drawdown,
                location=location,
                risk_up_share=news_stats["riskUpShare"],
                risk_down_share=news_stats["riskDownShare"],
                relative_20=relative_20,
                margin_safety=valuation_intel["marginSafety"],
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
                },
            }
            ticker_intel.append(intel_row)

            if (
                (opportunity_index >= 0.67 and confidence >= 0.45)
                or (valuation_intel["marginSafety"] >= 0.12 and valuation_intel["confidence"] >= 0.4 and news_stats["eventRisk"] < 0.75)
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
                        "signal": "intrinsic-undervaluation" if valuation_intel["marginSafety"] >= 0.12 else "undervaluation-window",
                        "reason": opp_reason,
                    }
                )
            if (
                (distribution_index >= 0.67 and confidence >= 0.45)
                or (valuation_intel["marginSafety"] <= -0.12 and valuation_intel["confidence"] >= 0.4)
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
                        "signal": "intrinsic-overvaluation" if valuation_intel["marginSafety"] <= -0.12 else "crowded-upside",
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
        }

    def _build_signals(
        self,
        positions: list[PositionAnalysis],
        notes: list[str],
        news: dict[str, list[Headline]],
        risk: dict[str, float | None],
        macro: dict[str, MacroPoint],
        behavioral: dict[str, Any],
    ) -> dict[str, Any]:
        top_tickers = [p.ticker for p in positions[:5]]
        radar = self._build_headline_radar(news, top_tickers)
        watchouts = self._build_watchouts(positions, radar, behavioral)
        scenarios = self._build_scenarios(positions)
        warnings = self._build_warnings(notes, risk, macro, radar, scenarios, behavioral)
        pulse = _build_pulse(warnings, scenarios)
        return {
            "pulse": pulse,
            "warnings": warnings,
            "watchouts": watchouts,
            "radar": radar,
            "scenarios": scenarios,
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


def _action_bias(opportunity: float, distribution: float, macro_panic: float, event_risk: float, margin_safety: float) -> str:
    if margin_safety >= 0.15 and macro_panic <= 0.9 and event_risk <= 0.82:
        return "accumulate-on-weakness"
    if margin_safety <= -0.12 and distribution >= 0.55:
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
    margin_safety: float,
) -> str:
    rel = f"{relative_20:+.1%}" if relative_20 is not None else "n/a"
    loc = f"{location:.0%}" if location is not None else "n/a"
    ret20_txt = f"{ret20:+.1%}" if ret20 is not None else "n/a"
    mos_txt = f"{margin_safety:+.1%}"
    if action_bias == "accumulate-on-weakness":
        return f"Deep pullback setup: 5d {ret5:+.1%}, 20d {ret20_txt}, drawdown {drawdown:.1%}, range location {loc}, vs SPY {rel}, margin of safety {mos_txt}."
    if action_bias == "trim-into-strength":
        return f"Crowded upside setup: 5d {ret5:+.1%}, 20d {ret20_txt}, range location {loc}, positive headline pressure {risk_down_share:.0%}, margin of safety {mos_txt}."
    if action_bias == "de-risk-hedge":
        return f"Risk pressure setup: drawdown {drawdown:.1%}, risk-up headline share {risk_up_share:.0%}, relative performance {rel}, margin of safety {mos_txt}."
    return f"Mixed setup: 5d {ret5:+.1%}, 20d {ret20_txt}, range location {loc}, risk-up headlines {risk_up_share:.0%}, margin of safety {mos_txt}."


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

    pe = _as_float(valuation.get("pe"))
    pb = _as_float(valuation.get("pb"))
    fcf_yield = _as_float(valuation.get("fcfYield"))
    roe = _as_float(quality.get("roe"))
    gm = _as_float(quality.get("grossMargin"))
    d2e = _as_float(quality.get("debtToEquity"))
    target_price = _as_float(analyst.get("targetPrice"))
    recommendation = _as_float(analyst.get("recommendationMean"))
    eps_growth = _as_float(fundamental.get("earningsGrowth"))
    rev_growth = _as_float(fundamental.get("revenueGrowth"))

    methods: list[dict[str, Any]] = []
    method_weights = 0.0
    weighted_value = 0.0

    if target_price and target_price > 0:
        rel = target_price / price
        if 0.45 <= rel <= 2.8:
            w = 0.38
            methods.append(
                {
                    "name": "analyst_consensus",
                    "value": round(target_price, 2),
                    "weight": round(w, 3),
                    "impliedUpside": round((target_price / price) - 1, 4),
                }
            )
            weighted_value += target_price * w
            method_weights += w

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
        if 0.4 <= (fcf_value / price) <= 2.8:
            w = 0.34
            methods.append(
                {
                    "name": "fcf_cap_model",
                    "value": round(fcf_value, 2),
                    "weight": round(w, 3),
                    "impliedUpside": round((fcf_value / price) - 1, 4),
                    "assumedFairYield": round(fair_yield, 4),
                }
            )
            weighted_value += fcf_value * w
            method_weights += w

    if pe and pe > 0:
        growth = eps_growth if isinstance(eps_growth, (int, float)) else rev_growth
        growth = max(-0.05, min(0.35, float(growth))) if isinstance(growth, (int, float)) else 0.08
        fair_pe = 16.0 + (growth * 38.0)
        if isinstance(roe, (int, float)):
            fair_pe += max(-2.0, min(4.0, (float(roe) - 0.12) * 22.0))
        fair_pe = max(11.0, min(42.0, fair_pe))
        pe_value = price * (fair_pe / float(pe))
        if 0.45 <= (pe_value / price) <= 2.5:
            w = 0.18
            methods.append(
                {
                    "name": "pe_relative",
                    "value": round(pe_value, 2),
                    "weight": round(w, 3),
                    "impliedUpside": round((pe_value / price) - 1, 4),
                    "fairMultiple": round(fair_pe, 2),
                }
            )
            weighted_value += pe_value * w
            method_weights += w

    if pb and pb > 0 and isinstance(roe, (int, float)):
        fair_pb = max(1.2, min(12.0, float(roe) * 13.0))
        pb_value = price * (fair_pb / float(pb))
        if 0.45 <= (pb_value / price) <= 2.5:
            w = 0.1
            methods.append(
                {
                    "name": "pb_relative",
                    "value": round(pb_value, 2),
                    "weight": round(w, 3),
                    "impliedUpside": round((pb_value / price) - 1, 4),
                    "fairMultiple": round(fair_pb, 2),
                }
            )
            weighted_value += pb_value * w
            method_weights += w

    if method_weights <= 0:
        return {
            "fairValue": None,
            "marginSafety": 0.0,
            "verdict": "unknown",
            "confidence": 0.0,
            "undervaluationScore": 0.5,
            "overvaluationScore": 0.5,
            "methods": [],
        }

    fair_value = weighted_value / method_weights
    margin_safety = (fair_value / price) - 1.0
    confidence = _clamp01(0.25 + (method_weights * 0.7) + (0.05 if recommendation is not None else 0.0))
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
