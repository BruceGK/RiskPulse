"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import TerminalTopNav from "@/app/components/TerminalTopNav";
import { getInvestmentAgent, peekInvestmentAgent } from "@/lib/api";
import { STORAGE_KEY, assetTypeFor } from "@/lib/constants";
import { label, num, pct as pctBase } from "@/lib/format";
import type { AgentResponse, AgentSetup, Position } from "@/lib/types";

const pct = (value: number | null | undefined) => pctBase(value, 1);

function setupPositions(agent: AgentResponse): Position[] {
  return agent.source_daily_brief.selected.map((row) => ({
    ticker: row.ticker,
    qty: 1,
    asset_type: assetTypeFor(row.ticker),
  }));
}

function bucketTitle(bucket: string): string {
  switch (bucket) {
    case "confirmed-entry":
      return "Confirmed Entry";
    case "wait-confirmation":
      return "Waiting for Trigger";
    case "trim-risk":
      return "Trim / Hedge Risk";
    case "avoid":
      return "Avoid New Risk";
    default:
      return "Watch";
  }
}

function SetupCard({ setup }: { setup: AgentSetup }) {
  const confluence = num(setup.evidence.confluence);
  const opportunity = num(setup.evidence.opportunity);
  const distribution = num(setup.evidence.distribution);
  const change = typeof setup.memory.change === "string" ? setup.memory.change : "new";
  return (
    <article className={`agent-setup-card ${setup.bucket}`}>
      <div className="agent-setup-head">
        <div>
          <span>{bucketTitle(setup.bucket)}</span>
          <strong>{setup.ticker}</strong>
        </div>
        <div className="agent-score">{Math.round(setup.score * 100)}</div>
      </div>
      <div className="agent-setup-meta">
        <span>{label(setup.action)}</span>
        <span>{setup.urgency}</span>
        <span>{setup.time_horizon}</span>
        <span>{change}</span>
      </div>
      <p>{setup.why_now}</p>
      <div className="agent-evidence-strip">
        <span>Opp {pct(opportunity)}</span>
        <span>Dist {pct(distribution)}</span>
        <span>Conf {pct(setup.confidence)}</span>
        <span>Signal {confluence === null ? "-" : confluence.toFixed(1)}</span>
      </div>
      <div className="agent-trigger-grid">
        <div>
          <span>Confirms if</span>
          <p>{setup.confirm_if}</p>
        </div>
        <div>
          <span>Invalidates if</span>
          <p>{setup.invalidate_if}</p>
        </div>
      </div>
      <div className="agent-tags">
        {setup.tags.slice(0, 5).map((tag) => (
          <span key={tag}>{label(tag)}</span>
        ))}
      </div>
    </article>
  );
}

