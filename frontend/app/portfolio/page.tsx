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

  return (
    <main className={`container ${lossMode ? "losspulse" : ""}`}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          {lossMode ? "LossPulse" : "RiskPulse"}
        </div>
        <button className="btn secondary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
          {lossMode ? "Open Doom Desk" : "Open Analysis"}
        </button>
      </header>

      <section className="hero">
        <h1>{lossMode ? "Build your bag and optimize regret." : "Build your portfolio inputs and launch the risk engine."}</h1>
        <p className="hero-sub">
          {lossMode
            ? "Flip to LossPulse and route your portfolio through pure chaos analytics."
            : "Enter symbols, set quantities, and run a single API-driven pass for concentration, macro context, and headline risk."}
        </p>
        <p className="hero-sub research-note">
          Analysis framework backed by research from{" "}
          <a href="https://shitjournal.org" target="_blank" rel="noreferrer" className="research-link">
            S.H.I.T Journal
          </a>
          .
        </p>
        <div className="hero-meta">
          <span className="pill">{positions.length} lines</span>
          <span className="pill">{totals.unique} unique tickers</span>
          <span className="pill">{totals.totalQty.toFixed(2)} total quantity</span>
          <span className="pill">Mode {lossMode ? "LossPulse" : "RiskPulse"}</span>
        </div>
      </section>

      <section className="grid two">
        <article className="panel slide-up">
          <h3>{lossMode ? "Add Future Bagholder" : "Add Position"}</h3>
          <p className="muted" style={{ marginBottom: 12 }}>
            {lossMode ? "Symbols auto-normalize before financial self-sabotage." : "Symbols auto-normalize to uppercase."}
          </p>
          <div className="form-grid">
            <input
              className="text-input"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              placeholder="Ticker (e.g. NVDA)"
            />
            <input
              className="text-input"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              type="number"
              min="0"
              step="0.01"
              placeholder="Quantity"
            />
            <button className="btn primary" onClick={addPosition}>
              {lossMode ? "Buy The Top" : "Add"}
            </button>
            <button className="btn secondary" onClick={useSamples}>
              {lossMode ? "Load Doom Basket" : "Load Sample"}
            </button>
          </div>
        </article>

        <article className="panel slide-up">
          <h3>Session Controls</h3>
          <div className="theme-switch" style={{ marginTop: 10 }}>
            <button
              className={`switch-chip ${!lossMode ? "active" : ""}`}
              onClick={() => setLossMode(false)}
              type="button"
            >
              RiskPulse Theme
            </button>
            <button
              className={`switch-chip ${lossMode ? "active" : ""}`}
              onClick={() => setLossMode(true)}
              type="button"
            >
              LossPulse Theme
            </button>
          </div>
          <div className="notes" style={{ marginTop: 8 }}>
            <div className="note">
              {lossMode
                ? "Run analysis to score your regret velocity, concentration pain, and headline-induced panic."
                : "Run analysis after adding positions to fetch market, macro, and news context."}
            </div>
            <div className="note">
              {lossMode
                ? "Data is stored in browser local storage so your bad ideas survive refresh."
                : "Data is stored in browser local storage for quick iteration."}
            </div>
            {lossMode && <div className="note">Zero guardrails mode enabled. Confidence optional.</div>}
          </div>
          <div className="hero-meta" style={{ marginTop: 14 }}>
            <button className="btn primary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
              {lossMode ? "Run Loss Analysis" : "Run Analysis"}
            </button>
            <button className="btn danger" onClick={clearAll} disabled={positions.length === 0}>
              {lossMode ? "Nuke Portfolio" : "Clear"}
            </button>
          </div>
        </article>
      </section>

      {lossMode && (
        <section className="panel loss-portfolio-panel" style={{ marginTop: 14 }}>
          <h3>Capital Destruction Dashboard</h3>
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
          <div className="notes" style={{ marginTop: 10 }}>
            <div className="note">Suggested process: buy euphoric spikes, deny risk, then panic-sell drawdowns.</div>
            <div className="note">Inverse this playbook for actual investing discipline.</div>
          </div>
        </section>
      )}

      <section className="panel" style={{ marginTop: 14 }}>
        <h3>{lossMode ? "Current Bags" : "Current Positions"}</h3>
        {positions.length === 0 ? (
          <div className="status" style={{ marginTop: 10 }}>
            {lossMode ? "No bags yet. Add symbols above or load a prebuilt regret basket." : "No positions yet. Add symbols above or load sample data."}
          </div>
        ) : (
          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>Ticker</th>
                  <th>Quantity</th>
                  <th>Asset</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {positions.map((position, index) => (
                  <tr key={`${position.ticker}-${index}`}>
                    <td className="mono">{position.ticker}</td>
                    <td>{position.qty.toFixed(2)}</td>
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
    </main>
  );
}
