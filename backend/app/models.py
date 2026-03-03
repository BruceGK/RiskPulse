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
