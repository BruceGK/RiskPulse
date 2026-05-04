# RiskPulse

RiskPulse is an Azure-ready portfolio intelligence app that combines market data, macro context, and news-driven signals into a single analysis view.

## Product Overview
- **Frontend**: Next.js dashboard for portfolio entry and analysis.
- **Backend**: FastAPI engine for pricing, risk, valuation, and narrative synthesis.
- **Deployment target**: Azure Static Web Apps (frontend) + Azure Container Apps (backend).
- **Scale target**: small team / demo usage (tens of concurrent users).

## Implemented Capabilities
- Portfolio workflow:
  - add/edit positions on `/portfolio`
  - run full analysis on `/analysis`
  - shareable analysis links
- Analysis engine:
  - `POST /api/analyze` with `quick` and `full` phases
  - `POST /api/valuation` for ticker-level intrinsic value pass
  - `GET /api/daily-brief` for an automated daily analyst desk and selected watchlist basket
  - concentration, volatility, drawdown, regime, and scenario outputs
  - ticker intelligence (tech state, value view, action bias, confidence)
  - headline/event integration for macro and holdings
  - macro release surprise interpretation (`actual vs forecast`) with plain-English impact and portfolio readthrough
- Data provider strategy:
  - resilient fallback chain across multiple providers (market, macro, news)
  - in-memory TTL caching and free-tier throttle guards
  - provider coverage telemetry returned in API metadata
  - macro calendar support via OpenBB and optional TradingEconomics fallback
- AI layer:
  - transforms model outputs into a concise market pulse, warnings, and watchouts
  - designed to be optional/fail-open when AI is unavailable

## API Surface
- `POST /api/analyze`
- `POST /api/valuation`
- `GET /api/daily-brief`
- `GET /health`

Example analyze request:
```json
{
  "positions": [
    { "ticker": "MSFT", "qty": 12.5, "asset_type": "stock" },
    { "ticker": "SPY", "qty": 5, "asset_type": "etf" }
  ]
}
```

Example valuation request:
```json
{
  "tickers": ["MSFT", "NVDA", "META"]
}
```

## Local Development
1. Copy environment templates:
```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
```
2. Start services:
```bash
docker compose up --build
```
3. Open:
- Frontend: `http://localhost:3000`
- Backend health: `http://localhost:8000/health`

## Azure Deployment Architecture
- **SWA** hosts the Next.js frontend.
- **Container Apps** hosts the FastAPI backend image.
- **ACR** stores backend images.
- **Key Vault / app settings** hold runtime secrets.
- **SWA backend link** routes frontend `/api/*` to the backend service.

This repository includes GitHub Actions workflows for frontend and backend deployment. Credentials and provider keys must be configured as repository/environment secrets in your own Azure subscription and are intentionally not documented here with account-specific steps.

Optional daily automation:
- Set repository variable `RISKPULSE_API_URL` to the backend base URL, for example the Azure Container Apps URL.
- The `Warm Daily Analyst Desk` workflow calls `/api/daily-brief?force=true` on weekdays after the US market opens to refresh the automated watchlist briefing.

## Engineering Notes
- Prefer licensed APIs for production-quality data.
- Keep backend provider integrations modular and fail-open.
- Treat AI output as an explanation layer on top of deterministic signals.
- Validate all external provider payloads and track coverage in `meta.providers`.
- Optional macro calendar knobs: `OPENBB_MACRO_PROVIDER`, `TRADING_ECONOMICS_API_KEY`.

## Next Product Steps
- persistent portfolio storage and user auth
- alerting channels (email/slack/webhooks)
- model monitoring and drift dashboards
- backtesting / paper-trade loop for signal quality
