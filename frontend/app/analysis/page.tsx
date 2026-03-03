"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { analyzePortfolio } from "@/lib/api";
import type { AnalysisResponse, Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";

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
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
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

export default function AnalysisPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [activeScenario, setActiveScenario] = useState("");
  const [scenarioScale, setScenarioScale] = useState(1);

  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      setLoading(false);
      setError("No positions found. Add positions first.");
      return;
    }
    try {
      setPositions(JSON.parse(raw) as Position[]);
    } catch {
      setLoading(false);
      setError("Invalid positions payload in local storage.");
    }
  }, []);

  useEffect(() => {
    if (positions.length === 0) return;
    let active = true;
    const run = async () => {
      try {
        setLoading(true);
        setError("");
        const response = await analyzePortfolio({ positions });
        if (active) setAnalysis(response);
      } catch (e) {
        if (active) setError((e as Error).message);
      } finally {
        if (active) setLoading(false);
      }
    };
    run();
    return () => {
      active = false;
    };
  }, [positions, refreshTick]);

  const topHeadlines = useMemo(() => analysis?.news?.macro?.slice(0, 6) || [], [analysis]);
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
  const providerEntries = providers ? Object.entries(providers) : [];
  const signals = asRecord(meta?.signals);
  const intelligence = asRecord(meta?.intelligence) || asRecord(signals?.pulse);
  const pulseThesis = typeof intelligence?.thesis === "string" ? intelligence.thesis : "";
  const pulseStance = intelligence?.stance === "risk-on" || intelligence?.stance === "risk-off" ? intelligence.stance : "balanced";
  const pulseFocus = asStringArray(intelligence?.focus).slice(0, 3);
  const warnings = asRecordArray(signals?.warnings);
  const watchouts = asRecordArray(signals?.watchouts);
  const radar = asRecordArray(signals?.radar);
  const scenarios = asRecordArray(signals?.scenarios);

  useEffect(() => {
    if (!scenarios.length) {
      setActiveScenario("");
      return;
    }
    const ids = scenarios
      .map((row) => (typeof row.id === "string" ? row.id : ""))
      .filter((id) => id.length > 0);
    if (ids.length && !ids.includes(activeScenario)) {
      setActiveScenario(ids[0]);
    }
  }, [scenarios, activeScenario]);

  const selectedScenario = scenarios.find((row) => row.id === activeScenario) || scenarios[0] || null;
  const scenarioExposed = selectedScenario ? asRecordArray(selectedScenario.exposed) : [];
  const movers = useMemo(
    () =>
      [...(analysis?.positions || [])]
        .filter((p) => p.chg_pct_1d !== null)
        .sort((a, b) => Math.abs(b.chg_pct_1d || 0) - Math.abs(a.chg_pct_1d || 0))
        .slice(0, 6),
    [analysis]
  );
  const scaledScenarioImpact = selectedScenario ? (asNumber(selectedScenario.portfolioImpactPct) || 0) * scenarioScale : null;

  return (
    <main className="container">
      <header className="topbar">
        <Link href="/portfolio" className="brand">
          <span className="brand-dot" />
          RiskPulse
        </Link>
        <div className="hero-meta" style={{ margin: 0 }}>
          <Link href="/portfolio" className="nav-link">
            Edit Portfolio
          </Link>
          <button className="btn secondary" onClick={() => setRefreshTick((n) => n + 1)} disabled={loading || !positions.length}>
            {loading ? "Refreshing..." : "Refresh Analysis"}
          </button>
        </div>
      </header>

      <section className="hero">
        <h1>Portfolio Risk Overview</h1>
        <p className="hero-sub">Provider-backed analysis of concentration, realized volatility proxies, macro regime, and market headlines.</p>
        {analysis && (
          <div className="hero-meta">
            <span className="pill">As of {analysis.as_of}</span>
            <span className="pill">Portfolio {money(analysis.portfolio_value)}</span>
            <span className="pill">Top5 {pct(analysis.top_concentration.top5Weight)}</span>
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
          {pulseFocus.length > 0 && (
            <div className="pulse-focus">
              {pulseFocus.map((item) => (
                <span className="chip" key={item}>
                  {item}
                </span>
              ))}
            </div>
          )}
        </section>
      )}

      {analysis && movers.length > 0 && (
        <section className="panel movers-panel">
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
        </section>
      )}

      {analysis && warnings.length > 0 && (
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

      {loading && <div className="status">Running analysis against live providers...</div>}
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
              <div className="kpi-label">Max Drawdown 120D</div>
              <div className="kpi-value">{pct(analysis.risk.maxDrawdown120d)}</div>
            </article>
            <article className="kpi">
              <div className="kpi-label">Data Quality</div>
              <div className="kpi-value">{dataQuality?.label || "-"}</div>
            </article>
          </section>

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
                      <div
                        className={`scenario-impact ${
                          (scaledScenarioImpact || 0) < 0 ? "neg" : "pos"
                        }`}
                      >
                        {pct(scaledScenarioImpact)}
                      </div>
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
              <h3>Model Notes</h3>
              <div className="notes">
                {analysis.notes.map((note) => (
                  <div className="note" key={note}>
                    {note}
                  </div>
                ))}
              </div>
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
                    <a
                      className="headline radar-item"
                      key={`${title}-${idx}`}
                      href={url || "#"}
                      target={url ? "_blank" : undefined}
                      rel={url ? "noreferrer" : undefined}
                    >
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
                      {item.published_at ? ` · ${item.published_at.slice(0, 10)}` : ""}
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
    </main>
  );
}
