"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { analyzePortfolio } from "@/lib/api";
import type { AnalysisResponse, Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";
const UI_PREFS_KEY = "riskpulse_ui_v1";
const UI_PREFS_MAX_AGE_MS = 1000 * 60 * 60 * 24 * 30;
const DEMO_SAMPLE: Position[] = [
  { ticker: "AAPL", qty: 8, asset_type: "stock" },
  { ticker: "MSFT", qty: 5, asset_type: "stock" },
  { ticker: "SPY", qty: 6, asset_type: "etf" },
];

type ViewTab = "overview" | "signals" | "holdings" | "news";
type RailTab = "macro" | "ticker" | "sec";
type HoldingsView = "essentials" | "quant" | "full";
type UiPrefs = {
  activeTab: ViewTab;
  railTab: RailTab;
  holdingsView: HoldingsView;
  activeScenario: string;
  scenarioScale: number;
  savedAt: number;
};

type SharePayload = {
  v: 1;
  ui?: {
    tab?: ViewTab;
    rail?: RailTab;
    holdingsView?: HoldingsView;
    scenario?: string;
    shock?: number;
  };
  positions?: Position[];
};

function isViewTab(value: string | null): value is ViewTab {
  return value === "overview" || value === "signals" || value === "holdings" || value === "news";
}

function isRailTab(value: string | null): value is RailTab {
  return value === "macro" || value === "ticker" || value === "sec";
}

function isHoldingsView(value: string | null): value is HoldingsView {
  return value === "essentials" || value === "quant" || value === "full";
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(2)}%`;
}

function bp(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${value.toFixed(1)} bp`;
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function asRecordArray(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item));
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function normalizePositions(value: unknown): Position[] {
  if (!Array.isArray(value)) return [];
  const out: Position[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const row = item as Record<string, unknown>;
    const ticker = typeof row.ticker === "string" ? row.ticker.toUpperCase().trim() : "";
    const qty = typeof row.qty === "number" ? row.qty : Number(row.qty);
    const assetType = typeof row.asset_type === "string" && row.asset_type.trim() ? row.asset_type.trim() : "stock";
    if (!ticker || !Number.isFinite(qty) || qty <= 0) continue;
    out.push({ ticker, qty, asset_type: assetType });
  }
  return out;
}

function encodeSharePayload(payload: SharePayload): string {
  const json = JSON.stringify(payload);
  const bytes = new TextEncoder().encode(json);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const b64 = btoa(binary);
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function decodeSharePayload(raw: string | null): SharePayload | null {
  if (!raw || !raw.trim()) return null;
  try {
    const normalized = raw.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4 || 4)) % 4);
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    const json = new TextDecoder().decode(bytes);
    const parsed = JSON.parse(json) as Partial<SharePayload>;
    if (!parsed || typeof parsed !== "object" || parsed.v !== 1) return null;
    return {
      v: 1,
      ui: parsed.ui && typeof parsed.ui === "object" ? parsed.ui : undefined,
      positions: normalizePositions(parsed.positions),
    };
  } catch {
    return null;
  }
}

