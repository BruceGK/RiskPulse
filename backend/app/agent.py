from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.daily import DailyBriefService
from app.models import AgentResponse, AgentSetup, DailyBriefResponse


_AGENT_CACHE: dict[str, Any] = {}
_AGENT_MEMORY: dict[str, dict[str, Any]] = {}


class InvestmentAgentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.daily = DailyBriefService(settings)

    async def get_agent(self, force: bool = False) -> AgentResponse:
        cached_at = _AGENT_CACHE.get("cached_at")
        cached_payload = _AGENT_CACHE.get("payload")
        if (
            not force
            and isinstance(cached_at, datetime)
            and isinstance(cached_payload, AgentResponse)
            and (datetime.now(UTC) - cached_at).total_seconds() < self.settings.agent_cache_ttl_seconds
        ):
            return cached_payload

        brief = await self.daily.get_brief(force=force)
        response = _build_agent_response(brief)
        _AGENT_CACHE["cached_at"] = datetime.now(UTC)
        _AGENT_CACHE["payload"] = response
        _remember(response)
        return response


def _build_agent_response(brief: DailyBriefResponse) -> AgentResponse:
    signals = _dict(_dict(brief.analysis.meta).get("signals"))
    regime = _dict(signals.get("regime"))
    predictions = _dict(signals.get("predictions"))
    horizon5d = _dict(predictions.get("horizon5d"))
    analyst_desk = _dict(signals.get("analystDesk"))
    macro_context = _dict(signals.get("macroContext"))
    ticker_intel = _list_of_dicts(signals.get("tickerIntel"))

    setups = [_build_setup(row, brief) for row in ticker_intel]
    setups.sort(key=lambda row: (bucket_rank(row.bucket), row.score, row.confidence), reverse=True)

    confirmed = [row for row in setups if row.bucket == "confirmed-entry"][:5]
    watchlist = [row for row in setups if row.bucket in {"wait-confirmation", "watch"}][:8]
    trim_risks = [row for row in setups if row.bucket == "trim-risk"][:5]
    avoid = [row for row in setups if row.bucket == "avoid"][:5]

    market_state = {
        "regime": regime.get("state") or "balanced",
        "panicScore": _round_float(regime.get("panicScore")),
        "crowdingScore": _round_float(regime.get("crowdingScore")),
        "downside5d": _round_float(horizon5d.get("downsideProb")),
        "upside5d": _round_float(horizon5d.get("upsideProb")),
        "macroRead": macro_context.get("summary") or analyst_desk.get("marketRead") or brief.thesis,
    }
    headline = _agent_headline(confirmed, watchlist, trim_risks, brief)
    thesis = _agent_thesis(market_state, confirmed, watchlist, trim_risks, analyst_desk, brief)
    priorities = _agent_priorities(confirmed, watchlist, trim_risks, avoid, analyst_desk)

    return AgentResponse(
        as_of=brief.as_of,
        generated_at=datetime.now(UTC).isoformat(),
        headline=headline,
        thesis=thesis,
        market_state=market_state,
        priorities=priorities,
        setups=setups[:12],
        confirmed_entries=confirmed,
        watchlist=watchlist,
        trim_risks=trim_risks,
        avoid=avoid,
        source_daily_brief=brief,
    )


