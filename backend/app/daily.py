from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from app.analysis import AnalysisService
from app.config import Settings
from app.models import AnalysisRequest, DailyBriefResponse, DailyBriefTicker, PositionIn
from app.providers.market import MarketProvider


_DAILY_CACHE: dict[str, Any] = {}
_DAILY_LOCK = asyncio.Lock()


class DailyBriefService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.market = MarketProvider(settings)
        self.analysis = AnalysisService(settings)

    async def get_brief(self, force: bool = False) -> DailyBriefResponse:
        async with _DAILY_LOCK:
            today = date.today().isoformat()
            cached_at = _DAILY_CACHE.get("cached_at")
            cached_payload = _DAILY_CACHE.get("payload")
            if (
                not force
                and isinstance(cached_at, datetime)
                and isinstance(cached_payload, DailyBriefResponse)
                and cached_payload.as_of.isoformat() == today
                and (datetime.now(UTC) - cached_at).total_seconds() < self.settings.daily_brief_cache_ttl_seconds
            ):
                return cached_payload

            universe = _daily_universe(self.settings.daily_brief_watchlist)
            selected = await self._select_tickers(universe)
            if not selected:
                selected = [
                    DailyBriefTicker(ticker=ticker, score=0.0, reason="Fallback core desk name.")
                    for ticker in universe[: max(1, self.settings.daily_brief_ticker_count)]
                ]

            payload = AnalysisRequest(
                positions=[
                    PositionIn(ticker=row.ticker, qty=1, asset_type="etf" if row.ticker in {"SPY", "QQQ", "IWM", "SMH", "SOXX"} else "stock")
                    for row in selected[: self.settings.daily_brief_ticker_count]
                ]
            )
            analysis = await self.analysis.analyze(payload, quick_mode=False)
            headline, thesis, agenda = _brief_narrative(analysis.meta, selected)
            brief = DailyBriefResponse(
                as_of=analysis.as_of,
                generated_at=datetime.now(UTC).isoformat(),
                universe=universe,
                selected=selected[: self.settings.daily_brief_ticker_count],
                headline=headline,
                thesis=thesis,
                agenda=agenda,
                analysis=analysis,
            )
            _DAILY_CACHE["cached_at"] = datetime.now(UTC)
            _DAILY_CACHE["payload"] = brief
            return brief

    async def _select_tickers(self, universe: list[str]) -> list[DailyBriefTicker]:
        quotes = await self.market.get_quotes(universe)
        history_tasks = [self.market.get_history(ticker, 30) for ticker in universe]
        histories = await asyncio.gather(*history_tasks, return_exceptions=True)

        candidates: list[tuple[float, DailyBriefTicker]] = []
        tech_inputs: list[tuple[str, list[float], float | None, float | None]] = []
        for ticker, raw_history in zip(universe, histories, strict=False):
            quote = quotes.get(ticker)
            prices = raw_history if isinstance(raw_history, list) else []
            move_1d = None
            if quote and quote.prev_close:
                move_1d = (quote.price / quote.prev_close) - 1
            move_5d = _period_return(prices, 5)
            base = (abs(move_1d or 0.0) * 8.0) + (abs(move_5d or 0.0) * 3.0)
            if ticker in {"SPY", "QQQ", "IWM"}:
                base += 0.08
            if ticker in {"NVDA", "MSFT", "AAPL", "META", "TSLA"}:
                base += 0.05
            tech_inputs.append((ticker, prices, move_1d, move_5d))

        tech_tasks = [
            self.market.get_technical_snapshot(ticker, prices=prices, enrich_remote=False)
            for ticker, prices, _, _ in tech_inputs
            if prices
        ]
        tech_rows = await asyncio.gather(*tech_tasks, return_exceptions=True) if tech_tasks else []
        tech_by_ticker: dict[str, dict[str, Any]] = {}
        tech_idx = 0
        for ticker, prices, _, _ in tech_inputs:
            if not prices:
                continue
            row = tech_rows[tech_idx] if tech_idx < len(tech_rows) else {}
            tech_idx += 1
            if isinstance(row, dict):
                tech_by_ticker[ticker] = row

        for ticker, prices, move_1d, move_5d in tech_inputs:
            technical = tech_by_ticker.get(ticker, {})
            technical_state = str(technical.get("signalState") or "unknown")
            technical_score = _floatish(technical.get("technicalScore")) or 0.5
            overbought = _floatish(technical.get("overboughtScore")) or 0.0
            oversold = _floatish(technical.get("oversoldScore")) or 0.0
            base = (abs(move_1d or 0.0) * 8.0) + (abs(move_5d or 0.0) * 3.0) + (abs(technical_score - 0.5) * 0.35)
            base += max(overbought, oversold) * 0.25
            if ticker in {"SPY", "QQQ", "IWM"}:
                base += 0.08
            if ticker in {"NVDA", "MSFT", "AAPL", "META", "TSLA"}:
                base += 0.05
            reason_bits = []
            if move_1d is not None:
                reason_bits.append(f"1d {move_1d:+.1%}")
            if move_5d is not None:
                reason_bits.append(f"5d {move_5d:+.1%}")
            if technical_state != "unknown":
                reason_bits.append(technical_state.replace("-", " "))
            if max(overbought, oversold) >= 0.55:
                reason_bits.append("technical extreme")
            candidates.append(
                (
                    base,
                    DailyBriefTicker(
                        ticker=ticker,
                        score=round(base, 3),
                        move_1d=round(move_1d, 4) if move_1d is not None else None,
                        move_5d=round(move_5d, 4) if move_5d is not None else None,
                        technical_state=technical_state,
                        reason="; ".join(reason_bits) or "Core watchlist name with enough market data for the desk run.",
                    ),
                )
            )

        candidates.sort(key=lambda item: item[0], reverse=True)
        selected: list[DailyBriefTicker] = []
        for required in ("SPY", "QQQ"):
            row = next((candidate for _, candidate in candidates if candidate.ticker == required), None)
            if row:
                selected.append(row)
        for _, candidate in candidates:
            if candidate.ticker not in {row.ticker for row in selected}:
                selected.append(candidate)
            if len(selected) >= self.settings.daily_brief_ticker_count:
                break
        return selected