export default function AnalysisPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadPhase, setLoadPhase] = useState<"idle" | "quick" | "full">("idle");
  const [error, setError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [activeScenario, setActiveScenario] = useState("");
  const [scenarioScale, setScenarioScale] = useState(1);
  const [activeTab, setActiveTab] = useState<ViewTab>("overview");
  const [railTab, setRailTab] = useState<RailTab>("macro");
  const [holdingsView, setHoldingsView] = useState<HoldingsView>("essentials");
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null);
  const [shareStatus, setShareStatus] = useState<"" | "copied" | "failed">("");
  const [isDemoSeeded, setIsDemoSeeded] = useState(false);
  const [showDemoBanner, setShowDemoBanner] = useState(false);
  const [prefsHydrated, setPrefsHydrated] = useState(false);

  useEffect(() => {
    let hasUrlTab = false;
    let hasUrlRail = false;
    let hasUrlScenario = false;
    let hasUrlShock = false;
    let hasUrlHoldingsView = false;
    try {
      const params = new URLSearchParams(window.location.search);
      const shared = decodeSharePayload(params.get("share"));
      if (shared?.ui) {
        const sharedTab = shared.ui.tab || null;
        const sharedRail = shared.ui.rail || null;
        const sharedHoldings = shared.ui.holdingsView || null;
        if (isViewTab(sharedTab)) {
          setActiveTab(sharedTab);
          hasUrlTab = true;
        }
        if (isRailTab(sharedRail)) {
          setRailTab(sharedRail);
          hasUrlRail = true;
        }
        if (isHoldingsView(sharedHoldings)) {
          setHoldingsView(sharedHoldings);
          hasUrlHoldingsView = true;
        }
        if (typeof shared.ui.scenario === "string" && shared.ui.scenario.trim().length > 0) {
          setActiveScenario(shared.ui.scenario.trim());
          hasUrlScenario = true;
        }
        if (typeof shared.ui.shock === "number" && Number.isFinite(shared.ui.shock)) {
          setScenarioScale(Math.min(2, Math.max(0.5, shared.ui.shock)));
          hasUrlShock = true;
        }
      }
      const urlTab = params.get("tab");
      const urlRail = params.get("rail");
      const urlScenario = params.get("scenario");
      const urlShock = params.get("shock");
      const urlHoldingsView = params.get("hview");
      if (isViewTab(urlTab)) {
        setActiveTab(urlTab);
        hasUrlTab = true;
      }
      if (isRailTab(urlRail)) {
        setRailTab(urlRail);
        hasUrlRail = true;
      }
      if (typeof urlScenario === "string" && urlScenario.trim().length > 0) {
        setActiveScenario(urlScenario.trim());
        hasUrlScenario = true;
      }
      if (typeof urlShock === "string" && urlShock.trim().length > 0) {
        const parsedShock = Number(urlShock);
        if (Number.isFinite(parsedShock)) {
          setScenarioScale(Math.min(2, Math.max(0.5, parsedShock)));
          hasUrlShock = true;
        }
      }
      if (isHoldingsView(urlHoldingsView)) {
        setHoldingsView(urlHoldingsView);
        hasUrlHoldingsView = true;
      }
    } catch {
      // Ignore malformed URL params and continue with persisted preferences.
    }

    try {
      const raw = localStorage.getItem(UI_PREFS_KEY);
      if (!raw) {
        setPrefsHydrated(true);
        return;
      }
      const prefs = JSON.parse(raw) as Partial<UiPrefs>;
      if (typeof prefs.savedAt === "number" && Date.now() - prefs.savedAt > UI_PREFS_MAX_AGE_MS) {
        localStorage.removeItem(UI_PREFS_KEY);
        setPrefsHydrated(true);
        return;
      }
      const savedTab = typeof prefs.activeTab === "string" ? prefs.activeTab : null;
      const savedRail = typeof prefs.railTab === "string" ? prefs.railTab : null;
      const savedHoldingsView = typeof prefs.holdingsView === "string" ? prefs.holdingsView : null;
      if (!hasUrlTab && isViewTab(savedTab)) setActiveTab(savedTab);
      if (!hasUrlRail && isRailTab(savedRail)) setRailTab(savedRail);
      if (!hasUrlHoldingsView && isHoldingsView(savedHoldingsView)) setHoldingsView(savedHoldingsView);
      if (!hasUrlScenario && typeof prefs.activeScenario === "string") setActiveScenario(prefs.activeScenario);
      if (!hasUrlShock && typeof prefs.scenarioScale === "number" && Number.isFinite(prefs.scenarioScale)) {
        setScenarioScale(Math.min(2, Math.max(0.5, prefs.scenarioScale)));
      }
    } catch {
      // Ignore malformed local storage payload.
    } finally {
      setPrefsHydrated(true);
    }
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const hasParams = params.toString().length > 0;
    const cleanQuery = () => {
      if (!hasParams) return;
      const cleanUrl = `${window.location.pathname}${window.location.hash}`;
      window.history.replaceState({}, "", cleanUrl);
    };

    try {
      const shared = decodeSharePayload(params.get("share"));
      if (shared?.positions?.length) {
        setPositions(shared.positions);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(shared.positions));
        setIsDemoSeeded(false);
        cleanQuery();
        return;
      }
    } catch {
      // Continue to local storage fallback.
    }

    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      setPositions(DEMO_SAMPLE);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(DEMO_SAMPLE));
      setIsDemoSeeded(true);
      setShowDemoBanner(true);
      setError("");
      cleanQuery();
      return;
    }
    try {
      const parsed = normalizePositions(JSON.parse(raw));
      if (parsed.length > 0) {
        setPositions(parsed);
        setIsDemoSeeded(false);
        cleanQuery();
        return;
      }
      setPositions(DEMO_SAMPLE);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(DEMO_SAMPLE));
      setIsDemoSeeded(true);
      setShowDemoBanner(true);
      setError("");
      cleanQuery();
    } catch {
      setPositions(DEMO_SAMPLE);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(DEMO_SAMPLE));
      setIsDemoSeeded(true);
      setShowDemoBanner(true);
      setError("");
      cleanQuery();
    }
  }, []);

  useEffect(() => {
    if (positions.length === 0) return;
    let active = true;
    const run = async () => {
      let quickLoaded = false;
      try {
        setLoading(true);
        setLoadPhase("quick");
        setError("");
        const quickResponse = await analyzePortfolio({ positions }, { phase: "quick" });
        if (active) {
          setAnalysis(quickResponse);
          quickLoaded = true;
        }
      } catch (e) {
        if (active) setError(`Quick pass failed, retrying full analysis... (${(e as Error).message})`);
      }

      if (!active) return;
      try {
        setLoadPhase("full");
        const fullResponse = await analyzePortfolio({ positions }, { phase: "full" });
        if (active) {
          setAnalysis(fullResponse);
          setError("");
        }
      } catch (e) {
        if (!active) return;
        if (quickLoaded) {
          setError("Deep analysis failed for this run. Showing quick pass data.");
        } else {
          setError((e as Error).message);
        }
      } finally {
        if (active) {
          setLoading(false);
          setLoadPhase("idle");
        }
      }
    };
    run();
    return () => {
      active = false;
    };
  }, [positions, refreshTick]);

  const topHeadlines = useMemo(() => analysis?.news?.macro?.slice(0, 10) || [], [analysis]);
  const tickerHeadlineGroups = useMemo(() => {
    if (!analysis?.news) return [] as Array<{ ticker: string; items: NonNullable<AnalysisResponse["news"]>[string] }>;
    return Object.entries(analysis.news)
      .filter(([key, value]) => key !== "macro" && key !== "sec" && Array.isArray(value) && value.length > 0)
      .map(([ticker, items]) => ({ ticker, items }));
  }, [analysis]);
  const secHeadlines = useMemo(() => analysis?.news?.sec || [], [analysis]);
  const topPositions = useMemo(() => analysis?.positions?.slice(0, 5) || [], [analysis]);
  const dataQuality = (analysis?.meta?.dataQuality ||
    null) as null | { score?: number; label?: string; priceCoverage?: number; macroCoverage?: number; macroNewsCount?: number };
  const quoteSources = (analysis?.meta?.quoteSources || null) as null | Record<string, string>;
  const meta = asRecord(analysis?.meta);
  const providers = asRecord(meta?.providers);
  const modelInfo = asRecord(meta?.model);
  const providerEntries = providers ? Object.entries(providers) : [];
  const signals = asRecord(meta?.signals);
  const fullPending = loadPhase === "full";
  const loadingMessage =
    loadPhase === "quick"
      ? "Loading quick pass (quotes, weights, base KPIs)..."
      : loadPhase === "full"
        ? "Hydrating deep analysis (risk, news, AI, signals)..."
        : "Running analysis against live providers...";

  const intelligence = asRecord(meta?.intelligence) || asRecord(signals?.pulse);
  const pulseThesis = typeof intelligence?.thesis === "string" ? intelligence.thesis : "";
  const pulseStance = intelligence?.stance === "risk-on" || intelligence?.stance === "risk-off" ? intelligence.stance : "balanced";
  const pulseFocus = asStringArray(intelligence?.focus).slice(0, 3);
  const pulseDeskNote = typeof intelligence?.deskNote === "string" ? intelligence.deskNote : "";
  const pulseMacroTape = typeof intelligence?.macroTape === "string" ? intelligence.macroTape : "";
  const pulseEventTape = typeof intelligence?.eventTape === "string" ? intelligence.eventTape : "";
  const pulsePositioningTape = typeof intelligence?.positioningTape === "string" ? intelligence.positioningTape : "";
  const pulsePlaybook = asStringArray(intelligence?.playbook).slice(0, 3);
  const pulseDrivers = asRecordArray(intelligence?.signalDrivers).slice(0, 4);

  const warnings = asRecordArray(signals?.warnings);
  const watchouts = asRecordArray(signals?.watchouts);
  const radar = asRecordArray(signals?.radar);
  const scenarios = asRecordArray(signals?.scenarios);
  const themeBoard = asRecordArray(signals?.themes);
  const regime = asRecord(signals?.regime);
  const tickerIntel = asRecordArray(signals?.tickerIntel);
  const opportunities = asRecordArray(signals?.opportunities);
  const exitSignals = asRecordArray(signals?.exitSignals);

  const predictions = asRecord(signals?.predictions);
  const prediction5d = asRecord(predictions?.horizon5d);
  const prediction20d = asRecord(predictions?.horizon20d);
  const predictionConfidence = asNumber(predictions?.confidence);
  const portfolioActions = asRecordArray(signals?.portfolioActions);
  const hedgePlan = asRecordArray(signals?.hedgePlan);
  const regimeProbs = asRecord(regime?.probabilities);
  const construction = asRecord(signals?.construction);
  const constructionTargets = asRecordArray(construction?.targets).slice(0, 8);
  const projectedTop1 = asNumber(construction?.projectedTop1);
  const projectedTurnover = asNumber(construction?.projectedTurnover);
  const cashBuffer = asNumber(construction?.cashBuffer);
  const alphaBook = asRecord(signals?.alphaBook);
  const alphaLongBias = asRecordArray(alphaBook?.longBias).slice(0, 5);
  const alphaUnderBias = asRecordArray(alphaBook?.underweightBias).slice(0, 5);
  const submodels = asRecord(signals?.submodels);
  const technicalSummary = asRecord(signals?.technicalSummary);
  const macroContext = asRecord(signals?.macroContext);
  const macroContextSummary = typeof macroContext?.summary === "string" ? macroContext.summary : "";
  const macroContextRegime = macroContext?.regimeBias === "risk-up" || macroContext?.regimeBias === "risk-down" ? macroContext.regimeBias : "balanced";
  const macroContextRegimeClass = macroContextRegime === "balanced" ? "neutral" : macroContextRegime;
  const macroContextDrivers = asRecordArray(macroContext?.drivers).slice(0, 6);
  const macroContextReleases = asRecordArray(macroContext?.releaseHighlights).slice(0, 4);
  const macroContextEvents = asRecordArray(macroContext?.eventReadthrough).slice(0, 4);
  const macroContextImplications = asStringArray(macroContext?.portfolioImplications).slice(0, 4);
  const holdingsColSpan =
    holdingsView === "full" ? 15 : holdingsView === "quant" ? 9 : 8;

  const convictionLabel = (row: Record<string, unknown>) => {
    const confidence = asNumber(row.confidence) || 0;
    const opportunity = asNumber(row.opportunityIndex) || 0;
    const distribution = asNumber(row.distributionIndex) || 0;
    const edge = Math.abs(opportunity - distribution);
    const score = Math.min(1, confidence * (0.6 + edge));
    if (score >= 0.66) return { label: "high", cls: "high" as const };
    if (score >= 0.45) return { label: "medium", cls: "medium" as const };
    return { label: "low", cls: "low" as const };
  };
  const submodelRows = useMemo(() => {
    if (!submodels) return [] as Array<{ name: string; score: number; confidence: number }>;
    return Object.entries(submodels)
      .map(([name, value]) => {
        const row = asRecord(value);
        const score = asNumber(row?.score);
        const confidence = asNumber(row?.confidence);
        if (score === null || confidence === null) return null;
        return { name, score, confidence };
      })
      .filter((row): row is { name: string; score: number; confidence: number } => row !== null)
      .sort((a, b) => b.confidence - a.confidence);
  }, [submodels]);

  useEffect(() => {
    if (!scenarios.length) {
      setActiveScenario("");
      return;
    }
    const ids = scenarios.map((row) => (typeof row.id === "string" ? row.id : "")).filter((id) => id.length > 0);
    if (ids.length && !ids.includes(activeScenario)) {
      setActiveScenario(ids[0]);
    }
  }, [scenarios, activeScenario]);

  useEffect(() => {
    if (!expandedTicker) return;
    const exists = tickerIntel.some((row) => typeof row.ticker === "string" && row.ticker === expandedTicker);
    if (!exists) setExpandedTicker(null);
  }, [tickerIntel, expandedTicker]);

  useEffect(() => {
    if (holdingsView !== "essentials" && expandedTicker) {
      setExpandedTicker(null);
    }
  }, [holdingsView, expandedTicker]);

  useEffect(() => {
    if (!prefsHydrated) return;
    const payload: UiPrefs = {
      activeTab,
      railTab,
      holdingsView,
      activeScenario,
      scenarioScale,
      savedAt: Date.now(),
    };
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify(payload));
  }, [activeTab, railTab, holdingsView, activeScenario, scenarioScale, prefsHydrated]);

  const selectedScenario = scenarios.find((row) => row.id === activeScenario) || scenarios[0] || null;
  const scenarioExposed = selectedScenario ? asRecordArray(selectedScenario.exposed) : [];
  const scaledScenarioImpact = selectedScenario ? (asNumber(selectedScenario.portfolioImpactPct) || 0) * scenarioScale : null;

  const movers = useMemo(
    () =>
      [...(analysis?.positions || [])]
        .filter((p) => p.chg_pct_1d !== null)
        .sort((a, b) => Math.abs(b.chg_pct_1d || 0) - Math.abs(a.chg_pct_1d || 0))
        .slice(0, 5),
    [analysis]
  );

  const railCounts = {
    macro: topHeadlines.length,
    ticker: tickerHeadlineGroups.reduce((sum, group) => sum + Math.min(group.items.length, 3), 0),
    sec: secHeadlines.length,
  };

  useEffect(() => {
    if (!shareStatus) return;
    const timer = window.setTimeout(() => setShareStatus(""), 2200);
    return () => window.clearTimeout(timer);
  }, [shareStatus]);

  const handleShareView = async () => {
    const payload: SharePayload = {
      v: 1,
      ui: {
        tab: activeTab,
        rail: railTab,
        holdingsView,
        scenario: activeScenario || undefined,
        shock: Math.abs(scenarioScale - 1) > 0.001 ? Number(scenarioScale.toFixed(1)) : undefined,
      },
      positions: normalizePositions(positions),
    };
    const encoded = encodeSharePayload(payload);
    const url = `${window.location.origin}${window.location.pathname}?share=${encodeURIComponent(encoded)}`;
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(url);
      setShareStatus("copied");
    } catch {
      setShareStatus("failed");
      window.prompt("Copy this share link:", url);
    }
  };

  return (
    <main className="container">
      <header className="topbar">
        <Link href="/portfolio" className="brand">
          <span className="brand-dot" />
          RiskPulse
        </Link>
        <div className="top-actions">
          <Link href="/portfolio" className="nav-link">
            Edit Portfolio
          </Link>
          <button className="btn secondary" onClick={handleShareView} disabled={!positions.length}>
            Share View
          </button>
          <button className="btn secondary" onClick={() => setRefreshTick((n) => n + 1)} disabled={loading || !positions.length}>
            {loadPhase === "quick" ? "Loading Quick Pass..." : loadPhase === "full" ? "Hydrating Deep Analysis..." : "Refresh Analysis"}
          </button>
        </div>
      </header>
      {showDemoBanner && isDemoSeeded && (
        <div className="status onboarding-banner" style={{ marginBottom: 12 }}>
          <div>
            <strong>Demo portfolio loaded for first run.</strong> You are seeing the analysis immediately so the risk engine is easier to evaluate.
          </div>
          <div className="banner-actions">
            <Link href="/portfolio" className="btn secondary">
              Customize Portfolio
            </Link>
            <button className="btn secondary" type="button" onClick={() => setShowDemoBanner(false)}>
              Hide
            </button>
          </div>
        </div>
      )}
      {shareStatus && (
        <div className={`status ${shareStatus === "failed" ? "error" : ""}`} style={{ marginBottom: 12 }}>
          {shareStatus === "copied"
            ? "Share link copied to clipboard."
            : "Could not access clipboard; manual copy dialog opened."}
        </div>
      )}

      <section className="hero">
        <h1>Portfolio Risk Overview</h1>
        <p className="hero-sub">Multi-model risk engine with forecast, action book, and instant news context.</p>
        {analysis && (
          <div className="hero-meta">
            <span className="pill">As of {analysis.as_of}</span>
            <span className="pill">Portfolio {money(analysis.portfolio_value)}</span>
            <span className="pill">Top5 {pct(analysis.top_concentration.top5Weight)}</span>
            <span className="pill">Model {typeof modelInfo?.name === "string" ? modelInfo.name : "baseline"}</span>
          </div>
        )}
      </section>

      {analysis && pulseThesis && (
        <section className="pulse">
          <div className="pulse-label">Market Pulse</div>
          <div className="pulse-row">
            <p className="pulse-text">{pulseThesis}</p>
            <span className={`stance ${pulseStance}`}>{pulseStance.replace("-", " ")}</span>
          </div>
          {pulseDeskNote && <p className="pulse-note">{pulseDeskNote}</p>}
          {pulseFocus.length > 0 && (
            <div className="pulse-focus">
              {pulseFocus.map((item) => (
                <span className="chip" key={item}>
                  {item}
                </span>
              ))}
            </div>
          )}
          {(pulseMacroTape || pulseEventTape || pulsePositioningTape) && (
            <div className="pulse-lines">
              {pulseMacroTape && <div className="pulse-line">{pulseMacroTape}</div>}
              {pulseEventTape && <div className="pulse-line">{pulseEventTape}</div>}
              {pulsePositioningTape && <div className="pulse-line">{pulsePositioningTape}</div>}
            </div>
          )}
          {pulsePlaybook.length > 0 && (
            <div className="pulse-playbook">
              {pulsePlaybook.map((step) => (
                <span className="chip" key={step}>
                  {step}
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      {loading && <div className="status">{loadingMessage}</div>}
      {!loading && fullPending && <div className="status">Deep analysis is still loading in the background...</div>}
      {error && <div className="status error">{error}</div>}

      {analysis && (
        <>
          <section className="grid cards" style={{ marginTop: 14 }}>
            <article className="kpi">
              <div className="kpi-label">Portfolio Value</div>
              <div className="kpi-value">{money(analysis.portfolio_value)}</div>
            </article>
            <article className="kpi">
              <div className="kpi-label">Volatility 60D</div>
              <div className="kpi-value">{pct(analysis.risk.vol60d)}</div>
            </article>
            <article className="kpi">
              <div className="kpi-label">5D Downside Prob</div>
              <div className="kpi-value">{pct(asNumber(prediction5d?.downsideProb))}</div>
            </article>
            <article className="kpi">
              <div className="kpi-label">Forecast Confidence</div>
              <div className="kpi-value">{pct(predictionConfidence)}</div>
            </article>
          </section>
          <p className="helper-text">Fast read: these cards summarize portfolio scale, realized volatility, near-term downside odds, and model confidence.</p>

          <section className="section-switcher" style={{ marginTop: 14 }}>
            <button className={`switch-chip ${activeTab === "overview" ? "active" : ""}`} onClick={() => setActiveTab("overview")}>Overview</button>
            <button className={`switch-chip ${activeTab === "signals" ? "active" : ""}`} onClick={() => setActiveTab("signals")}>Signals</button>
            <button className={`switch-chip ${activeTab === "holdings" ? "active" : ""}`} onClick={() => setActiveTab("holdings")}>Holdings</button>
            <button className={`switch-chip ${activeTab === "news" ? "active" : ""}`} onClick={() => setActiveTab("news")}>News</button>
          </section>

          <section className="analysis-shell" style={{ marginTop: 14 }}>
            <div className="analysis-main">
              {activeTab === "overview" && (
                <>
                  {movers.length > 0 && (
                    <article className="panel movers-panel">
                      <h3>Live Movers</h3>
                      <div className="mover-list">
                        {movers.map((row) => {
                          const chg = row.chg_pct_1d || 0;
                          const dirClass = chg >= 0 ? "up" : "down";
                          return (
                            <div className={`mover ${dirClass}`} key={row.ticker}>
                              <span className="mono">{row.ticker}</span>
                              <strong>{pct(chg)}</strong>
                              <span className="mover-value">{money(row.value)}</span>
                            </div>
                          );
                        })}
                      </div>
                    </article>
                  )}

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel">
                      <h3>Top Allocation</h3>
                      <div className="allocation-list">
                        {topPositions.map((position) => (
                          <div className="allocation-item" key={position.ticker}>
                            <span className="mono">{position.ticker}</span>
                            <div className="allocation-track">
                              <div className="allocation-fill" style={{ width: `${Math.max(position.weight * 100, 2)}%` }} />
                            </div>
                            <strong>{pct(position.weight)}</strong>
                          </div>
                        ))}
                      </div>
                    </article>

                    <article className="panel">
                      <h3>Input Coverage</h3>
                      <div className="notes">
                        <div className="note">Price coverage: {pct(dataQuality?.priceCoverage)}</div>
                        <div className="note">Macro coverage: {pct(dataQuality?.macroCoverage)}</div>
                        <div className="note">Macro headlines: {String(dataQuality?.macroNewsCount ?? "-")}</div>
                      </div>
                      <div className="hero-meta" style={{ marginTop: 12 }}>
                        {providerEntries.map(([k, v]) => (
                          <span className="chip" key={k}>
                            <span className="chip-dot" style={{ background: Boolean(v) ? "var(--good)" : "#9ca9b6" }} />
                            {k.replace("_enabled", "")}
                          </span>
                        ))}
                      </div>
                    </article>
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel signal-panel">
                      <h3>Model Reliability Stack</h3>
                      <p className="helper-text">Confidence reflects data coverage and internal agreement for each model slice.</p>
                      {submodelRows.length === 0 ? (
                        <div className="status">Submodel telemetry unavailable in this run.</div>
                      ) : (
                        <div className="signal-list">
                          {submodelRows.map((row) => (
                            <div className="signal-item" key={row.name}>
                              <div className="signal-head">
                                <strong>{row.name}</strong>
                                <span className="severity medium">conf {pct(row.confidence)}</span>
                              </div>
                              <div className="signal-meta">model score {pct(row.score)}</div>
                              <div className="meter-track">
                                <div className="meter-fill" style={{ width: `${Math.max(4, Math.min(100, row.confidence * 100))}%` }} />
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </article>

                    <article className="panel signal-panel">
                      <h3>Construction Engine</h3>
                      <p className="helper-text">Target weights are model suggestions for balance and risk control, not direct trade instructions.</p>
                      <div className="notes">
                        <div className="note">Projected top holding: {pct(projectedTop1)}</div>
                        <div className="note">Projected turnover: {pct(projectedTurnover)}</div>
                        <div className="note">Cash buffer: {pct(cashBuffer)}</div>
                      </div>
                      {constructionTargets.length > 0 && (
                        <div className="table-wrap" style={{ marginTop: 10 }}>
                          <table>
                            <thead>
                              <tr>
                                <th>Ticker</th>
                                <th>Current</th>
                                <th>Target</th>
                                <th>Delta</th>
                              </tr>
                            </thead>
                            <tbody>
                              {constructionTargets.map((row, idx) => {
                                const ticker = typeof row.ticker === "string" ? row.ticker : `T${idx + 1}`;
                                const current = asNumber(row.currentWeight);
                                const target = asNumber(row.targetWeight);
                                const delta = asNumber(row.delta);
                                return (
                                  <tr key={`${ticker}-${idx}`}>
                                    <td className="mono">{ticker}</td>
                                    <td>{pct(current)}</td>
                                    <td>{pct(target)}</td>
                                    <td className={delta !== null && delta < 0 ? "neg" : "pos"}>{pct(delta)}</td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel signal-panel">
                      <h3>Signal Drivers</h3>
                      {pulseDrivers.length === 0 ? (
                        <div className="status">Driver narrative unavailable in this run.</div>
                      ) : (
                        <div className="signal-list">
                          {pulseDrivers.map((row, idx) => {
                            const label = typeof row.label === "string" ? row.label : `Driver ${idx + 1}`;
                            const detail = typeof row.detail === "string" ? row.detail : "";
                            const severity = row.severity === "high" || row.severity === "low" ? row.severity : "medium";
                            return (
                              <div className="signal-item" key={`${label}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{label}</strong>
                                  <span className={`severity ${severity}`}>{severity}</span>
                                </div>
                                {detail && <div className="signal-text">{detail}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>

                    <article className="panel">
                      <h3>Theme Radar</h3>
                      {themeBoard.length === 0 ? (
                        <div className="status">Theme extraction unavailable in this run.</div>
                      ) : (
                        <div className="signal-list">
                          {themeBoard.slice(0, 6).map((row, idx) => {
                            const theme = typeof row.theme === "string" ? row.theme : `Theme ${idx + 1}`;
                            const intensity = asNumber(row.intensity);
                            const confidence = asNumber(row.confidence);
                            const direction = row.direction === "risk-up" || row.direction === "risk-down" ? row.direction : "neutral";
                            return (
                              <div className="signal-item" key={`${theme}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{theme}</strong>
                                  <span className={`dir ${direction}`}>{direction}</span>
                                </div>
                                <div className="signal-meta">intensity {pct(intensity)} · confidence {pct(confidence)}</div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Technical Breadth</h3>
                    <div className="notes">
                      <div className="note">Coverage: {pct(asNumber(technicalSummary?.coverage))}</div>
                      <div className="note">Bullish breadth: {pct(asNumber(technicalSummary?.bullishShare))}</div>
                      <div className="note">Bearish breadth: {pct(asNumber(technicalSummary?.bearishShare))}</div>
                      <div className="note">Oversold pressure: {pct(asNumber(technicalSummary?.oversoldShare))}</div>
                      <div className="note">Overbought pressure: {pct(asNumber(technicalSummary?.overboughtShare))}</div>
                    </div>
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel">
                      <h3>Macro Snapshot</h3>
                      <div className="table-wrap" style={{ marginTop: 8 }}>
                        <table>
                          <thead>
                            <tr>
                              <th>Series</th>
                              <th>Value</th>
                              <th>1D %</th>
                              <th>1D bp</th>
                              <th>As Of</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(analysis.macro).map(([name, point]) => (
                              <tr key={name}>
                                <td className="mono">{name}</td>
                                <td>{point.value === null ? "-" : point.value.toFixed(3)}</td>
                                <td>{pct(point.chg_pct_1d)}</td>
                                <td>{bp(point.chg_bp_1d)}</td>
                                <td>{point.as_of || "-"}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </article>

                    <article className="panel">
                      <h3>Macro: What It Means</h3>
                      <p className="helper-text">Plain-language readthrough for non-experts: what macro moves imply for positioning.</p>
                      <div className="notes" style={{ marginTop: 10 }}>
                        {macroContextSummary ? <div className="note">{macroContextSummary}</div> : <div className="note">Interpretation layer is calibrating for this run.</div>}
                      </div>
                      <div className="macro-meaning-meta">
                        <span className={`dir ${macroContextRegimeClass}`}>{macroContextRegime}</span>
                      </div>
                      {macroContextDrivers.length > 0 && (
                        <div className="signal-list" style={{ marginTop: 10 }}>
                          {macroContextDrivers.slice(0, 4).map((row, idx) => {
                            const driver = typeof row.driver === "string" ? row.driver : `Driver ${idx + 1}`;
                            const move = typeof row.move === "string" ? row.move : "-";
                            const signal = row.signal === "risk-up" || row.signal === "risk-down" ? row.signal : "neutral";
                            const meaning = typeof row.meaning === "string" ? row.meaning : "";
                            const playbook = typeof row.playbook === "string" ? row.playbook : "";
                            return (
                              <div className="signal-item macro-driver" key={`${driver}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{driver}</strong>
                                  <span className="signal-meta">{move}</span>
                                </div>
                                <div className="macro-driver-tags">
                                  <span className={`dir ${signal}`}>{signal}</span>
                                </div>
                                {meaning && <div className="signal-text">{meaning}</div>}
                                {playbook && <div className="signal-meta">{playbook}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {macroContextReleases.length > 0 && (
                        <div className="macro-release-grid">
                          {macroContextReleases.map((row, idx) => {
                            const event = typeof row.event === "string" ? row.event : `Release ${idx + 1}`;
                            const signal = row.signal === "risk-up" || row.signal === "risk-down" ? row.signal : "neutral";
                            const importance = asNumber(row.importance);
                            const surprise = asNumber(row.surprise);
                            const actual = asNumber(row.actual);
                            const forecast = asNumber(row.forecast);
                            const actualText = typeof row.actualText === "string" ? row.actualText : null;
                            const forecastText = typeof row.forecastText === "string" ? row.forecastText : null;
                            const meaning = typeof row.meaning === "string" ? row.meaning : "";
                            const date = typeof row.date === "string" ? row.date : "-";
                            return (
                              <div className="signal-item macro-release" key={`${event}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{event}</strong>
                                  <span className={`dir ${signal}`}>{signal}</span>
                                </div>
                                <div className="signal-meta">
                                  {date} · importance {importance ? `${importance}/3` : "-"}
                                </div>
                                <div className="signal-meta">
                                  actual {actual !== null ? actual.toFixed(3) : actualText || "-"} · forecast {forecast !== null ? forecast.toFixed(3) : forecastText || "-"} · surprise {surprise === null ? "-" : surprise.toFixed(3)}
                                </div>
                                {meaning && <div className="signal-text">{meaning}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {(macroContextEvents.length > 0 || macroContextImplications.length > 0) && (
                        <div className="notes" style={{ marginTop: 10 }}>
                          {macroContextEvents.slice(0, 2).map((row, idx) => {
                            const theme = typeof row.theme === "string" ? row.theme : `Macro Event ${idx + 1}`;
                            const meaning = typeof row.meaning === "string" ? row.meaning : "";
                            return (
                              <div className="note" key={`${theme}-${idx}`}>
                                <strong>{theme}:</strong> {meaning || "Event readthrough pending."}
                              </div>
                            );
                          })}
                          {macroContextImplications.slice(0, 2).map((line, idx) => (
                            <div className="note" key={`implication-${idx}`}>
                              <strong>Portfolio:</strong> {line}
                            </div>
                          ))}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                      <h3>Model Notes</h3>
                      <div className="notes">
                        {analysis.notes.map((note) => (
                          <div className="note" key={note}>
                            {note}
                          </div>
                        ))}
                      </div>
                  </section>
                </>
              )}

              {activeTab === "signals" && (
                <>
                  {warnings.length > 0 && (
                    <section className="panel warning-panel">
                      <h3>Warning Board</h3>
                      <div className="warning-list">
                        {warnings.map((row, idx) => {
                          const title = typeof row.title === "string" ? row.title : "Warning";
                          const severity = row.severity === "high" || row.severity === "low" ? row.severity : "medium";
                          const reason = typeof row.reason === "string" ? row.reason : "";
                          return (
                            <div className={`warning-item ${severity}`} key={`${title}-${idx}`}>
                              <div className="warning-head">
                                <strong>{title}</strong>
                                <span className={`severity ${severity}`}>{severity}</span>
                              </div>
                              {reason && <div className="warning-reason">{reason}</div>}
                            </div>
                          );
                        })}
                      </div>
                    </section>
                  )}

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel signal-panel">
                      <h3>Opportunity Scanner</h3>
                      {opportunities.length === 0 ? (
                        <div className="status">No high-conviction dislocation setup in this run.</div>
                      ) : (
                        <div className="signal-list">
                          {opportunities.map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : "-";
                            const score = asNumber(row.score);
                            const confidence = asNumber(row.confidence);
                            const reason = typeof row.reason === "string" ? row.reason : "";
                            return (
                              <div className="signal-item opportunity" key={`${ticker}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{ticker}</strong>
                                  <span className="severity low">score {score?.toFixed(2) || "-"}</span>
                                </div>
                                <div className="signal-meta">confidence {pct(confidence)}</div>
                                {reason && <div className="signal-text">{reason}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>

                    <article className="panel signal-panel">
                      <h3>Distribution Scanner</h3>
                      {exitSignals.length === 0 ? (
                        <div className="status">No crowding-driven trim signal in this run.</div>
                      ) : (
                        <div className="signal-list">
                          {exitSignals.map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : "-";
                            const score = asNumber(row.score);
                            const confidence = asNumber(row.confidence);
                            const reason = typeof row.reason === "string" ? row.reason : "";
                            return (
                              <div className="signal-item exit" key={`${ticker}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{ticker}</strong>
                                  <span className="severity high">score {score?.toFixed(2) || "-"}</span>
                                </div>
                                <div className="signal-meta">confidence {pct(confidence)}</div>
                                {reason && <div className="signal-text">{reason}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel signal-panel">
                      <h3>Forecast Engine</h3>
                      <div className="signal-list">
                        <div className="signal-item">
                          <div className="signal-head">
                            <strong>5D</strong>
                            <span className="severity medium">conf {pct(predictionConfidence)}</span>
                          </div>
                          <div className="signal-meta">
                            expected {pct(asNumber(prediction5d?.expectedReturn))} · downside {pct(asNumber(prediction5d?.downsideProb))} · upside {" "}
                            {pct(asNumber(prediction5d?.upsideProb))}
                          </div>
                        </div>
                        <div className="signal-item">
                          <div className="signal-head">
                            <strong>20D</strong>
                          </div>
                          <div className="signal-meta">
                            expected {pct(asNumber(prediction20d?.expectedReturn))} · downside {pct(asNumber(prediction20d?.downsideProb))} · upside {" "}
                            {pct(asNumber(prediction20d?.upsideProb))}
                          </div>
                        </div>
                      </div>
                      {regimeProbs && (
                        <div className="notes" style={{ marginTop: 10 }}>
                          {Object.entries(regimeProbs).map(([label, value]) => (
                            <div className="note" key={label}>
                              <strong>{label}</strong> {pct(asNumber(value))}
                            </div>
                          ))}
                        </div>
                      )}
                    </article>

                    <article className="panel signal-panel">
                      <h3>Portfolio Action Book</h3>
                      {portfolioActions.length === 0 ? (
                        <div className="status">No explicit rebalance actions from current model run.</div>
                      ) : (
                        <div className="signal-list">
                          {portfolioActions.slice(0, 6).map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : "-";
                            const action = typeof row.action === "string" ? row.action : "hold";
                            const delta = asNumber(row.targetWeightDelta);
                            const urgency = row.urgency === "high" || row.urgency === "low" ? row.urgency : "medium";
                            const reason = typeof row.reason === "string" ? row.reason : "";
                            return (
                              <div className={`signal-item ${action === "trim" || action === "de-risk" ? "exit" : "opportunity"}`} key={`${ticker}-${idx}`}>
                                <div className="signal-head">
                                  <strong>{ticker}</strong>
                                  <span className={`severity ${urgency}`}>{action}</span>
                                </div>
                                <div className="signal-meta">target delta {pct(delta)} · urgency {urgency}</div>
                                {reason && <div className="signal-text">{reason}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      {hedgePlan.length > 0 && (
                        <div className="notes" style={{ marginTop: 10 }}>
                          {hedgePlan.map((row, idx) => {
                            const name = typeof row.name === "string" ? row.name : "Hedge";
                            const reason = typeof row.reason === "string" ? row.reason : "";
                            return (
                              <div className="note" key={`${name}-${idx}`}>
                                <strong>{name}</strong>
                                {reason ? ` · ${reason}` : ""}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel">
                      <h3>Scenario Lens</h3>
                      <div className="scenario-tabs">
                        {scenarios.map((row) => {
                          const id = typeof row.id === "string" ? row.id : "";
                          const name = typeof row.name === "string" ? row.name : id;
                          if (!id) return null;
                          return (
                            <button
                              key={id}
                              className={`scenario-tab ${id === activeScenario ? "active" : ""}`}
                              onClick={() => setActiveScenario(id)}
                              type="button"
                            >
                              {name}
                            </button>
                          );
                        })}
                      </div>
                      {selectedScenario ? (
                        <div className="scenario-body">
                          <div className="slider-row">
                            <label htmlFor="scenario-scale">Shock Intensity</label>
                            <input
                              id="scenario-scale"
                              type="range"
                              min={0.5}
                              max={2}
                              step={0.1}
                              value={scenarioScale}
                              onChange={(e) => setScenarioScale(Number(e.target.value))}
                            />
                            <span>{scenarioScale.toFixed(1)}x</span>
                          </div>
                          <div className="scenario-metrics">
                            <div>
                              <div className="kpi-label">Shock</div>
                              <div>{typeof selectedScenario.shock === "string" ? selectedScenario.shock : "-"}</div>
                            </div>
                            <div>
                              <div className="kpi-label">Estimated Portfolio Impact</div>
                              <div className={`scenario-impact ${(scaledScenarioImpact || 0) < 0 ? "neg" : "pos"}`}>{pct(scaledScenarioImpact)}</div>
                            </div>
                          </div>
                          <div className="notes" style={{ marginTop: 10 }}>
                            {scenarioExposed.map((row) => {
                              const ticker = typeof row.ticker === "string" ? row.ticker : "-";
                              const weight = asNumber(row.weight);
                              const sens = asNumber(row.sensitivity);
                              return (
                                <div className="note" key={`${activeScenario}-${ticker}`}>
                                  <strong>{ticker}</strong> weight {pct(weight)} · sensitivity {sens?.toFixed(2) ?? "-"}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      ) : (
                        <div className="status" style={{ marginTop: 8 }}>
                          Scenario engine unavailable for this run.
                        </div>
                      )}
                    </article>

                    <article className="panel">
                      <h3>Position Watchouts</h3>
                      {watchouts.length === 0 ? (
                        <div className="status">No watchouts available in this run.</div>
                      ) : (
                        <div className="watchout-list">
                          {watchouts.map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : "-";
                            const severity = row.severity === "high" || row.severity === "low" ? row.severity : "medium";
                            const text = typeof row.text === "string" ? row.text : "";
                            return (
                              <div className={`watchout-item ${severity}`} key={`${ticker}-${idx}`}>
                                <div className="warning-head">
                                  <strong>{ticker}</strong>
                                  <span className={`severity ${severity}`}>{severity}</span>
                                </div>
                                <div className="watchout-text">{text}</div>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Headline Impact Radar</h3>
                    {radar.length === 0 ? (
                      <div className="status" style={{ marginTop: 8 }}>
                        Headline scoring unavailable in this run.
                      </div>
                    ) : (
                      <div className="headlines radar-list" style={{ marginTop: 8 }}>
                        {radar.map((row, idx) => {
                          const title = typeof row.title === "string" ? row.title : "";
                          const source = typeof row.source === "string" ? row.source : "Signal";
                          const url = typeof row.url === "string" ? row.url : "";
                          const publishedAt = typeof row.publishedAt === "string" ? row.publishedAt : "";
                          const impact = row.impact === "high" || row.impact === "low" ? row.impact : "medium";
                          const direction = row.direction === "risk-up" || row.direction === "risk-down" ? row.direction : "neutral";
                          const horizon = row.horizon === "intraday" || row.horizon === "1m" ? row.horizon : "1w";
                          const related = asStringArray(row.relatedTickers);
                          return (
                            <a className="headline radar-item" key={`${title}-${idx}`} href={url || "#"} target={url ? "_blank" : undefined} rel={url ? "noreferrer" : undefined}>
                              <div className="radar-tags">
                                <span className={`severity ${impact}`}>{impact}</span>
                                <span className={`dir ${direction}`}>{direction}</span>
                                <span className="chip">{horizon}</span>
                                {related.map((ticker) => (
                                  <span className="chip" key={`${title}-${ticker}`}>
                                    {ticker}
                                  </span>
                                ))}
                              </div>
                              <div className="headline-title">{title}</div>
                              <div className="headline-meta">
                                {source}
                                {publishedAt ? ` · ${publishedAt.slice(0, 16)}` : ""}
                              </div>
                            </a>
                          );
                        })}
                      </div>
                    )}
                  </section>
                </>
              )}

              {activeTab === "holdings" && (
                <>
                  <section className="panel">
                    <h3>Holdings Intelligence</h3>
                    <p className="helper-text">Use Essentials for quick decisions. Open row details for rationale, valuation inputs, and technical context.</p>
                    <div className="section-switcher" style={{ marginTop: 10 }}>
                      <button className={`switch-chip ${holdingsView === "essentials" ? "active" : ""}`} onClick={() => setHoldingsView("essentials")}>
                        Essentials
                      </button>
                      <button className={`switch-chip ${holdingsView === "quant" ? "active" : ""}`} onClick={() => setHoldingsView("quant")}>
                        Quant
                      </button>
                      <button className={`switch-chip ${holdingsView === "full" ? "active" : ""}`} onClick={() => setHoldingsView("full")}>
                        Full
                      </button>
                    </div>
                    {tickerIntel.length === 0 ? (
                      <div className="status">Ticker intelligence was unavailable in this run.</div>
                    ) : (
                      <div className="table-wrap" style={{ marginTop: 8 }}>
                        <table>
                          <thead>
                            <tr>
                              <th>Ticker</th>
                              <th>Action</th>
                              {holdingsView === "essentials" && (
                                <>
                                  <th>Conviction</th>
                                  <th>Value View</th>
                                  <th>Tech State</th>
                                  <th>MoS</th>
                                  <th>Opportunity</th>
                                  <th>Details</th>
                                </>
                              )}
                              {holdingsView === "quant" && (
                                <>
                                  <th>Tech Score</th>
                                  <th>RSI</th>
                                  <th>ADX</th>
                                  <th>Opportunity</th>
                                  <th>Distribution</th>
                                  <th>Panic</th>
                                  <th>Crowding</th>
                                </>
                              )}
                              {holdingsView === "full" && (
                                <>
                                  <th>Tech State</th>
                                  <th>Tech Score</th>
                                  <th>RSI</th>
                                  <th>ADX</th>
                                  <th>Value View</th>
                                  <th>Val Inputs</th>
                                  <th>Fair Value</th>
                                  <th>MoS</th>
                                  <th>Opportunity</th>
                                  <th>Distribution</th>
                                  <th>Panic</th>
                                  <th>Crowding</th>
                                  <th>Themes</th>
                                </>
                              )}
                            </tr>
                          </thead>
                          <tbody>
                            {tickerIntel.map((row, idx) => {
                              const ticker = typeof row.ticker === "string" ? row.ticker : `T${idx + 1}`;
                              const action = typeof row.actionBias === "string" ? row.actionBias : "-";
                              const opportunity = asNumber(row.opportunityIndex);
                              const distribution = asNumber(row.distributionIndex);
                              const panic = asNumber(row.panicScore);
                              const crowding = asNumber(row.crowdingScore);
                              const valuation = asRecord(row.valuation);
                              const openbb = asRecord(row.openbb);
                              const technical = asRecord(row.technical);
                              const coverage = asRecord(openbb?.coverage);
                              const fairValue = asNumber(valuation?.fairValue);
                              const marginSafety = asNumber(valuation?.marginSafety);
                              const valueView = typeof valuation?.verdict === "string" ? valuation.verdict : "-";
                              const valInputs = asNumber(coverage?.valuationInputs);
                              const techState = typeof technical?.signalState === "string" ? technical.signalState : "-";
                              const techScore = asNumber(technical?.technicalScore);
                              const rsi = asNumber(technical?.rsi14);
                              const adx = asNumber(technical?.adx14);
                              const rationale = typeof row.rationale === "string" ? row.rationale : "";
                              const confidence = asNumber(row.confidence);
                              const themes = asStringArray(row.themes).slice(0, 2).join(", ") || "-";
                              const conviction = convictionLabel(row);
                              const isExpanded = expandedTicker === ticker;
                              return [
                                  <tr key={`row-${ticker}-${idx}`} className="intel-row">
                                    <td className="mono">{ticker}</td>
                                    <td>{action}</td>
                                    {holdingsView === "essentials" && (
                                      <>
                                        <td>
                                          <span className={`severity ${conviction.cls}`}>{conviction.label}</span>
                                        </td>
                                        <td>{valueView}</td>
                                        <td>{techState}</td>
                                        <td className={marginSafety !== null && marginSafety < 0 ? "neg" : "pos"}>{pct(marginSafety)}</td>
                                        <td>{pct(opportunity)}</td>
                                        <td>
                                          <button
                                            type="button"
                                            className="mini-btn"
                                            onClick={() => setExpandedTicker(isExpanded ? null : ticker)}
                                          >
                                            {isExpanded ? "Hide" : "Open"}
                                          </button>
                                        </td>
                                      </>
                                    )}
                                    {holdingsView === "quant" && (
                                      <>
                                        <td>{pct(techScore)}</td>
                                        <td>{rsi === null ? "-" : rsi.toFixed(1)}</td>
                                        <td>{adx === null ? "-" : adx.toFixed(1)}</td>
                                        <td>{pct(opportunity)}</td>
                                        <td>{pct(distribution)}</td>
                                        <td>{pct(panic)}</td>
                                        <td>{pct(crowding)}</td>
                                      </>
                                    )}
                                    {holdingsView === "full" && (
                                      <>
                                        <td>{techState}</td>
                                        <td>{pct(techScore)}</td>
                                        <td>{rsi === null ? "-" : rsi.toFixed(1)}</td>
                                        <td>{adx === null ? "-" : adx.toFixed(1)}</td>
                                        <td>{valueView}</td>
                                        <td>{valInputs === null ? "-" : valInputs.toFixed(0)}</td>
                                        <td>{money(fairValue)}</td>
                                        <td className={marginSafety !== null && marginSafety < 0 ? "neg" : "pos"}>{pct(marginSafety)}</td>
                                        <td>{pct(opportunity)}</td>
                                        <td>{pct(distribution)}</td>
                                        <td>{pct(panic)}</td>
                                        <td>{pct(crowding)}</td>
                                        <td>{themes}</td>
                                      </>
                                    )}
                                  </tr>,
                                  isExpanded ? (
                                    <tr key={`detail-${ticker}-${idx}`} className="intel-expand">
                                      <td colSpan={holdingsColSpan}>
                                        <div className="intel-detail-grid">
                                          <div>
                                            <div className="kpi-label">Positioning Context</div>
                                            <div className="note" style={{ marginTop: 6 }}>
                                              Confidence {pct(confidence)} · Alpha {pct(asNumber(row.alphaScore))}
                                            </div>
                                            <div className="note">{rationale || "No rationale text was available in this run."}</div>
                                          </div>
                                          <div>
                                            <div className="kpi-label">Valuation & Inputs</div>
                                            <div className="note">Fair value {money(fairValue)} · margin {pct(marginSafety)}</div>
                                            <div className="note">Input coverage {valInputs === null ? "-" : `${valInputs.toFixed(0)} factors`}</div>
                                            <div className="note">Value view {valueView}</div>
                                          </div>
                                          <div>
                                            <div className="kpi-label">Technicals</div>
                                            <div className="note">State {techState} · score {pct(techScore)}</div>
                                            <div className="note">RSI {rsi === null ? "-" : rsi.toFixed(1)} · ADX {adx === null ? "-" : adx.toFixed(1)}</div>
                                            <div className="note">Themes {themes}</div>
                                          </div>
                                        </div>
                                      </td>
                                    </tr>
                                  ) : null,
                                ];
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </section>

                  <section className="grid two" style={{ marginTop: 14 }}>
                    <article className="panel">
                      <h3>Alpha Book: Long Bias</h3>
                      {alphaLongBias.length === 0 ? (
                        <div className="status">No long-bias candidates surfaced by this run.</div>
                      ) : (
                        <div className="notes" style={{ marginTop: 8 }}>
                          {alphaLongBias.map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : `L${idx + 1}`;
                            const score = asNumber(row.score);
                            const confidence = asNumber(row.confidence);
                            return (
                              <div className="note" key={`${ticker}-${idx}`}>
                                <strong>{ticker}</strong> alpha {pct(score)} · confidence {pct(confidence)}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>

                    <article className="panel">
                      <h3>Alpha Book: Underweight Bias</h3>
                      {alphaUnderBias.length === 0 ? (
                        <div className="status">No underweight candidates surfaced by this run.</div>
                      ) : (
                        <div className="notes" style={{ marginTop: 8 }}>
                          {alphaUnderBias.map((row, idx) => {
                            const ticker = typeof row.ticker === "string" ? row.ticker : `U${idx + 1}`;
                            const score = asNumber(row.score);
                            const confidence = asNumber(row.confidence);
                            return (
                              <div className="note" key={`${ticker}-${idx}`}>
                                <strong>{ticker}</strong> alpha {pct(score)} · confidence {pct(confidence)}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </article>
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Positions</h3>
                    <div className="table-wrap" style={{ marginTop: 8 }}>
                      <table>
                        <thead>
                          <tr>
                            <th>Ticker</th>
                            <th>Qty</th>
                            <th>Price</th>
                            <th>Value</th>
                            <th>Weight</th>
                            <th>1D</th>
                            <th>Source</th>
                          </tr>
                        </thead>
                        <tbody>
                          {analysis.positions.map((position) => (
                            <tr key={position.ticker}>
                              <td className="mono">{position.ticker}</td>
                              <td>{position.qty.toFixed(2)}</td>
                              <td>{money(position.price)}</td>
                              <td>{money(position.value)}</td>
                              <td>{pct(position.weight)}</td>
                              <td>{pct(position.chg_pct_1d)}</td>
                              <td>{quoteSources?.[position.ticker] || "-"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                </>
              )}

              {activeTab === "news" && (
                <>
                  <section className="panel">
                    <h3>Headline Impact Radar</h3>
                    {radar.length === 0 ? (
                      <div className="status" style={{ marginTop: 8 }}>
                        Headline scoring unavailable in this run.
                      </div>
                    ) : (
                      <div className="headlines radar-list" style={{ marginTop: 8 }}>
                        {radar.map((row, idx) => {
                          const title = typeof row.title === "string" ? row.title : "";
                          const source = typeof row.source === "string" ? row.source : "Signal";
                          const url = typeof row.url === "string" ? row.url : "";
                          const publishedAt = typeof row.publishedAt === "string" ? row.publishedAt : "";
                          const impact = row.impact === "high" || row.impact === "low" ? row.impact : "medium";
                          const direction = row.direction === "risk-up" || row.direction === "risk-down" ? row.direction : "neutral";
                          const horizon = row.horizon === "intraday" || row.horizon === "1m" ? row.horizon : "1w";
                          const related = asStringArray(row.relatedTickers);
                          return (
                            <a className="headline radar-item" key={`${title}-${idx}`} href={url || "#"} target={url ? "_blank" : undefined} rel={url ? "noreferrer" : undefined}>
                              <div className="radar-tags">
                                <span className={`severity ${impact}`}>{impact}</span>
                                <span className={`dir ${direction}`}>{direction}</span>
                                <span className="chip">{horizon}</span>
                                {related.map((ticker) => (
                                  <span className="chip" key={`${title}-${ticker}`}>
                                    {ticker}
                                  </span>
                                ))}
                              </div>
                              <div className="headline-title">{title}</div>
                              <div className="headline-meta">
                                {source}
                                {publishedAt ? ` · ${publishedAt.slice(0, 16)}` : ""}
                              </div>
                            </a>
                          );
                        })}
                      </div>
                    )}
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Macro Headlines</h3>
                    {topHeadlines.length === 0 ? (
                      <div className="status" style={{ marginTop: 8 }}>
                        No macro headlines were available in this run.
                      </div>
                    ) : (
                      <div className="headlines" style={{ marginTop: 8 }}>
                        {topHeadlines.map((item) => (
                          <a className="headline" key={`${item.url}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                            <div className="headline-title">{item.title}</div>
                            <div className="headline-meta">
                              {item.source}
                              {item.published_at ? ` · ${item.published_at.slice(0, 16)}` : ""}
                            </div>
                          </a>
                        ))}
                      </div>
                    )}
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Ticker Headlines</h3>
                    {tickerHeadlineGroups.length === 0 ? (
                      <div className="status" style={{ marginTop: 8 }}>
                        No ticker-specific headlines were available in this run.
                      </div>
                    ) : (
                      <div className="grid two" style={{ marginTop: 8 }}>
                        {tickerHeadlineGroups.map(({ ticker, items }) => (
                          <div key={ticker}>
                            <h4 style={{ margin: "0 0 8px 0" }}>{ticker}</h4>
                            <div className="headlines">
                              {items.slice(0, 4).map((item) => (
                                <a className="headline" key={`${item.url}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                                  <div className="headline-title">{item.title}</div>
                                  <div className="headline-meta">
                                    {item.source}
                                    {item.published_at ? ` · ${item.published_at.slice(0, 16)}` : ""}
                                  </div>
                                </a>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </section>

                  <section className="panel" style={{ marginTop: 14 }}>
                    <h3>Recent SEC Filings</h3>
                    {secHeadlines.length === 0 ? (
                      <div className="status" style={{ marginTop: 8 }}>
                        No recent SEC filings were available in this run.
                      </div>
                    ) : (
                      <div className="headlines" style={{ marginTop: 8 }}>
                        {secHeadlines.map((item) => (
                          <a className="headline" key={`${item.title}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                            <div className="headline-title">{item.title}</div>
                            <div className="headline-meta">
                              {item.source}
                              {item.published_at ? ` · ${item.published_at.slice(0, 10)}` : ""}
                            </div>
                          </a>
                        ))}
                      </div>
                    )}
                  </section>
                </>
              )}
            </div>

            <aside className="panel news-rail">
              <h3>News & Events</h3>
              <div className="rail-tabs">
                <button className={`rail-tab ${railTab === "macro" ? "active" : ""}`} onClick={() => setRailTab("macro")}>
                  Macro ({railCounts.macro})
                </button>
                <button className={`rail-tab ${railTab === "ticker" ? "active" : ""}`} onClick={() => setRailTab("ticker")}>
                  Holdings ({railCounts.ticker})
                </button>
                <button className={`rail-tab ${railTab === "sec" ? "active" : ""}`} onClick={() => setRailTab("sec")}>
                  SEC ({railCounts.sec})
                </button>
              </div>

              {railTab === "macro" && (
                <div className="headlines" style={{ marginTop: 8 }}>
                  {topHeadlines.length === 0 ? (
                    <div className="status">No macro headlines available.</div>
                  ) : (
                    topHeadlines.slice(0, 6).map((item) => (
                      <a className="headline" key={`${item.url}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                        <div className="headline-title">{item.title}</div>
                        <div className="headline-meta">
                          {item.source}
                          {item.published_at ? ` · ${item.published_at.slice(0, 16)}` : ""}
                        </div>
                      </a>
                    ))
                  )}
                </div>
              )}

              {railTab === "ticker" && (
                <div className="headlines" style={{ marginTop: 8 }}>
                  {tickerHeadlineGroups.length === 0 ? (
                    <div className="status">No holdings headlines available.</div>
                  ) : (
                    tickerHeadlineGroups.slice(0, 3).map(({ ticker, items }) => (
                      <div key={ticker}>
                        <h4 style={{ margin: "0 0 6px 0" }}>{ticker}</h4>
                        <div className="headlines">
                          {items.slice(0, 3).map((item) => (
                            <a className="headline" key={`${item.url}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                              <div className="headline-title">{item.title}</div>
                              <div className="headline-meta">
                                {item.source}
                                {item.published_at ? ` · ${item.published_at.slice(0, 16)}` : ""}
                              </div>
                            </a>
                          ))}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}

              {railTab === "sec" && (
                <div className="headlines" style={{ marginTop: 8 }}>
                  {secHeadlines.length === 0 ? (
                    <div className="status">No recent SEC filings available.</div>
                  ) : (
                    secHeadlines.slice(0, 6).map((item) => (
                      <a className="headline" key={`${item.title}-${item.published_at}`} href={item.url} target="_blank" rel="noreferrer">
                        <div className="headline-title">{item.title}</div>
                        <div className="headline-meta">
                          {item.source}
                          {item.published_at ? ` · ${item.published_at.slice(0, 10)}` : ""}
                        </div>
                      </a>
                    ))
                  )}
                </div>
              )}
            </aside>
          </section>
        </>
      )}
    </main>
  );
}