def _build_setup(row: dict[str, Any], brief: DailyBriefResponse) -> AgentSetup:
    ticker = str(row.get("ticker") or "").upper() or "UNKNOWN"
    action_bias = str(row.get("actionBias") or "watch-hold")
    confidence = _clamp01(_float(row.get("confidence"), 0.0))
    opportunity = _clamp01(_float(row.get("opportunityIndex"), 0.0))
    distribution = _clamp01(_float(row.get("distributionIndex"), 0.0))
    event_risk = _clamp01(_float(row.get("eventRisk"), 0.0))
    panic = _clamp01(_float(row.get("panicScore"), 0.0))
    crowding = _clamp01(_float(row.get("crowdingScore"), 0.0))
    alpha = _float(row.get("alphaScore"), 0.0)
    confirmation_state = str(row.get("confirmationState") or "unconfirmed")
    entry_discipline = str(row.get("entryDiscipline") or "wait-for-confirmation")
    confluence = _dict(row.get("confluenceScore"))
    final_score = _float(confluence.get("final"), 0.0)
    layer_scores = _dict(row.get("layerScores"))
    macro_gate = _dict(row.get("macroGate"))
    analyst_read = _dict(row.get("analystRead"))
    valuation = _dict(row.get("valuation"))
    features = _dict(row.get("features"))
    technical = _dict(row.get("technical"))

    bucket = _setup_bucket(
        action_bias=action_bias,
        opportunity=opportunity,
        distribution=distribution,
        event_risk=event_risk,
        final_score=final_score,
        confirmation_state=confirmation_state,
        macro_factor=_float(macro_gate.get("factor"), 1.0),
    )
    setup = _setup_name(bucket, row, opportunity, distribution, panic, crowding)
    score = _setup_score(bucket, opportunity, distribution, confidence, final_score, event_risk, panic, crowding)
    urgency = _urgency(bucket, score, event_risk, confirmation_state)
    horizon = _time_horizon(bucket, event_risk, confirmation_state)

    why_now = str(
        analyst_read.get("whyNow")
        or row.get("rationale")
        or f"{ticker} is selected by the agent because opportunity={opportunity:.0%}, distribution={distribution:.0%}, confluence={final_score:.1f}."
    )
    confirm_if = str(analyst_read.get("confirmsIf") or _default_confirm_if(bucket, confirmation_state, entry_discipline))
    invalidate_if = str(analyst_read.get("invalidatesIf") or _default_invalidate_if(bucket, event_risk, distribution))
    thesis = str(analyst_read.get("thesis") or "")
    if thesis and thesis not in why_now:
        why_now = f"{thesis} {why_now}"

    previous = _AGENT_MEMORY.get(ticker, {})
    memory = {
        "previousBucket": previous.get("bucket"),
        "previousAction": previous.get("action"),
        "change": _memory_change(previous, bucket, action_bias, score),
    }
    tags = _tags(row, bucket, brief)

    return AgentSetup(
        ticker=ticker,
        setup=setup,
        action=_agent_action(bucket, action_bias),
        bucket=bucket,
        score=round(score, 3),
        confidence=round(confidence, 3),
        urgency=urgency,
        time_horizon=horizon,
        why_now=why_now,
        confirm_if=confirm_if,
        invalidate_if=invalidate_if,
        evidence={
            "opportunity": round(opportunity, 3),
            "distribution": round(distribution, 3),
            "eventRisk": round(event_risk, 3),
            "panic": round(panic, 3),
            "crowding": round(crowding, 3),
            "alpha": round(alpha, 3),
            "confluence": round(final_score, 3),
            "confirmationState": confirmation_state,
            "entryDiscipline": entry_discipline,
            "macroGate": macro_gate,
            "layers": layer_scores,
            "valuation": {
                "fairValue": valuation.get("fairValue"),
                "marginSafety": valuation.get("marginSafety"),
                "verdict": valuation.get("verdict"),
                "confidence": valuation.get("confidence"),
            },
            "technicalState": technical.get("signalState") or features.get("technicalState"),
        },
        tags=tags,
        memory=memory,
    )


def _setup_bucket(
    *,
    action_bias: str,
    opportunity: float,
    distribution: float,
    event_risk: float,
    final_score: float,
    confirmation_state: str,
    macro_factor: float,
) -> str:
    if macro_factor <= 0.1 or event_risk >= 0.88:
        return "avoid"
    if distribution >= 0.62 or final_score <= -2:
        return "trim-risk"
    if final_score >= 2 and opportunity >= 0.45 and confirmation_state in {"confirmed-breakout", "reclaiming-resistance"}:
        return "confirmed-entry"
    if action_bias in {"accumulate", "accumulate-on-weakness"} and opportunity >= 0.5:
        return "wait-confirmation"
    if opportunity >= 0.58 and distribution < 0.58:
        return "wait-confirmation"
    return "watch"


