# RiskPulse

Azure-ready portfolio + macro analysis MVP.

## What Is Implemented
- `backend/`: FastAPI service with:
  - `POST /api/analyze` (supports `?phase=quick|full`)
  - `POST /api/valuation` (ticker-only intrinsic value pass)
  - market data provider fallback: Polygon -> FMP -> OpenBB -> Yahoo -> Alpha Vantage
  - macro data: FRED (`VIXCLS`, `DGS10`, `DTWEXBGS`) + SPY/GLD market quotes
  - headlines: Polygon -> Alpha Vantage -> NewsAPI fallback
  - in-memory TTL cache for prices, history, macro series, and news to reduce API quota pressure
  - free-tier safeguards (Alpha Vantage call cap and graceful throttling fallback)
  - SEC filing hints for top holdings
  - behavioral signal model for panic/crowding/dislocation and ticker-level action bias
  - regime probabilities, forecast probabilities (5d/20d), action book, and hedge plan outputs
  - optional low-cost OpenAI narrative refinement (cached to reduce repeated token spend)
- `frontend/`: Next.js app
  - `/portfolio` position entry
  - `/analysis` dashboard rendering API output
- `docker-compose.yml` for local two-service startup
- GitHub Actions CI/CD:
  - backend deploy to ACR + Container Apps
  - frontend deploy to Static Web Apps

## API Contract
`POST /api/analyze`

Request:
```json
{
  "positions": [
    { "ticker": "MSFT", "qty": 12.5, "asset_type": "stock" },
    { "ticker": "SPY", "qty": 5, "asset_type": "etf" }
  ]
}
```

Response (shape):
```json
{
  "as_of": "2026-03-02",
  "portfolio_value": 12345.67,
  "positions": [],
  "top_concentration": { "top5Weight": 0.78 },
  "risk": { "vol60d": 0.22, "vol120d": 0.19, "maxDrawdown120d": 0.08 },
  "macro": {},
  "news": {},
  "notes": [],
  "meta": {}
}
```

Quick pass request:
`POST /api/analyze?phase=quick`

Valuation-only request:
`POST /api/valuation`

```json
{
  "tickers": ["MSFT", "NVDA", "META"]
}
```

Valuation-only response (shape):
```json
{
  "as_of": "2026-03-03",
  "items": [
    {
      "ticker": "MSFT",
      "price": 403.93,
      "price_source": "yahoo",
      "fair_value": 526.4,
      "margin_safety": 0.3032,
      "verdict": "undervalued",
      "confidence": 0.71,
      "valuation_inputs": 6,
      "methods": [],
      "providers": {
        "openbb": false,
        "alpha_vantage": true,
        "yahoo_quote_summary": true,
        "yahoo_quote": true
      }
    }
  ],
  "notes": []
}
```

## Local Run
1. Backend env:
```bash
cp backend/.env.example backend/.env
```

2. Frontend env:
```bash
cp frontend/.env.example frontend/.env.local
```

3. Run with Docker:
```bash
docker compose up --build
```

4. Open:
- Frontend: `http://localhost:3000`
- Backend health: `http://localhost:8000/health`

## Azure Deployment
Recommended architecture:
- Frontend: Azure Static Web Apps (SWA)
- Backend: Azure Container Apps
- Secrets: Azure Key Vault + Managed Identity
- Optional cache: Azure Managed Redis

### Deploy backend (Container Apps)
1. Build/push image to ACR.
2. Create Container App from image.
3. Set env vars from `backend/.env.example`.
4. Expose ingress externally.

### Deploy frontend (Static Web Apps)
1. Connect GitHub repo in SWA.
2. Set build path to `frontend`.
3. Link backend in SWA "Bring your own API":
   - `az staticwebapp backends link -n riskpulse-web -g RiskPulse --backend-resource-id <container-app-resource-id>`
4. Keep `NEXT_PUBLIC_API_BASE_URL` empty when backend link is configured (frontend defaults to `/api/analyze`).

## Provider Notes
- Production-grade sources should be licensed APIs, not scraping.
- OpenAI does not provide raw live market/news feed; it should consume provider output.
- SEC API requires a real contact in `SEC_USER_AGENT`.
- OpenBB integration is optional and expects a running OpenBB API service.

## GitHub Actions Secrets
Set these repository secrets before enabling CI/CD:
- `AZURE_CREDENTIALS` (service principal JSON for Azure login)
- `AZURE_STATIC_WEB_APPS_API_TOKEN` (from SWA deployment token)

Generate `AZURE_CREDENTIALS` JSON:
```bash
az ad sp create-for-rbac \
  --name riskpulse-github-deploy \
  --role Contributor \
  --scopes /subscriptions/<SUBSCRIPTION_ID>/resourceGroups/RiskPulse \
  --sdk-auth
```

Get SWA deployment token:
```bash
az staticwebapp secrets list -n riskpulse-web -g RiskPulse
```

Note:
- After linking SWA backend (`backends link`), direct calls to Container App URL may return `401` by design.
- Call backend via SWA route (`/api/*`) once frontend is deployed.

Optional runtime secrets for backend (store in Container App / Key Vault, not in git):
- `POLYGON_API_KEY`
- `FMP_API_KEY`
- `ALPHA_VANTAGE_API_KEY`
- `FRED_API_KEY`
- `NEWSAPI_API_KEY`
- `OPENBB_BASE_URL` (optional self-hosted OpenBB API endpoint, e.g. `http://openbb:6900`)
- `OPENBB_PROVIDER` (default `yfinance`)
- `OPENAI_API_KEY`
- `OPENAI_MODEL` (recommended low-cost default: `gpt-4.1-nano`)
- `SEC_USER_AGENT`

Optional runtime tuning knobs:
- `MAX_POSITIONS_FOR_INTEL` (default `8`)
- `MAX_TICKER_NEWS_SYMBOLS` (default `5`)
- `TICKER_NEWS_PER_SYMBOL` (default `5`)
