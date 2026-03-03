from dataclasses import dataclass


@dataclass
class Quote:
    ticker: str
    price: float
    prev_close: float | None = None
    source: str | None = None


@dataclass
class SeriesPoint:
    symbol: str
    value: float
    previous_value: float | None
    as_of: str | None
    source: str | None = None


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published_at: str | None = None
    sentiment_hint: str | None = None
