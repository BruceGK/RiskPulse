from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "RiskPulse API"
    api_prefix: str = "/api"
    request_timeout_seconds: int = 20
    history_days: int = 120
    news_limit: int = 20
    quote_cache_ttl_seconds: int = 120
    quote_miss_cache_ttl_seconds: int = 15
    history_cache_ttl_seconds: int = 3600
    history_miss_cache_ttl_seconds: int = 60
    technical_cache_ttl_seconds: int = 1800
    macro_cache_ttl_seconds: int = 1800
    news_cache_ttl_seconds: int = 900
    alpha_vantage_max_calls_per_request: int = 4
    alpha_vantage_technical_calls_per_ticker: int = 2
    alpha_vantage_technical_enriched_tickers: int = 2
    max_positions_for_risk: int = 5
    max_positions_for_intel: int = 8
    max_ticker_news_symbols: int = 5
    ticker_news_per_symbol: int = 5

    polygon_api_key: str = ""
    fmp_api_key: str = ""
    alpha_vantage_api_key: str = ""
    fred_api_key: str = ""
    newsapi_api_key: str = ""
    openbb_base_url: str = ""
    openbb_provider: str = "yfinance"
    openbb_macro_provider: str = "tradingeconomics"
    trading_economics_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-nano"
    ai_cache_ttl_seconds: int = 1800
    sec_user_agent: str = "RiskPulse/1.0 support@example.com"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
