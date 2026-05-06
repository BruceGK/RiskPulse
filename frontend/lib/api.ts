import { cacheKey, clearCached, dedupe, getCached, setCached } from "@/lib/clientCache";
import type { AgentResponse, AnalysisRequest, AnalysisResponse, DailyBriefResponse } from "@/lib/types";

const RAW_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL;
const API_BASE_URL = RAW_API_BASE_URL ? RAW_API_BASE_URL.replace(/\/$/, "") : "";

type AnalyzeOptions = {
  phase?: "quick" | "full";
  /** Skip the cache and force a fresh fetch (e.g. user clicked Refresh). */
  force?: boolean;
};

// Cache TTLs match the backend's own caches so we don't refetch ahead of the
// upstream cache anyway. Stale-while-revalidate happens at the page layer.
const ANALYZE_TTL_MS = 90_000; // backend quote cache is 120s
const DAILY_TTL_MS = 5 * 60_000; // backend daily-brief cache is 6h, but UI shouldn't show 6h-old data silently
const AGENT_TTL_MS = 5 * 60_000;

async function postAnalyze(payload: AnalysisRequest, phase: "quick" | "full"): Promise<AnalysisResponse> {
  const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/analyze?phase=${phase}` : `/api/analyze?phase=${phase}`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`Backend error (${response.status})`);
  return (await response.json()) as AnalysisResponse;
}

export async function analyzePortfolio(payload: AnalysisRequest, options?: AnalyzeOptions): Promise<AnalysisResponse> {
  const phase = options?.phase || "full";
  const key = cacheKey(`analyze:${phase}`, payload);
  if (options?.force) clearCached(key);
  // dedupe stops double-fires (e.g. React StrictMode dev double-mount, or a
  // useEffect re-running before the in-flight request resolved).
  return dedupe(key, async () => {
    const data = await postAnalyze(payload, phase);
    setCached(key, data);
    return data;
  });
}

/** Returns a cached response if it's fresh, else null. The page layer can use
 *  this to render instantly while a fresh fetch runs in the background. */
export function peekAnalyze(payload: AnalysisRequest, phase: "quick" | "full" = "full"): AnalysisResponse | null {
  const key = cacheKey(`analyze:${phase}`, payload);
  const cached = getCached<AnalysisResponse>(key);
  return cached && cached.ageMs <= ANALYZE_TTL_MS ? cached.data : null;
}

export async function getDailyBrief(force = false): Promise<DailyBriefResponse> {
  const key = "daily-brief";
  if (force) clearCached(key);
  return dedupe(key, async () => {
    const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/daily-brief?force=${force}` : `/api/daily-brief?force=${force}`;
    const response = await fetch(endpoint, { method: "GET", headers: { "Content-Type": "application/json" } });
    if (!response.ok) throw new Error(`Backend error (${response.status})`);
    const data = (await response.json()) as DailyBriefResponse;
    setCached(key, data);
    return data;
  });
}

export function peekDailyBrief(): DailyBriefResponse | null {
  const cached = getCached<DailyBriefResponse>("daily-brief");
  return cached && cached.ageMs <= DAILY_TTL_MS ? cached.data : null;
}

export async function getInvestmentAgent(force = false): Promise<AgentResponse> {
  const key = "investment-agent";
  if (force) clearCached(key);
  return dedupe(key, async () => {
    const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/agent?force=${force}` : `/api/agent?force=${force}`;
    const response = await fetch(endpoint, { method: "GET", headers: { "Content-Type": "application/json" } });
    if (!response.ok) throw new Error(`Backend error (${response.status})`);
    const data = (await response.json()) as AgentResponse;
    setCached(key, data);
    // The agent response embeds source_daily_brief, so warm the daily-brief
    // cache too — that means navigating /agent → /daily can render instantly.
    if (data.source_daily_brief) setCached("daily-brief", data.source_daily_brief);
    return data;
  });
}

export function peekInvestmentAgent(): AgentResponse | null {
  const cached = getCached<AgentResponse>("investment-agent");
  return cached && cached.ageMs <= AGENT_TTL_MS ? cached.data : null;
}
