export const STORAGE_KEY = "riskpulse_positions";

export const ETF_TICKERS = ["SPY", "QQQ", "IWM", "SMH", "SOXX"] as const;

export function assetTypeFor(ticker: string): "etf" | "stock" {
  return (ETF_TICKERS as readonly string[]).includes(ticker) ? "etf" : "stock";
}
