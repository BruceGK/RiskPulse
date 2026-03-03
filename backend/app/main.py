import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.analysis import AnalysisService
from app.config import get_settings
from app.models import AnalysisRequest, AnalysisResponse, ValuationRequest, ValuationResponse

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
