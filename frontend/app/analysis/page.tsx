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

export default function AnalysisPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      setLoading(false);
      setError("No positions found. Add positions first.");
      return;
    }
    try {
      const parsed = JSON.parse(raw) as Position[];
      setPositions(parsed);
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
  }, [positions]);

  const topHeadlines = useMemo(() => analysis?.news?.macro?.slice(0, 5) || [], [analysis]);
  const dataQuality = (analysis?.meta?.dataQuality ||
    null) as null | { score?: number; label?: string; priceCoverage?: number; macroCoverage?: number };
  const quoteSources = (analysis?.meta?.quoteSources || null) as null | Record<string, string>;

  return (
    <main className="container">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h1>Portfolio Analysis</h1>
        <Link href="/portfolio">Back to Portfolio</Link>
      </div>

      {loading && <p className="muted">Running analysis...</p>}
      {error && <p style={{ color: "#b42318" }}>{error}</p>}

      {analysis && (
        <>
          <section className="panel" style={{ marginBottom: 16 }}>
            <h3>Summary</h3>
            <p>
              As of {analysis.as_of} | Portfolio Value: <strong>{money(analysis.portfolio_value)}</strong>
            </p>
            <p>
              Top5 Weight: <strong>{pct(analysis.top_concentration.top5Weight)}</strong>
            </p>
            <p>
              Vol60d: <strong>{pct(analysis.risk.vol60d)}</strong> | Vol120d: <strong>{pct(analysis.risk.vol120d)}</strong> |
              Max Drawdown120d: <strong>{pct(analysis.risk.maxDrawdown120d)}</strong>
            </p>
            {dataQuality && (
              <p>
                Data Quality: <strong>{dataQuality.label || "-"}</strong> ({pct(dataQuality.score)})
              </p>
            )}
            <div>
              {analysis.notes.map((note) => (
                <div key={note}>- {note}</div>
              ))}
            </div>
          </section>

          <section className="panel" style={{ marginBottom: 16 }}>
            <h3>Positions</h3>
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
                    <td>{position.ticker}</td>
                    <td>{position.qty}</td>
                    <td>{money(position.price)}</td>
                    <td>{money(position.value)}</td>
                    <td>{pct(position.weight)}</td>
                    <td>{pct(position.chg_pct_1d)}</td>
                    <td>{quoteSources?.[position.ticker] || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel" style={{ marginBottom: 16 }}>
            <h3>Macro Snapshot</h3>
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
                    <td>{name}</td>
                    <td>{point.value === null ? "-" : point.value.toFixed(3)}</td>
                    <td>{pct(point.chg_pct_1d)}</td>
                    <td>{bp(point.chg_bp_1d)}</td>
                    <td>{point.as_of || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="panel">
            <h3>Macro Headlines</h3>
            {topHeadlines.length === 0 ? (
              <p className="muted">No macro headlines returned from configured providers.</p>
            ) : (
              <ul>
                {topHeadlines.map((item) => (
                  <li key={`${item.url}-${item.published_at}`}>
                    <a href={item.url} target="_blank" rel="noreferrer">
                      {item.title}
                    </a>{" "}
                    <span className="muted">
                      ({item.source}
                      {item.published_at ? `, ${item.published_at.slice(0, 10)}` : ""})
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </main>
  );
}
