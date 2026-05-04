"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

type NavKey = "analysis" | "daily" | "agent" | "builder";

type TerminalTopNavProps = {
  active: NavKey;
  children?: ReactNode;
  mode?: "risk" | "loss";
  section: string;
  status?: string;
};

const navItems: Array<{ key: NavKey; href: string; label: string }> = [
  { key: "analysis", href: "/analysis", label: "Analysis" },
  { key: "daily", href: "/daily", label: "Daily Desk" },
  { key: "agent", href: "/agent", label: "Agent" },
  { key: "builder", href: "/portfolio", label: "Builder" }
];

const SURFACE_MODE_KEY = "riskpulse_surface_mode";

export default function TerminalTopNav({
  active,
  children,
  mode = "risk",
  section,
  status
}: TerminalTopNavProps) {
  const isLoss = mode === "loss";
  const [surfaceMode, setSurfaceMode] = useState<"dark" | "light">("light");

  useEffect(() => {
    const saved = localStorage.getItem(SURFACE_MODE_KEY);
    const nextMode =
      saved === "dark" || saved === "light"
        ? saved
        : document.body.classList.contains("surface-light")
          ? "light"
          : "dark";
    setSurfaceMode(nextMode);
    document.body.classList.toggle("surface-light", nextMode === "light");
  }, []);

  const updateSurfaceMode = (nextMode: "dark" | "light") => {
    setSurfaceMode(nextMode);
    document.body.classList.toggle("surface-light", nextMode === "light");
    localStorage.setItem(SURFACE_MODE_KEY, nextMode);
  };

  const surfaceToggle = (
    <div className="terminal-surface-toggle" aria-label="Surface mode">
      <span className="terminal-surface-label">Theme</span>
      <button
        className={`terminal-surface-pill ${surfaceMode === "dark" ? "active" : ""}`}
        onClick={() => updateSurfaceMode("dark")}
        type="button"
      >
        Dark
      </button>
      <button
        className={`terminal-surface-pill ${surfaceMode === "light" ? "active" : ""}`}
        onClick={() => updateSurfaceMode("light")}
        type="button"
      >
        Light
      </button>
    </div>
  );

  return (
    <header className="terminal-topnav">
      <div className="terminal-topnav-left">
        <Link href="/analysis" className="terminal-brand-lockup" aria-label="Open RiskPulse analysis">
          <span className="terminal-brand-mark">{isLoss ? "LP" : "RP"}</span>
          <span className="terminal-brand-copy">
            <span className="terminal-brand-title">{isLoss ? "LossPulse" : "RiskPulse"}</span>
            <span className="terminal-brand-subtitle">{section}</span>
          </span>
        </Link>
        <nav className="terminal-nav-links" aria-label="Primary workspace navigation">
          {navItems.map((item) => (
            <Link
              aria-current={active === item.key ? "page" : undefined}
              className={`terminal-nav-link ${active === item.key ? "active" : ""}`}
              href={item.href}
              key={item.key}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </div>
      <div className="terminal-topnav-right">
        {status ? <div className={`terminal-context-chip ${isLoss ? "loss" : "risk"}`}>{status}</div> : null}
        {children ? (
          <div className="terminal-action-group">
            {surfaceToggle}
            {children}
          </div>
        ) : (
          surfaceToggle
        )}
      </div>
    </header>
  );
}
