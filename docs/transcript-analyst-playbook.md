# RiskPulse Strategy Playbook v2

## Confluence Signal Engine For Portfolio Intelligence

Source material:

- Six Chinese market-commentary transcripts in `transcripts/*.zh.txt`
- External distillation: confluence signal engine specification
- Core themes: macro gates, CTA/flow state, prior-day OHLC confirmation, event/IV windows, analyst-rating reactions, post-earnings volatility, and failure-mode warnings.

This document is an engineering spec for RiskPulse. It is not investment advice.

## 0. Philosophy And Architecture Mapping

The transcript framework is simple but powerful: markets are layered, and a signal is only useful when layers agree.

| Transcript Concept | RiskPulse Module |
| --- | --- |
| Macro/geopolitical regime | `macro_gates.py` / current `macroGate` object |
| CTA / retail / HF flow | `flows.py` / current flow proxy from trend, squeeze, news, shorts |
| Technical levels | `technical.py` / current technical snapshot + future OHLC rules |
| Earnings + IV bands | `events.py` / future calendar and options layer |
| Confluence scoring | current `layerScores` + `confluenceScore` |
| Narrative output | AI layer explains deterministic score, never overrides it |

Current implementation is inside `backend/app/analysis.py`. Longer term, split this into modules.

## 1. Target Signal Schema

The unit RiskPulse should return per holding:

```yaml
TickerSignal:
  ticker: str
  composite_score: float        # [-3.0, +3.0], after macro gate
  direction: enum               # long | short | neutral | avoid
  conviction: enum              # high | medium | low
  size_hint: float              # 0.0-1.5 R-units; null if avoid
  layers:
    L1_flows: LayerScore
    L2_technical: LayerScore
    L3_event: LayerScore
  macro_factor: float           # 0.0, 0.5, 1.0; future 1.2 for squeeze relief
  invalidation_level: float|null
  notes: list[str]
```

Layer score:

```yaml
LayerScore:
  contribution: int             # -1, 0, +1
  state: str
  evidence: dict
```

Composite:

```text
raw = L1 + L2 + L3
composite_score = raw * macro_factor
```

Direction:

- `long` if `composite_score >= +2`
- `short` if `composite_score <= -2`
- `neutral` otherwise
- `avoid` if `macro_factor == 0.0`

Conviction:

- `high`: all three layers non-zero and same sign
- `medium`: two layers agree
- `low`: otherwise

Current implementation already exposes a simpler version:

- `tickerIntel[*].layerScores`
- `tickerIntel[*].confluenceScore`
- `tickerIntel[*].macroGate.factor`
- `tickerIntel[*].analystRead`

## 2. Macro Gate Layer

Macro is a multiplier, not additive. This is the most important correction versus naive score dashboards.

### Gate Definitions

| Gate ID | Trigger | Effect |
| --- | --- | --- |
| `oil_shock` | Brent close > 110 for 2 sessions | `macro_factor = 0.5` |
| `oil_extreme` | Brent > 130 or strait closure > 60 days | `macro_factor = 0.0` for new longs |
| `yield_warning` | US10Y > 4.55 | `macro_factor = 0.5` on growth/long-duration |
| `yield_critical` | US10Y > 4.65 | `macro_factor = 0.0` on growth; allow defensive value |
| `yield_relief` | US10Y < 4.00 sustained | `macro_factor = 1.0` |
| `geo_resolution_window` | Strait/conflict resolved | possible squeeze multiplier |
| `breadth_narrow` | <3 of 11 sectors green and top 5 names dominate index move | index ETF factor reduced |

When multiple gates fire, take the minimum applicable factor.

### Geopolitical Scenario Clock

```yaml
geopolitical_scenario:
  resolved_within_2mo:
    spx_floor: 6100
    qqq_floor_zone: [560, 580]
    spy_floor_zone: [605, 630]
    bias: accumulate_on_dip
  resolved_2_to_6mo:
    spx_floor: 5500
    bias: defensive_only
    ai_capex_thesis: under_review
  unresolved_6mo_plus:
    spx_floor: null
    bias: capital_preservation
    macro_factor_override: 0.0
```

Implementation now:

- Current `macroGate` is score-based from VIX, rates, dollar, SPY, and headline shock.

Implementation next:

- Add named gate IDs.
- Add Brent/WTI values from OpenBB if available.
- Add event clock for unresolved geopolitical shocks.
- Add sector ETF breadth proxy.

## 3. Layer 1: Flow And Positioning

### CTA State Machine

| CTA State | Definition | L1 Contribution |
| --- | --- | --- |
| `accumulating` | trailing-month buy > 20B, trigger far below spot | +1 |
| `loaded_slowing` | buy velocity decelerating, spot overshot trigger | 0 |
| `exhausted` | buy velocity near zero, no sell trigger near | 0 |
| `at_sell_trigger` | spot within 1% of sell trigger | -1 |
| `cascading` | trigger breached, forced selling | -1 and macro factor pressure |
| `net_short` | aggregate position negative | +1 for squeeze setup |