def _daily_universe(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.split(","):
        ticker = part.strip().upper()
        if ticker and ticker not in out:
            out.append(ticker)
    return out or ["SPY", "QQQ", "NVDA", "MSFT", "AAPL", "META"]


def _period_return(prices: list[float], days: int) -> float | None:
    if len(prices) <= days:
        return None
    start = prices[-days - 1]
    end = prices[-1]
    if not start:
        return None
    return (end / start) - 1


def _brief_narrative(meta: dict[str, Any], selected: list[DailyBriefTicker]) -> tuple[str, str, list[str]]:
    intelligence = meta.get("intelligence") if isinstance(meta.get("intelligence"), dict) else {}
    pulse = meta.get("pulse") if isinstance(meta.get("pulse"), dict) else {}
    behavioral = intelligence if isinstance(intelligence, dict) else {}
    regime = behavioral.get("regime") if isinstance(behavioral.get("regime"), dict) else {}
    analyst_desk = behavioral.get("analystDesk") if isinstance(behavioral.get("analystDesk"), dict) else {}

    top = selected[0].ticker if selected else "the tape"
    regime_state = str(regime.get("state") or "balanced")
    headline = str(pulse.get("thesis") or analyst_desk.get("headline") or f"Daily desk is focused on {top} and the index tape.")
    thesis = str(
        analyst_desk.get("summary")
        or f"Auto-selected watchlist shows a {regime_state} regime. Start with {top}, then compare index confirmation against single-name dispersion."
    )
    agenda = []
    if isinstance(analyst_desk.get("playbook"), list):
        agenda.extend(str(item) for item in analyst_desk["playbook"][:3])
    if not agenda:
        agenda = [
            "Check whether SPY/QQQ confirm or contradict the selected single-name moves.",
            "Separate panic dislocation from real distribution before adding risk.",
            "Use the expanded Holding Intelligence rows for confirmation and invalidation levels.",
        ]
    return headline, thesis, agenda[:4]


def _floatish(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
