from __future__ import annotations

from urllib.parse import quote_plus

import httpx

from app.config import Settings
from app.providers.cache import TTLCache
from app.providers.types import NewsItem

_NEWS_CACHE = TTLCache[list[NewsItem]](max_size=3000)


class NewsProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def get_ticker_news(self, ticker: str, limit: int) -> list[NewsItem]:
        cache_key = f"ticker-news:{ticker}:{limit}"
        cached = _NEWS_CACHE.get(cache_key)
        if cached is not None:
            return cached

        items = await self._polygon_ticker_news(ticker, limit)
        if not items:
            items = await self._alpha_vantage_ticker_news(ticker, limit)
        if not items:
            items = await self._newsapi_ticker_news(ticker, limit)

        _NEWS_CACHE.set(cache_key, items, ttl_seconds=self.settings.news_cache_ttl_seconds)
        return items

    async def get_macro_news(self, limit: int) -> list[NewsItem]:
        cache_key = f"macro-news:{limit}"
        cached = _NEWS_CACHE.get(cache_key)
        if cached is not None:
            return cached

        items = await self._polygon_macro_news(limit)
        if not items:
            items = await self._alpha_vantage_macro_news(limit)
        if not items:
            items = await self._newsapi_macro_news(limit)

        _NEWS_CACHE.set(cache_key, items, ttl_seconds=self.settings.news_cache_ttl_seconds)
        return items

    async def _polygon_ticker_news(self, ticker: str, limit: int) -> list[NewsItem]:
        if not self.settings.polygon_api_key:
            return []
        url = "https://api.polygon.io/v2/reference/news"
        params = {
            "ticker": ticker,
            "limit": limit,
            "order": "desc",
            "sort": "published_utc",
            "apiKey": self.settings.polygon_api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                rows = resp.json().get("results", [])
            return [self._polygon_row_to_item(row) for row in rows if row.get("title") and row.get("article_url")]
        except Exception:
            return []

    async def _polygon_macro_news(self, limit: int) -> list[NewsItem]:
        if not self.settings.polygon_api_key:
            return []
        url = "https://api.polygon.io/v2/reference/news"
        params = {
            "limit": limit,
            "order": "desc",
            "sort": "published_utc",
            "apiKey": self.settings.polygon_api_key,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                rows = resp.json().get("results", [])
            out: list[NewsItem] = []
            keywords = ("fed", "inflation", "treasury", "yield", "dollar", "macro", "cpi", "jobs")
            for row in rows:
                title = (row.get("title") or "").lower()
                description = (row.get("description") or "").lower()
                joined = f"{title} {description}"
                if any(keyword in joined for keyword in keywords):
                    out.append(self._polygon_row_to_item(row))
            return out[:limit]
        except Exception:
            return []

    async def _newsapi_ticker_news(self, ticker: str, limit: int) -> list[NewsItem]:
        if not self.settings.newsapi_api_key:
            return []
        query = quote_plus(f"{ticker} stock")
        url = f"https://newsapi.org/v2/everything?q={query}&language=en&pageSize={limit}&sortBy=publishedAt"
        headers = {"X-Api-Key": self.settings.newsapi_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                rows = resp.json().get("articles", [])
            return [self._newsapi_row_to_item(row) for row in rows if row.get("title") and row.get("url")]
        except Exception:
            return []

    async def _newsapi_macro_news(self, limit: int) -> list[NewsItem]:
        if not self.settings.newsapi_api_key:
            return []
        query = quote_plus("(federal reserve OR treasury yields OR inflation OR dollar index OR vix)")
        url = f"https://newsapi.org/v2/everything?q={query}&language=en&pageSize={limit}&sortBy=publishedAt"
        headers = {"X-Api-Key": self.settings.newsapi_api_key}
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                rows = resp.json().get("articles", [])
            return [self._newsapi_row_to_item(row) for row in rows if row.get("title") and row.get("url")]
        except Exception:
            return []

    async def _alpha_vantage_ticker_news(self, ticker: str, limit: int) -> list[NewsItem]:
        if not self.settings.alpha_vantage_api_key:
            return []
        params = {
            "function": "NEWS_SENTIMENT",
            "tickers": ticker,
            "sort": "LATEST",
            "limit": limit,
            "apikey": self.settings.alpha_vantage_api_key,
        }
        return await self._alpha_vantage_news(params, limit)

    async def _alpha_vantage_macro_news(self, limit: int) -> list[NewsItem]:
        if not self.settings.alpha_vantage_api_key:
            return []
        params = {
            "function": "NEWS_SENTIMENT",
            "topics": "economy_macro,financial_markets",
            "sort": "LATEST",
            "limit": limit,
            "apikey": self.settings.alpha_vantage_api_key,
        }
        return await self._alpha_vantage_news(params, limit)

    async def _alpha_vantage_news(self, params: dict[str, str | int], limit: int) -> list[NewsItem]:
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
                resp = await client.get("https://www.alphavantage.co/query", params=params)
                resp.raise_for_status()
                payload = resp.json()
            if _alpha_vantage_is_limited(payload):
                return []
            rows = payload.get("feed", [])
            out: list[NewsItem] = []
            for row in rows[:limit]:
                title = row.get("title")
                url = row.get("url")
                if not title or not url:
                    continue
                out.append(
                    NewsItem(
                        source=row.get("source") or "Alpha Vantage",
                        title=title,
                        url=url,
                        published_at=row.get("time_published"),
                        sentiment_hint=row.get("overall_sentiment_label"),
                    )
                )
            return out
        except Exception:
            return []

    @staticmethod
    def _polygon_row_to_item(row: dict) -> NewsItem:
        publisher = row.get("publisher", {}) or {}
        return NewsItem(
            source=publisher.get("name") or "Polygon",
            title=row.get("title", ""),
            url=row.get("article_url", ""),
            published_at=row.get("published_utc"),
            sentiment_hint=row.get("insights", [{}])[0].get("sentiment") if row.get("insights") else None,
        )

    @staticmethod
    def _newsapi_row_to_item(row: dict) -> NewsItem:
        source = row.get("source", {}) or {}
        return NewsItem(
            source=source.get("name") or "NewsAPI",
            title=row.get("title", ""),
            url=row.get("url", ""),
            published_at=row.get("publishedAt"),
            sentiment_hint=None,
        )


def _alpha_vantage_is_limited(payload: dict) -> bool:
    return bool(payload.get("Note") or payload.get("Information") or payload.get("Error Message"))
