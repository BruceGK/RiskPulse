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