def _setup_name(bucket: str, row: dict[str, Any], opportunity: float, distribution: float, panic: float, crowding: float) -> str:
    valuation = _dict(row.get("valuation"))
    verdict = str(valuation.get("verdict") or "unknown")
    if bucket == "confirmed-entry":
        return "confirmed-confluence-entry"
    if bucket == "wait-confirmation":
        if verdict in {"undervalued", "slightly-undervalued"} or opportunity >= 0.65:
            return "panic-or-value-dislocation"
        return "constructive-watch"
    if bucket == "trim-risk":
        if crowding >= 0.55 or distribution >= 0.65:
            return "crowded-distribution-risk"
        return "risk-reduction-watch"
    if bucket == "avoid":
        return "macro-or-event-block"
    if panic >= 0.55:
        return "panic-watch"
    return "monitor"


def _setup_score(bucket: str, opportunity: float, distribution: float, confidence: float, final_score: float, event_risk: float, panic: float, crowding: float) -> float:
    confluence = max(0.0, min(1.0, abs(final_score) / 3.0))
    if bucket in {"confirmed-entry", "wait-confirmation"}:
        return _clamp01((opportunity * 0.42) + (confidence * 0.22) + (confluence * 0.22) + (panic * 0.08) - (event_risk * 0.12))
    if bucket == "trim-risk":
        return _clamp01((distribution * 0.42) + (crowding * 0.22) + (confidence * 0.18) + (confluence * 0.16) + (event_risk * 0.08))
    if bucket == "avoid":
        return _clamp01(0.55 + event_risk * 0.35 + distribution * 0.1)
    return _clamp01((max(opportunity, distribution) * 0.36) + (confidence * 0.22) + (confluence * 0.16))


def _agent_action(bucket: str, action_bias: str) -> str:
    if bucket == "confirmed-entry":
        return "consider-entry"
    if bucket == "wait-confirmation":
        return "wait-for-trigger"
    if bucket == "trim-risk":
        return "trim-or-hedge"
    if bucket == "avoid":
        return "avoid-new-risk"
    return action_bias or "watch"


def _urgency(bucket: str, score: float, event_risk: float, confirmation_state: str) -> str:
    if bucket == "confirmed-entry" and score >= 0.62:
        return "high"
    if bucket == "trim-risk" and (score >= 0.62 or event_risk >= 0.7):
        return "high"
    if confirmation_state in {"failed-breakdown-watch", "reclaiming-resistance"}:
        return "medium"
    return "medium" if score >= 0.45 else "low"


def _time_horizon(bucket: str, event_risk: float, confirmation_state: str) -> str:
    if event_risk >= 0.65:
        return "1-5 trading days"
    if bucket == "confirmed-entry" or confirmation_state in {"reclaiming-resistance", "confirmed-breakout"}:
        return "1-10 trading days"
    if bucket == "wait-confirmation":
        return "5-20 trading days"
    return "1-20 trading days"


def _default_confirm_if(bucket: str, confirmation_state: str, entry_discipline: str) -> str:
    if bucket == "confirmed-entry":
        return "Hold only while the confirmed technical state remains intact and macro gate does not close."
    if bucket == "trim-risk":
        return "Risk rises if price fails to reclaim prior support or crowding continues into strength."
    if bucket == "avoid":
        return "Reconsider only after macro/event gate improves and price stabilizes."
    return f"Upgrade only after {entry_discipline.replace('-', ' ')}; current state is {confirmation_state.replace('-', ' ')}."


def _default_invalidate_if(bucket: str, event_risk: float, distribution: float) -> str:
    if bucket in {"confirmed-entry", "wait-confirmation"}:
        return "Invalidate if support fails, event risk accelerates, or the confluence score drops below actionable range."
    if bucket == "trim-risk":
        return "Invalidate the trim thesis if price absorbs selling, event risk fades, and opportunity score overtakes distribution."
    if event_risk >= 0.65:
        return "Event risk must normalize before new exposure is considered."
    return "No hard thesis yet; keep this as observation until confirmation appears."


