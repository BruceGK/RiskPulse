export type Position = {
  ticker: string;
  qty: number;
  asset_type?: string;
};

export type AnalysisRequest = {
  positions: Position[];
};

export type AnalysisResponse = {
  as_of: string;
  portfolio_value: number;
  positions: Array<{
    ticker: string;
    qty: number;
    price: number;
    value: number;
    weight: number;
    chg_pct_1d: number | null;
  }>;
  top_concentration: {
    top5Weight: number;
  };
  risk: {
    vol60d: number | null;
    vol120d: number | null;
    maxDrawdown120d: number | null;
  };
  macro: Record<
    string,
    {
      value: number | null;
      chg_pct_1d: number | null;
      chg_bp_1d: number | null;
      as_of: string | null;
    }
  >;
  news: Record<
    string,
    Array<{
      source: string;
      title: string;
      url: string;
      published_at: string | null;
      sentiment_hint: string | null;
    }>
  >;
  notes: string[];
  meta: Record<string, unknown>;
};

export type DailyBriefTicker = {
  ticker: string;
  score: number;
  move_1d: number | null;
  move_5d: number | null;
  technical_state: string;
  reason: string;
};

export type DailyBriefResponse = {
  as_of: string;
  generated_at: string;
  universe: string[];
  selected: DailyBriefTicker[];
  headline: string;
  thesis: string;
  agenda: string[];
  analysis: AnalysisResponse;
};

export type AgentSetup = {
  ticker: string;
  setup: string;
  action: string;
  bucket: string;
  score: number;
  confidence: number;
  urgency: string;
  time_horizon: string;
  why_now: string;
  confirm_if: string;
  invalidate_if: string;
  evidence: Record<string, unknown>;
  tags: string[];
  memory: Record<string, unknown>;
};

export type AgentResponse = {
  as_of: string;
  generated_at: string;
  headline: string;
  thesis: string;
  market_state: Record<string, unknown>;
  priorities: string[];
  setups: AgentSetup[];
  confirmed_entries: AgentSetup[];
  watchlist: AgentSetup[];
  trim_risks: AgentSetup[];
  avoid: AgentSetup[];
  source_daily_brief: DailyBriefResponse;
};