export default function InvestmentAgentPage() {
  const router = useRouter();
  // Hydrate from session cache so navigating back to this page is instant.
  const [agent, setAgent] = useState<AgentResponse | null>(() => peekInvestmentAgent());
  const [loading, setLoading] = useState(() => !peekInvestmentAgent());
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const loadAgent = async (force = false) => {
    if (force || agent) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const next = await getInvestmentAgent(force);
      setAgent(next);
    } catch (e) {
      setError((e as Error).message || "Agent unavailable");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadAgent(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const allSetups = useMemo(() => agent?.setups || [], [agent]);
  const featured = allSetups[0] || null;
  const marketState = agent?.market_state || {};
  const downside5d = num(marketState.downside5d);
  const upside5d = num(marketState.upside5d);
  const panicScore = num(marketState.panicScore);
  const crowdingScore = num(marketState.crowdingScore);

  const applyBasket = () => {
    if (!agent) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(setupPositions(agent)));
    router.push("/analysis");
  };

  return (
    <div className="terminal-app terminal-risk">
      <TerminalTopNav active="agent" section="Opportunity Agent" status="Observe Mode">
        <button className="btn secondary terminal-top-action" onClick={() => loadAgent(true)} disabled={refreshing || loading}>
          {refreshing ? "Thinking..." : "Refresh"}
        </button>
        <button className="btn primary terminal-top-action" onClick={applyBasket} disabled={!agent}>
          Analyze Basket
        </button>
      </TerminalTopNav>

      <aside className="terminal-sidebar">
        <div className="terminal-sidebar-brand">
          <div className="terminal-sidebar-icon">AI</div>
          <div>
            <div className="terminal-sidebar-title">Investment Agent</div>
            <div className="terminal-sidebar-meta">observe · decide · remember</div>
          </div>
        </div>
        <nav className="terminal-sidebar-nav">
          <div className="terminal-sidebar-item active">Command</div>
          <div className="terminal-sidebar-item">Confirmed</div>
          <div className="terminal-sidebar-item">Watchlist</div>
          <div className="terminal-sidebar-item">Risk</div>
          <div className="terminal-sidebar-item">Memory</div>
        </nav>
        <Link href="/daily" className="terminal-sidebar-cta">
          Open Daily Desk
        </Link>
      </aside>

      <main className="container terminal-page">
        {error && <div className="status error">Agent unavailable: {error}</div>}

        <section className="agent-hero">
          <div>
            <div className="terminal-overline">Investment Assistant v1</div>
            <h1>{agent?.headline || "Warming the market brain..."}</h1>
            <p>
              {agent?.thesis ||
                "The agent watches the Daily Desk basket, converts signals into executable states, and tells you what must confirm before a setup is real."}
            </p>
            <div className="daily-desk-actions">
              <button className="btn primary" onClick={applyBasket} disabled={!agent}>
                Analyze Agent Basket
              </button>
              <button className="btn secondary" onClick={() => loadAgent(true)} disabled={refreshing || loading}>
                {refreshing ? "Refreshing..." : "Force Agent Run"}
              </button>
              {agent && <span className="signal-meta">Generated {new Date(agent.generated_at).toLocaleString()}</span>}
            </div>
          </div>
          <div className="agent-feature-card">
            <span>Top Setup</span>
            <strong>{featured?.ticker || (loading ? "Loading" : "None")}</strong>
            <p>{featured ? `${bucketTitle(featured.bucket)} · ${label(featured.setup)}` : "No setup has cleared the agent filter yet."}</p>
          </div>
        </section>

        <section className="terminal-stat-row agent-stat-row">
          <article className="terminal-stat-card">
            <div className="terminal-overline">5D Downside</div>
            <div className="terminal-stat-value danger">{pct(downside5d)}</div>
          </article>
          <article className="terminal-stat-card">
            <div className="terminal-overline">5D Upside</div>
            <div className="terminal-stat-value accent">{pct(upside5d)}</div>
          </article>
          <article className="terminal-stat-card">
            <div className="terminal-overline">Panic</div>
            <div className="terminal-stat-value">{pct(panicScore)}</div>
          </article>
          <article className="terminal-stat-card">
            <div className="terminal-overline">Crowding</div>
            <div className="terminal-stat-value">{pct(crowdingScore)}</div>
          </article>
        </section>

        <section className="agent-grid">
          <div className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Agent priorities</span>
                <h3>What Matters Now</h3>
              </div>
            </div>
            <div className="agent-priority-list">
              {(agent?.priorities || []).map((item) => (
                <div className="note" key={item}>{item}</div>
              ))}
              {!agent?.priorities?.length && <div className="note">Waiting for agent priorities.</div>}
            </div>
          </div>

          <div className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Market state</span>
                <h3>Agent Context</h3>
              </div>
            </div>
            <div className="agent-context-card">
              <strong>{String(marketState.regime || "balanced")}</strong>
              <p>{String(marketState.macroRead || "Macro context is not available yet.")}</p>
            </div>
          </div>
        </section>

        <section className="terminal-section">
          <div className="section-heading">
            <div>
              <span className="panel-kicker">Opportunity agent</span>
              <h3>Current Decisions</h3>
            </div>
          </div>
          <div className="agent-setup-grid">
            {allSetups.map((setup) => (
              <SetupCard setup={setup} key={`${setup.ticker}-${setup.bucket}`} />
            ))}
            {loading && (
              <article className="agent-setup-card watch">
                <div className="agent-setup-head">
                  <div>
                    <span>Initializing</span>
                    <strong>Agent</strong>
                  </div>
                  <div className="agent-score">...</div>
                </div>
                <p>Running Daily Desk observation, confluence scoring, and trigger classification.</p>
              </article>
            )}
          </div>
        </section>

        <section className="agent-grid">
          <div className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Confirmed entries</span>
                <h3>Do Not Chase, Execute</h3>
              </div>
            </div>
            <div className="agent-mini-list">
              {(agent?.confirmed_entries || []).map((setup) => (
                <div className="signal-item opportunity" key={setup.ticker}>
                  <div className="signal-head"><strong>{setup.ticker}</strong><span>{Math.round(setup.score * 100)}</span></div>
                  <div className="signal-text">{setup.confirm_if}</div>
                </div>
              ))}
              {!agent?.confirmed_entries?.length && <div className="status">No confirmed entry. This is useful: the agent is refusing to hallucinate a trade.</div>}
            </div>
          </div>

          <div className="terminal-section">
            <div className="section-heading">
              <div>
                <span className="panel-kicker">Risk control</span>
                <h3>Trim / Avoid</h3>
              </div>
            </div>
            <div className="agent-mini-list">
              {[...(agent?.trim_risks || []), ...(agent?.avoid || [])].slice(0, 6).map((setup) => (
                <div className="signal-item exit" key={`${setup.ticker}-${setup.bucket}`}>
                  <div className="signal-head"><strong>{setup.ticker}</strong><span>{label(setup.action)}</span></div>
                  <div className="signal-text">{setup.invalidate_if}</div>
                </div>
              ))}
              {!agent?.trim_risks?.length && !agent?.avoid?.length && <div className="status">No major trim or avoid signal in this agent run.</div>}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