def _agent_headline(confirmed: list[AgentSetup], watchlist: list[AgentSetup], trim_risks: list[AgentSetup], brief: DailyBriefResponse) -> str:
    if confirmed:
        return f"{confirmed[0].ticker} is the clearest confirmed setup; {len(watchlist)} names remain on trigger watch."
    if trim_risks:
        return f"Risk control leads today: {trim_risks[0].ticker} screens as the top trim/hedge candidate."
    if watchlist:
        return f"No clean entry yet; {watchlist[0].ticker} is the highest-quality watchlist setup."
    return brief.headline


def _agent_thesis(
    market_state: dict[str, Any],
    confirmed: list[AgentSetup],
    watchlist: list[AgentSetup],
    trim_risks: list[AgentSetup],
    analyst_desk: dict[str, Any],
    brief: DailyBriefResponse,
) -> str:
    if confirmed:
        return f"Agent sees actionable confirmation in {confirmed[0].ticker}, but sizing should still respect the {market_state.get('regime')} regime."
    if trim_risks:
        return f"Agent is prioritizing risk management because distribution/crowding beats opportunity in {trim_risks[0].ticker}."
    if watchlist:
        return f"Agent is waiting for price confirmation. {watchlist[0].ticker} has a setup, but the trigger has not fully fired."
    return str(analyst_desk.get("marketRead") or brief.thesis)


def _agent_priorities(
    confirmed: list[AgentSetup],
    watchlist: list[AgentSetup],
    trim_risks: list[AgentSetup],
    avoid: list[AgentSetup],
    analyst_desk: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    if confirmed:
        out.append(f"Review {confirmed[0].ticker} first: confirmed confluence requires execution discipline, not chasing.")
    if watchlist:
        out.append(f"Set alerts on {', '.join(row.ticker for row in watchlist[:3])}: these are wait-for-trigger candidates.")
    if trim_risks:
        out.append(f"Check trim/hedge risk in {', '.join(row.ticker for row in trim_risks[:3])}.")
    if avoid:
        out.append(f"Avoid new risk in {', '.join(row.ticker for row in avoid[:2])} until the blocking condition clears.")
    next_watch = analyst_desk.get("nextThingToWatch")
    if isinstance(next_watch, str) and next_watch:
        out.append(next_watch)
    return out[:5] or ["No urgent action. Keep the desk warm and wait for confirmation."]


def _tags(row: dict[str, Any], bucket: str, brief: DailyBriefResponse) -> list[str]:
    ticker = str(row.get("ticker") or "")
    selected = next((item for item in brief.selected if item.ticker == ticker), None)
    out = [bucket]
    if selected:
        out.append(selected.technical_state)
    for theme in row.get("themes") or []:
        if isinstance(theme, str) and theme not in out:
            out.append(theme)
    return out[:5]


def _remember(response: AgentResponse) -> None:
    for setup in response.setups:
        _AGENT_MEMORY[setup.ticker] = {
            "bucket": setup.bucket,
            "action": setup.action,
            "score": setup.score,
            "seenAt": response.generated_at,
        }


def _memory_change(previous: dict[str, Any], bucket: str, action: str, score: float) -> str:
    if not previous:
        return "new"
    previous_bucket = previous.get("bucket")
    previous_action = previous.get("action")
    previous_score = _float(previous.get("score"), score)
    if previous_bucket != bucket or previous_action != action:
        return "changed"
    if score >= previous_score + 0.08:
        return "strengthened"
    if score <= previous_score - 0.08:
        return "weakened"
    return "unchanged"


def bucket_rank(bucket: str) -> int:
    return {
        "confirmed-entry": 5,
        "wait-confirmation": 4,
        "trim-risk": 3,
        "avoid": 2,
        "watch": 1,
    }.get(bucket, 0)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
