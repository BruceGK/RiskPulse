from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator


class PositionIn(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
    qty: float = Field(gt=0)
    asset_type: str = "stock"

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper().strip()


class AnalysisRequest(BaseModel):
    positions: list[PositionIn] = Field(min_length=1)


class ValuationRequest(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=20)

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            symbol = value.upper().strip()
            if not symbol:
                continue
            if symbol not in out:
                out.append(symbol)
        if not out:
            raise ValueError("At least one valid ticker is required.")
        return out


class PositionAnalysis(BaseModel):
    ticker: str
    qty: float
    price: float
    value: float
    weight: float
    chg_pct_1d: float | None = None


class MacroPoint(BaseModel):
    value: float | None = None
    chg_pct_1d: float | None = None
    chg_bp_1d: float | None = None
    as_of: str | None = None


class Headline(BaseModel):
    source: str
    title: str
    url: str
    published_at: str | None = None
    sentiment_hint: str | None = None


class AnalysisResponse(BaseModel):
    as_of: date
    portfolio_value: float
    positions: list[PositionAnalysis]
    top_concentration: dict[str, float]
    risk: dict[str, float | None]
    macro: dict[str, MacroPoint]
    news: dict[str, list[Headline]]
    notes: list[str]
    meta: dict[str, Any]


class DailyBriefTicker(BaseModel):
    ticker: str
    score: float
    move_1d: float | None = None
    move_5d: float | None = None
    technical_state: str = "unknown"
    reason: str


class DailyBriefResponse(BaseModel):
    as_of: date
    generated_at: str
    universe: list[str]
    selected: list[DailyBriefTicker]
    headline: str
    thesis: str
    agenda: list[str]
    analysis: AnalysisResponse


class AgentSetup(BaseModel):
    ticker: str
    setup: str
    action: str
    bucket: str
    score: float
    confidence: float
    urgency: str
    time_horizon: str
    why_now: str
    confirm_if: str
    invalidate_if: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    memory: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    as_of: date
    generated_at: str
    headline: str
    thesis: str
    market_state: dict[str, Any]
    priorities: list[str]
    setups: list[AgentSetup]
    confirmed_entries: list[AgentSetup]
    watchlist: list[AgentSetup]
    trim_risks: list[AgentSetup]
    avoid: list[AgentSetup]
    source_daily_brief: DailyBriefResponse


class ValuationPoint(BaseModel):
    ticker: str
    price: float | None = None
    price_source: str | None = None
    fair_value: float | None = None
    margin_safety: float | None = None
    verdict: str = "unknown"
    confidence: float = 0.0
    valuation_inputs: int = 0
    methods: list[dict[str, Any]] = Field(default_factory=list)
    providers: dict[str, bool] = Field(default_factory=dict)


class ValuationResponse(BaseModel):
    as_of: date
    items: list[ValuationPoint]
    notes: list[str] = Field(default_factory=list)
