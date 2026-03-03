"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import type { Position } from "@/lib/types";

const STORAGE_KEY = "riskpulse_positions";

export default function PortfolioPage() {
  const router = useRouter();
  const [positions, setPositions] = useState<Position[]>([]);
  const [ticker, setTicker] = useState("");
  const [qty, setQty] = useState("1");

  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    try {
      const parsed = JSON.parse(raw) as Position[];
      setPositions(parsed);
    } catch {
      setPositions([]);
    }
  }, []);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
  }, [positions]);

  const totalQty = useMemo(() => positions.reduce((sum, p) => sum + p.qty, 0), [positions]);

  const addPosition = () => {
    const symbol = ticker.toUpperCase().trim();
    const parsedQty = Number(qty);
    if (!symbol || Number.isNaN(parsedQty) || parsedQty <= 0) return;
    setPositions((prev) => [...prev, { ticker: symbol, qty: parsedQty, asset_type: "stock" }]);
    setTicker("");
    setQty("1");
  };

  const removePosition = (index: number) => {
    setPositions((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <main className="container">
      <h1>RiskPulse Portfolio</h1>
      <p className="muted">Build positions, then run one-click analysis using backend providers on Azure.</p>

      <section className="panel" style={{ marginBottom: 16 }}>
        <h3>Add Position</h3>
        <div className="row">
          <input value={ticker} onChange={(e) => setTicker(e.target.value)} placeholder="Ticker (e.g. MSFT)" />
          <input value={qty} onChange={(e) => setQty(e.target.value)} type="number" min="0" step="0.01" placeholder="Quantity" />
          <button onClick={addPosition}>Add</button>
          <button className="secondary" onClick={() => router.push("/analysis")} disabled={positions.length === 0}>
            Run Analysis
          </button>
        </div>
      </section>

      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>Current Positions</h3>
          <span className="muted">
            {positions.length} positions | total qty {totalQty.toFixed(2)}
          </span>
        </div>

        {positions.length === 0 ? (
          <p className="muted">No positions yet.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Quantity</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {positions.map((position, index) => (
                <tr key={`${position.ticker}-${index}`}>
                  <td>{position.ticker}</td>
                  <td>{position.qty}</td>
                  <td>
                    <button className="danger" onClick={() => removePosition(index)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
