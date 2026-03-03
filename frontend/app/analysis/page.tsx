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

export default function AnalysisPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);

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
  const topPositions = useMemo(() => analysis?.positions?.slice(0, 5) || [], [analysis]);
  const dataQuality = (analysis?.meta?.dataQuality ||
    null) as null | { score?: number; label?: string; priceCoverage?: number; macroCoverage?: number; macroNewsCount?: number };
  const quoteSources = (analysis?.meta?.quoteSources || null) as null | Record<string, string>;
  const providers = asRecord(analysis?.meta?.providers);
  const providerEntries = providers ? Object.entries(providers) : [];

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
        </>
      )}
    </main>
  );
}
