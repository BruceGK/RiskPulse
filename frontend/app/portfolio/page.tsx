"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import type { Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";
const THEME_MODE_KEY = "riskpulse_theme_mode";

const SAMPLE: Position[] = [
  { ticker: "AAPL", qty: 8, asset_type: "stock" },
  { ticker: "MSFT", qty: 5, asset_type: "stock" },
  { ticker: "SPY", qty: 6, asset_type: "etf" }
];

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export default function PortfolioPage() {
  const router = useRouter();
  const [positions, setPositions] = useState<Position[]>([]);
  const [ticker, setTicker] = useState("");
  const [qty, setQty] = useState("1");
  const [lossMode, setLossMode] = useState(false);

  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    try {
      setPositions(JSON.parse(raw) as Position[]);
    } catch {
      setPositions([]);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
  }, [positions]);

  useEffect(() => {
    const savedThemeMode = localStorage.getItem(THEME_MODE_KEY);
    if (savedThemeMode === "losspulse") {
      setLossMode(true);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem(THEME_MODE_KEY, lossMode ? "losspulse" : "riskpulse");
  }, [lossMode]);

  const totals = useMemo(() => {
    const totalQty = positions.reduce((sum, p) => sum + p.qty, 0);
    const unique = new Set(positions.map((p) => p.ticker)).size;
    return { totalQty, unique };
  }, [positions]);
  const riskExposure = useMemo(() => {
    if (positions.length >= 6 || totals.unique >= 4) return "High";
    if (positions.length >= 3 || totals.unique >= 2) return "Moderate";
    return "Low";
  }, [positions.length, totals.unique]);

  const satire = useMemo(() => {
    if (positions.length === 0 || totals.totalQty <= 0) {
      return { regretPotential: 0, fomoLoad: 0, bagConcentration: 0 };
    }
    const maxQty = positions.reduce((m, p) => Math.max(m, p.qty), 0);
    const techNames = new Set(["AAPL", "MSFT", "NVDA", "TSLA", "META", "AMD", "COIN", "PLTR"]);
    const techCount = positions.reduce((sum, p) => sum + (techNames.has(p.ticker) ? 1 : 0), 0);
    const bagConcentration = clamp01(maxQty / totals.totalQty);
    const fomoLoad = clamp01(techCount / positions.length);
    const regretPotential = clamp01((bagConcentration * 0.62) + (fomoLoad * 0.38));
    return { regretPotential, fomoLoad, bagConcentration };
  }, [positions, totals.totalQty]);

  const addPosition = () => {
    const symbol = ticker.toUpperCase().trim();
    const parsedQty = Number(qty);
    if (!symbol || Number.isNaN(parsedQty) || parsedQty <= 0) return;
    setPositions((prev) => [...prev, { ticker: symbol, qty: parsedQty, asset_type: "stock" }]);
    setTicker("");
    setQty("1");
  };

  const useSamples = () => setPositions(SAMPLE);
  const clearAll = () => setPositions([]);
  const seriousPrimaryNav = ["Dashboard", "Risk Analysis", "Strategies", "History"];
  const sidebarItems = lossMode
    ? ["Overview", "Terminal", "Builder", "Doom Scenarios", "Reports"]
    : ["Overview", "Terminal", "Builder", "Signals", "Reports"];

  return (
    <div className={`terminal-app ${lossMode ? "terminal-loss" : "terminal-risk"}`}>
      <header className="terminal-topnav">
        <div className="terminal-topnav-left">
          <div className="terminal-wordmark">{lossMode ? "LossPulse Intelligence" : "Portfolio Intelligence"}</div>
          <nav className="terminal-nav-links">
            {seriousPrimaryNav.map((item) => (
              <span className={`terminal-nav-link ${item === "Dashboard" ? "active" : ""}`} key={item}>
                {item}
              </span>
            ))}
          </nav>
        </div>
        <div className="terminal-topnav-right">
          <div className="terminal-search-shell">
            <span className="terminal-search-icon">⌕</span>
            <input className="terminal-search-input" placeholder="Search Terminal..." aria-label="Search terminal" />
          </div>
          <div className={`terminal-mode-badge ${lossMode ? "loss" : "risk"}`}>
            {lossMode ? "LossPulse Pro Active" : "Pro View Active"}
          </div>
          <div className="terminal-theme-toggle">
            <button
              className={`terminal-theme-pill ${!lossMode ? "active" : ""}`}
              onClick={() => setLossMode(false)}
              type="button"
            >
              Serious
            </button>
            <button
              className={`terminal-theme-pill ${lossMode ? "active" : ""}`}
              onClick={() => setLossMode(true)}
              type="button"
            >
              Satirical
            </button>
          </div>
          <button className="btn secondary terminal-top-action" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
            {lossMode ? "Open Doom Desk" : "Open Analysis"}
          </button>
          <button className="btn secondary terminal-top-action" onClick={() => router.push("/agent")}>
            Agent
          </button>
        </div>
      </header>

      <aside className="terminal-sidebar">
        <div className="terminal-sidebar-brand">
          <div className="terminal-sidebar-icon">{lossMode ? "LP" : "RP"}</div>
          <div>
            <div className="terminal-sidebar-title">Pulse Terminal</div>
            <div className="terminal-sidebar-meta">{lossMode ? "v2.4.0 pro destruction" : "v2.4.0 active"}</div>
          </div>
        </div>
        <nav className="terminal-sidebar-nav">
          {sidebarItems.map((item) => (
            <div className={`terminal-sidebar-item ${item === "Builder" ? "active" : ""}`} key={item}>
              {item}
            </div>
          ))}
        </nav>
        <button className="terminal-sidebar-cta">{lossMode ? "Incinerate Capital" : "Deploy Capital"}</button>
        <div className="terminal-sidebar-footer">
          <div className="terminal-sidebar-item small">Help</div>
          <div className="terminal-sidebar-item small">Sign Out</div>
        </div>
      </aside>

      <main className={`container terminal-page ${lossMode ? "losspulse" : ""}`}>
        <section className="terminal-page-header">
          <div>
            <div className="terminal-overline">{lossMode ? "Bag Builder" : "Portfolio Builder"}</div>
            <h1 className="terminal-page-title">
              {lossMode ? "Build your bag and optimize regret." : "Build your portfolio inputs and launch the risk engine."}
            </h1>
            <p className="terminal-page-subtitle">
              {lossMode
                ? "Route your portfolio through pure chaos analytics, panic drivers, and anti-signals."
                : "Define capital allocation, sector exposure, and launch a full portfolio intelligence pass."}
            </p>
            <p className="hero-sub research-note">
              Analysis framework backed by research from{" "}
              <a href="https://shitjournal.org" target="_blank" rel="noreferrer" className="research-link">
                S.H.I.T Journal
              </a>
              .
            </p>
          </div>
          <div className="terminal-page-value">
            <div className="terminal-overline">{lossMode ? "Total Regret Notional" : "Total Equity Value"}</div>
            <div className="terminal-page-value-number">{totals.totalQty.toFixed(2)}</div>
          </div>
        </section>

        <section className="builder-layout">
          <article className="panel builder-panel slide-up">
            <div className="terminal-overline">{lossMode ? "Quick add bag" : "Quick add position"}</div>
            <div className="builder-form-row">
              <div className="builder-field">
                <label className="builder-label">Ticker Symbol</label>
                <input
                  className="text-input"
                  value={ticker}
                  onChange={(e) => setTicker(e.target.value)}
                  placeholder={lossMode ? "Search (e.g. NVDA, COIN, SPY)" : "Search (e.g. NVDA, BTC, SPY)"}
                />
              </div>
              <div className="builder-field builder-field-sm">
                <label className="builder-label">Quantity</label>
                <input
                  className="text-input"
                  value={qty}
                  onChange={(e) => setQty(e.target.value)}
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="0.00"
                />
              </div>
              <button className="btn primary builder-submit" onClick={addPosition}>
                {lossMode ? "Buy The Top" : "Add"}
              </button>
            </div>
          </article>

          <article className="panel summary-panel slide-up">
            <div className="summary-row">
              <span>Active Positions</span>
              <strong>{positions.length}</strong>
            </div>
            <div className="summary-row">
              <span>Unique Tickers</span>
              <strong>{totals.unique}</strong>
            </div>
            <div className="summary-row">
              <span>{lossMode ? "Chaos Exposure" : "Risk Exposure"}</span>
              <div className="summary-meter-wrap">
                <div className="summary-meter">
                  <div
                    className="summary-meter-fill"
                    style={{ width: `${riskExposure === "High" ? 82 : riskExposure === "Moderate" ? 56 : 28}%` }}
                  />
                </div>
                <strong>{riskExposure}</strong>
              </div>
            </div>
            <div className="summary-actions">
              <button className="btn primary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
                {lossMode ? "Run Destruction Analysis" : "Launch Portfolio Analysis"}
              </button>
              <button className="btn secondary" onClick={useSamples}>
                {lossMode ? "Load Doom Basket" : "Load Sample"}
              </button>
              <button className="btn danger" onClick={clearAll} disabled={positions.length === 0}>
                {lossMode ? "Nuke Portfolio" : "Clear"}
              </button>
            </div>
          </article>
        </section>

        {lossMode && (
          <section className="panel loss-portfolio-panel terminal-section">
            <div className="terminal-section-head">
              <h3>Capital Destruction Dashboard</h3>
            </div>
            <div className="grid cards" style={{ marginTop: 10 }}>
              <article className="kpi">
                <div className="kpi-label">Regret Potential</div>
                <div className="kpi-value">{(satire.regretPotential * 100).toFixed(1)}%</div>
              </article>
              <article className="kpi">
                <div className="kpi-label">FOMO Load</div>
                <div className="kpi-value">{(satire.fomoLoad * 100).toFixed(1)}%</div>
              </article>
              <article className="kpi">
                <div className="kpi-label">Bag Concentration</div>
                <div className="kpi-value">{(satire.bagConcentration * 100).toFixed(1)}%</div>
              </article>
              <article className="kpi">
                <div className="kpi-label">Strategy Quality</div>
                <div className="kpi-value">
                  {satire.regretPotential > 0.7 ? "catastrophic" : satire.regretPotential > 0.45 ? "questionable" : "surprisingly decent"}
                </div>
              </article>
            </div>
          </section>
        )}

        <section className="panel terminal-section">
          <div className="terminal-section-head">
            <h3>{lossMode ? "Current Bags" : "Current Positions"}</h3>
            <div className="table-actions">
              <button className="mini-btn">Filter</button>
              <button className="mini-btn">Export</button>
            </div>
          </div>
          {positions.length === 0 ? (
            <div className="status" style={{ marginTop: 10 }}>
              {lossMode ? "No bags yet. Add symbols above or load a regret basket." : "No positions yet. Add symbols above or load sample data."}
            </div>
          ) : (
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>Asset Ticker</th>
                    <th>Holdings</th>
                    <th>Allocation</th>
                    <th>Asset</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((position, index) => (
                    <tr key={`${position.ticker}-${index}`}>
                      <td className="mono">{position.ticker}</td>
                      <td>{position.qty.toFixed(2)}</td>
                      <td>{totals.totalQty > 0 ? `${((position.qty / totals.totalQty) * 100).toFixed(1)}%` : "-"}</td>
                      <td>{position.asset_type || "stock"}</td>
                      <td>
                        <button className="btn danger" onClick={() => setPositions((prev) => prev.filter((_, i) => i !== index))}>
                          {lossMode ? "Dump" : "Remove"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="terminal-cta panel">
          <div>
            <h3>{lossMode ? "Ready To Accelerate Losses?" : "Ready to Stress-Test?"}</h3>
            <p className="muted">
              {lossMode
                ? "Run a deeper destruction pass against this portfolio and surface panic catalysts, doom scenarios, and anti-signals."
                : "Simulate market crashes, rate hikes, and geopolitical events against this portfolio configuration."}
            </p>
          </div>
          <button className="btn primary terminal-cta-button" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
            {lossMode ? "Run Doom Analysis" : "Run Deep Analysis"}
          </button>
        </section>
      </main>
    </div>
  );
}