Free proxy until we have desk-note data:

- SPY/QQQ 20/50/200-day trend
- realized volatility
- distance from moving averages
- recent return velocity
- breadth

Critical nuance: `loaded_slowing` is **not bullish**. CTA may still buy, but the marginal buyer is fading. Score it `0`, not `+1`.

### Retail Flow Regime

| Retail State | Proxy | Contribution |
| --- | --- | --- |
| `puke` | outflows or extreme bearish sentiment | +1 contrarian |
| `buy_dip` | steady inflows on red days | +1 |
| `chase` | inflows into highs, call volume spike | -1 |
| `frenzy` | extreme call/put, leveraged ETF AUM record | -1, factor pressure |

Fallback proxy:

- leveraged ETF performance/volume (`TQQQ`, `SOXL`, `TSLL`)
- options call/put if available
- headline buzz + overbought technical state

### Hedge Fund Leverage Proxy

| State | Contribution |
| --- | --- |
| `defensive` | 0 |
| `engaged` | +1 |
| `crowded` | -1 |

Fallback proxy:

- crowded-long basket behavior if available
- mega-cap concentration and dispersion

## 4. Layer 2: Technical

### Prior-Day OHLC Rule

Most repeated transcript rule:

```python
def prior_day_break_signal(bars):
    today = bars[-1]
    yday = bars[-2]
    if today.open < yday.low or today.close < yday.low:
        return ("bearish_break", -1, yday.high)
    if today.open > yday.high or today.close > yday.high:
        return ("bullish_break", +1, yday.low)
    return ("no_signal", 0, None)
```

Important:

- Intraday wick does not count.
- Open or close matters.
- A broken support becomes invalidation logic.

Current implementation approximates this using returns, range location, and trend score. Next version should ingest OHLC bars.

### Intraday Tape Regime

Only computed when the prior-day rule fires:

- `absorption`: low in first 60 minutes, higher lows, close upper third. Downgrade bearish signal.
- `distribution`: high in first 60 minutes, lower highs, close lower third. Upgrade bearish signal.
- `inconclusive`: keep base contribution but lower conviction.

This prevents shorting mechanical selling that active buyers are absorbing.

### Structure Rules

`right_side_breakout`:

- multi-month consolidation high breaks
- volume > 1.5x 20-day average
- two nearby supports below
- contribution +1, conviction medium unless fundamentals confirm

`topping_thin_support`:

- two-bar head
- second bar closes below first range
- low support density below
- contribution -1, high conviction
- first target = nearest volume node, second = 200-day MA

`range_filter`:

- if 20-day ATR / median price > 4%, downgrade simple prior-day signals
- use range-edge breaks instead
- TSLA-class stocks need this

`volume_confirmation`:

- fresh high on shrinking volume = suspect
- breakout volume below 20-day average = contribution 0
- large red candle + high volume + no support response = no falling knife

## 5. Layer 3: Events

### Earnings Window

```yaml
earnings_window_state:
  pre_2d_to_pre_1d: implied_move_band_active
  report_day: no_new_position
  post_1d_to_post_2d: reaction_trade_window
  outside: normal
```

### Implied Move Band

For names near earnings:

```python
move = atm_straddle_price
lower = spot - move
upper = spot + move
```

Rules:

- upper band above strong resistance: long-skew if L1 supports
- lower band below breakout structure: short-skew if L1 supports
- both edges inside noise: no event trade
- max-pain pin near OPEX: annotate, do not force score

### Reaction Taxonomy

| Print Type | Pattern | L3 Contribution | Narrative |
| --- | --- | --- | --- |
| `beat_raise` | beat top/bottom + raised guide | +1 | LLY-template |
| `beat_inline_guide` | beat but flat next-Q guide | 0 | QCOM-template, expect retest |
| `beat_cash_return` | buyback/dividend without growth acceleration | 0 | AAPL defensive carry |
| `beat_capex_raise` | raised AI capex commentary | +1 | MSFT/META-template |
| `miss_or_cut` | miss or cut guide | -1 | apply breakdown rules |
| `mixed` | call tone decides | 0 | wait for D+2 close |

### AI Capex Window

For hyperscalers:

- Q2 reports, late July / early August, are key for AI capex lifecycle.
- unchanged/raised capex = +1 regime tag for AI complex.
- first moderation hint = `regime_review = true` for `NVDA`, `AMD`, `AVGO`, `TSM`, `MU`, `SOXX`, `SMH`.
- year-end monetization checkpoint reduces sizing 30 days before.

