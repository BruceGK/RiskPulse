"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import type { Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";

const SAMPLE: Position[] = [
  { ticker: "AAPL", qty: 8, asset_type: "stock" },
  { ticker: "MSFT", qty: 5, asset_type: "stock" },
  { ticker: "SPY", qty: 6, asset_type: "etf" }
];

export default function PortfolioPage() {
  const router = useRouter();
  const [positions, setPositions] = useState<Position[]>([]);
  const [ticker, setTicker] = useState("");
  const [qty, setQty] = useState("1");

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

  const totals = useMemo(() => {
    const totalQty = positions.reduce((sum, p) => sum + p.qty, 0);
    const unique = new Set(positions.map((p) => p.ticker)).size;
    return { totalQty, unique };
  }, [positions]);

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
    <main className="container">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          RiskPulse
        </div>
        <button className="btn secondary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
          Open Analysis
        </button>
      </header>

      <section className="hero">
        <h1>Build your portfolio inputs and launch the risk engine.</h1>
        <p className="hero-sub">
          Enter symbols, set quantities, and run a single API-driven pass for concentration, macro context, and headline risk.
        </p>
        <div className="hero-meta">
          <span className="pill">{positions.length} lines</span>
          <span className="pill">{totals.unique} unique tickers</span>
          <span className="pill">{totals.totalQty.toFixed(2)} total quantity</span>
        </div>
      </section>

      <section className="grid two">
        <article className="panel slide-up">
          <h3>Add Position</h3>
          <p className="muted" style={{ marginBottom: 12 }}>
            Symbols auto-normalize to uppercase.
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
              Add
            </button>
            <button className="btn secondary" onClick={useSamples}>
              Load Sample
            </button>
          </div>
        </article>

        <article className="panel slide-up">
          <h3>Session Controls</h3>
          <div className="notes" style={{ marginTop: 8 }}>
            <div className="note">Run analysis after adding positions to fetch market, macro, and news context.</div>
            <div className="note">Data is stored in browser local storage for quick iteration.</div>
          </div>
          <div className="hero-meta" style={{ marginTop: 14 }}>
            <button className="btn primary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
              Run Analysis
            </button>
            <button className="btn danger" onClick={clearAll} disabled={positions.length === 0}>
              Clear
            </button>
          </div>
        </article>
      </section>

      <section className="panel" style={{ marginTop: 14 }}>
        <h3>Current Positions</h3>
        {positions.length === 0 ? (
          <div className="status" style={{ marginTop: 10 }}>
            No positions yet. Add symbols above or load sample data.
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
                        Remove
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
