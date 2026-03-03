from __future__ import annotations

import asyncio
import math
from datetime import date
from statistics import mean

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
        ai_summary = await self.ai.summarize(
            {
                "asOf": date.today().isoformat(),
                "portfolioValue": round(portfolio_value, 2),
                "positions": [p.model_dump() for p in position_rows[:8]],
                "risk": risk,
                "macro": {k: v.model_dump() for k, v in macro.items()},
                "notes": notes,
                "macroNews": [h.model_dump() for h in news.get("macro", [])[:6]],
            }
        )
        if ai_summary:
            notes.append(f"AI summary: {ai_summary}")

        return AnalysisResponse(
            as_of=date.today(),
            portfolio_value=round(portfolio_value, 2),
            positions=position_rows,
            top_concentration={"top5Weight": round(top5_weight, 4)},
            risk=risk,
            macro=macro,
            news=news,
            notes=notes,
            meta={
                "providers": {
                    "polygon_enabled": bool(self.settings.polygon_api_key),
                    "fred_enabled": bool(self.settings.fred_api_key),
                    "newsapi_enabled": bool(self.settings.newsapi_api_key),
                    "fmp_enabled": bool(self.settings.fmp_api_key),
                    "alpha_vantage_enabled": bool(self.settings.alpha_vantage_api_key),
                    "openai_enabled": bool(self.settings.openai_api_key),
                },
                "quoteSources": quote_sources,
                "dataQuality": data_quality,
            },
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