## 6. Asymmetric Rules

### Short Squeeze Priority

When macro relief fires and flows are net short:

```text
priority = short_interest_pct / 90th_percentile_history
```

Transcript ranking:

- `IWM`: highest squeeze priority
- `SPY`: medium
- `QQQ`: lower

### Sell-Side Target Dispersion

```text
target_dispersion = (max_target - min_target) / median_target
```

| Dispersion | Meaning | Action |
| --- | --- | --- |
| < 0.3 | consensus name | normal sizing |
| 0.3-0.6 | disagreement/chop | size -25% |
| > 0.6 | narrative premium | cap size at 0.5R |

Useful for TSLA-class names.

### JPM Collar Levels

Quarterly JPM hedged-equity collar strikes can act as structural boundaries.

- lower strike = soft support
- upper strike = soft resistance
- refresh quarterly from public filings/prospectus data

### CTA Cascade Target Zones

When `cta_state == cascading`, prior CTA buying zones become downside magnets and eventual cover zones.

## 7. Sizing Matrix

| Confluence | Macro Factor 1.0 | Macro Factor 0.5 |
| --- | --- | --- |
| all 3 layers agree | 1.5R | 0.75R |
| 2 of 3 agree | 1.0R | 0.5R |
| only L2 fires | 0.5R | skip |
| L1 + L3, no technical | wait | wait |

Where:

- `1R = 0.5%` account equity at invalidation.
- single-name cap = 15%.
- mega-cap basket cap = 40%.

RiskPulse UI should not phrase this as a trading instruction. Use `size_hint` or `risk budget hint` language.

## 8. Portfolio State

Target portfolio-level object:

```yaml
PortfolioState:
  net_long_exposure: float
  ai_complex_exposure: float
  duration_sensitivity: high|medium|low
  geo_clock_weeks: int|null
  regime_label: accumulate|defensive|preserve|balanced
  macro_factor_floor: float
  watchouts: list[str]
```

This should power Market Pulse.

## 9. Failure Modes

Encode these as warnings, not hard assertions:

| Pattern | Detection | Warning |
| --- | --- | --- |
| chase after CTA exhaustion | `cta_state=exhausted` and L2 long | Marginal CTA buyer is gone; new longs are momentum-only. |
| index long in narrow regime | `breadth_narrow` and ticker in `SPY`,`QQQ` | Index beta misleading; trade leaders directly. |
| knife-catching cascade | thin-support topping and no catalyst | Thin support below; expect stop overshoot. |
| pre-earnings long in chase regime | retail chase and earnings within 2 sessions | Crowded into print; beat can still sell off. |
| macro gate ignored | macro factor < 1 and new long | Macro gate reduces usable size. |
| absorption mistaken for trend | absorption + L1 negative | One day of absorption is observation, not signal. |
| positive headlines vs tightening macro | good news but oil/yields rising | Track price and macro inputs, not headlines alone. |

## 10. Provider Requirements

| Layer | Feed | Fallback |
| --- | --- | --- |
| CTA | prime broker notes | trend-following proxy |
| Retail | JPM/GS reports | 0DTE, leveraged ETF volume/AUM |
| HF leverage | prime brokerage | crowded-long basket proxy |
| Technical | daily + intraday OHLCV | daily close/range proxy |
| Volume profile | computed from OHLCV | swing low density proxy |
| Earnings | OpenBB/vendor | manual watchlist |
| Options | options chain | unavailable/deferred |
| Macro | FRED/OpenBB/yahoo | current macro snapshot |
| JPM collar | public filings | unavailable/deferred |

Missing feeds should degrade gracefully:

- set sub-signal state to `unavailable`
- reduce conviction
- never silently assume zero

## 11. AI Layer Contract

AI may:

- explain the deterministic score
- translate notes into concise analyst language
- classify ambiguous headlines
- surface what confirms and invalidates

AI must not:

- override `direction`
- invent price targets
- claim macro events are resolved if the gate is unresolved
- turn neutral into advice

## 12. Backtest Before Serious Use

Required research before live capital use:

1. Backtest prior-day OHLC rule over 5+ years.
2. Test CTA/trend proxy as a regime overlay.
3. Validate implied-move band logic over 2+ years of earnings.
4. Stress-test macro gates on 2022 yields and 2020/2026 oil shocks.
5. Walk-forward target-dispersion modifier.

## 13. Current Implementation Status

Implemented now:

- `confirmationState`
- `entryDiscipline`
- `macroGate`
- `macroGate.factor`
- `layerScores`
- `confluenceScore`
- `analystRead`
- `analystDesk`

Still needed:

- OHLC-aware prior-day high/low rules
- real intraday absorption/distribution
- event calendar and IV band
- flow proxy module
- target dispersion
- portfolio state object
- frontend Confluence Engine panel

