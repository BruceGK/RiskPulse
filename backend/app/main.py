import logging
import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.agent import InvestmentAgentService
from app.analysis import AnalysisService
from app.config import get_settings
from app.daily import DailyBriefService
from app.models import AgentResponse, AnalysisRequest, AnalysisResponse, DailyBriefResponse, ValuationRequest, ValuationResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

settings = get_settings()
app = FastAPI(title=settings.app_name)
logger = logging.getLogger("riskpulse.api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_request(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, object]:
    """Readiness probe: reports which provider keys are configured.

    Returns 200 even if no keys are set so the probe still surfaces config,
    but the response makes degraded state visible to operators.
    """
    keys = {
        "polygon": bool(settings.polygon_api_key),
        "fmp": bool(settings.fmp_api_key),
        "alpha_vantage": bool(settings.alpha_vantage_api_key),
        "fred": bool(settings.fred_api_key),
        "openai": bool(settings.openai_api_key),
        "newsapi": bool(settings.newsapi_api_key),
        "openbb": bool(settings.openbb_base_url),
    }
    has_market = keys["polygon"] or keys["fmp"]
    has_fundamentals = keys["fmp"] or keys["alpha_vantage"] or keys["openbb"]
    has_news = keys["polygon"] or keys["alpha_vantage"] or keys["newsapi"] or keys["openbb"]
    return {
        "status": "ok" if (has_market and has_fundamentals) else "degraded",
        "providers": keys,
        "capabilities": {
            "market_data": has_market,
            "fundamentals": has_fundamentals,
            "news": has_news,
            "macro": keys["fred"],
            "ai_synthesis": keys["openai"],
        },
    }


@app.post(f"{settings.api_prefix}/analyze", response_model=AnalysisResponse)
async def analyze(payload: AnalysisRequest, phase: str = Query(default="full")) -> AnalysisResponse:
    try:
        service = AnalysisService(settings)
        quick_mode = phase.strip().lower() == "quick"
        return await service.analyze(payload, quick_mode=quick_mode)
    except Exception as exc:
        logger.exception("Analysis request failed")
        raise HTTPException(status_code=500, detail="Analysis failed") from exc


@app.post(f"{settings.api_prefix}/valuation", response_model=ValuationResponse)
async def valuation(payload: ValuationRequest) -> ValuationResponse:
    try:
        service = AnalysisService(settings)
        return await service.analyze_valuation(payload.tickers)
    except Exception as exc:
        logger.exception("Valuation request failed")
        raise HTTPException(status_code=500, detail="Valuation failed") from exc


@app.get(f"{settings.api_prefix}/daily-brief", response_model=DailyBriefResponse)
async def daily_brief(force: bool = Query(default=False)) -> DailyBriefResponse:
    try:
        service = DailyBriefService(settings)
        return await service.get_brief(force=force)
    except Exception as exc:
        logger.exception("Daily brief request failed")
        raise HTTPException(status_code=500, detail="Daily brief failed") from exc


@app.get(f"{settings.api_prefix}/agent", response_model=AgentResponse)
async def investment_agent(force: bool = Query(default=False)) -> AgentResponse:
    try:
        service = InvestmentAgentService(settings)
        return await service.get_agent(force=force)
    except Exception as exc:
        logger.exception("Investment agent request failed")
        raise HTTPException(status_code=500, detail="Investment agent failed") from exc
