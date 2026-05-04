"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { getDailyBrief } from "@/lib/api";
import type { DailyBriefResponse, Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(2)}%`;
}

function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function deskPositions(brief: DailyBriefResponse): Position[] {
  return brief.selected.map((row) => ({
    ticker: row.ticker,
    qty: 1,
    asset_type: ["SPY", "QQQ", "IWM", "SMH", "SOXX"].includes(row.ticker) ? "etf" : "stock",
  }));
}

export default function DailyDeskPage() {
  const [brief, setBrief] = useState<DailyBriefResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const loadBrief = async (force = false) => {
    if (force) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const next = await getDailyBrief(force);
      setBrief(next);
    } catch (e) {
      setError((e as Error).message || "Daily desk unavailable");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadBrief(false);
  }, []);

  const topPositions = useMemo(() => brief?.analysis.positions.slice(0, 6) || [], [brief]);
  const tickerIntel = useMemo(() => {
    const intelligence = brief?.analysis.meta?.intelligence;
    if (!intelligence || typeof intelligence !== "object" || Array.isArray(intelligence)) return [];
    const rows = (intelligence as Record<string, unknown>).tickerIntel;
    return Array.isArray(rows) ? rows.slice(0, 6) : [];
  }, [brief]);

  const applyToPortfolio = () => {
    if (!brief) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(deskPositions(brief)));
    window.location.href = "/analysis";
  };

  return (
    <div className="terminal-app terminal-risk">
      <header className="terminal-topnav">
        <div className="terminal-topnav-left">
          <Link href="/analysis" className="terminal-wordmark">
            Portfolio Intelligence
          </Link>
          <nav className="terminal-nav-links">
            <Link className="terminal-nav-link active" href="/daily">
              Daily Desk
            </Link>
            <Link className="terminal-nav-link" href="/analysis">
              Portfolio Analysis
            </Link>
            <Link className="terminal-nav-link" href="/portfolio">
              Builder
            </Link>
          </nav>
        </div>
        <div className="terminal-topnav-right">
          <button className="btn secondary terminal-top-action" onClick={() => loadBrief(true)} disabled={refreshing}>
            {refreshing ? "Refreshing..." : "Refresh Desk"}
          </button>
          <button className="btn primary terminal-top-action" onClick={applyToPortfolio} disabled={!brief?.selected?.length}>
            Analyze This Basket
          </button>
        </div>
      </header>

      <aside className="terminal-sidebar">
        <div className="terminal-sidebar-brand">
          <div className="terminal-sidebar-icon">DD</div>
          <div>
            <div className="terminal-sidebar-title">Daily Desk</div>
            <div className="terminal-sidebar-meta">auto watchlist</div>
          </div>
        </div>
        <nav className="terminal-sidebar-nav">
          <div className="terminal-sidebar-item active">Today</div>
          <div className="terminal-sidebar-item">Watchlist</div>
          <div className="terminal-sidebar-item">Agenda</div>
          <div className="terminal-sidebar-item">Runbook</div>
        </nav>
        <Link href="/analysis" className="terminal-sidebar-cta">
          Portfolio Terminal
        </Link>
      </aside>

      <main className="container terminal-page">
        {error && <div className="status error">Daily desk unavailable: {error}</div>}
        <section className="daily-hero">
          <div>
            <div className="terminal-overline">Daily Analyst Desk</div>
            <h1>{brief?.headline || "Building today’s tape map..."}</h1>
            <p>
              {brief?.thesis ||
                "RiskPulse automatically chooses the tickers worth discussing today, then runs the same confluence engine used by the portfolio terminal."}
            </p>
            <div className="daily-desk-actions">
              <button className="btn primary" onClick={applyToPortfolio} disabled={!brief?.selected?.length}>
                Analyze This Basket
              </button>
              <button className="btn secondary" onClick={() => loadBrief(true)} disabled={refreshing || loading}>
                {refreshing ? "Refreshing..." : "Force New Run"}
              </button>
              {brief && <span className="signal-meta">Generated {new Date(brief.generated_at).toLocaleString()}</span>}
            </div>
          </div>
          <div className="daily-hero-metrics">
            <article>
              <span>Selected</span>
              <strong>{brief?.selected.length || 0}</strong>
            </article>
            <article>
              <span>Universe</span>
              <strong>{brief?.universe.length || 0}</strong>
            </article>
            <article>
              <span>Desk Basket</span>
              <strong>{money(brief?.analysis.portfolio_value)}</strong>
            </article>
          </div>
        </section>

        <section className="daily-grid">
          <div className="terminal-section daily-section-main">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Today’s selected stocks</span>
                <h3>Watchlist Automation</h3>
              </div>
            </div>
            <div className="daily-desk-board daily-page-board">
              {(brief?.selected || []).map((row) => (
                <article className="daily-desk-ticker" key={row.ticker}>
                  <div className="daily-desk-ticker-top">
                    <strong>{row.ticker}</strong>
                    <span>{row.score.toFixed(2)}</span>
                  </div>
                  <div className="daily-desk-moves">
                    <span>1D {pct(row.move_1d)}</span>
                    <span>5D {pct(row.move_5d)}</span>
                  </div>
                  <p>{row.reason}</p>
                </article>
              ))}
              {loading && (
                <article className="daily-desk-ticker skeleton">
                  <strong>Desk warming up</strong>
                  <p>Fetching quote moves, trend state, macro context, and selected ticker analysis.</p>
                </article>
              )}
            </div>
          </div>

          <aside className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Run agenda</span>
                <h3>What to Talk About</h3>
              </div>
            </div>
            <div className="daily-agenda-list">
              {(brief?.agenda || []).slice(0, 4).map((item) => (
                <div className="note" key={item}>
                  {item}
                </div>
              ))}
              {!brief?.agenda?.length && <div className="note">Waiting for the model agenda.</div>}
            </div>
          </aside>
        </section>

        <section className="terminal-section">
          <div className="section-heading">
            <div>
              <span className="panel-kicker">Existing model output</span>
              <h3>Desk Basket Readthrough</h3>
            </div>
            <Link href="/analysis" className="btn secondary">
              Open Full Terminal
            </Link>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Price</th>
                  <th>1D</th>
                  <th>Weight</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {topPositions.map((row) => (
                  <tr key={row.ticker}>
                    <td>{row.ticker}</td>
                    <td>{money(row.price)}</td>
                    <td>{pct(row.chg_pct_1d)}</td>
                    <td>{pct(row.weight)}</td>
                    <td>{String(brief?.analysis.meta?.quoteSources && (brief.analysis.meta.quoteSources as Record<string, unknown>)[row.ticker] || "-")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {tickerIntel.length > 0 && (
          <section className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Confluence layer</span>
                <h3>Model Notes</h3>
              </div>
            </div>
            <div className="daily-model-notes">
              {tickerIntel.map((raw, idx) => {
                const row = raw as Record<string, unknown>;
                const ticker = String(row.ticker || `row-${idx}`);
                const action = String(row.actionBias || "watch");
                const analystRead = row.analystRead && typeof row.analystRead === "object" ? (row.analystRead as Record<string, unknown>) : {};
                return (
                  <article className="intel-info-card" key={ticker}>
                    <div className="daily-desk-ticker-top">
                      <strong>{ticker}</strong>
                      <span>{action}</span>
                    </div>
                    <p className="signal-text">{String(analystRead.thesis || row.rationale || "No model note for this run.")}</p>
                  </article>
                );
              })}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
