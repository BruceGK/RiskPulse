import type { AgentResponse, AnalysisRequest, AnalysisResponse, DailyBriefResponse } from "@/lib/types";

const RAW_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL;
const API_BASE_URL = RAW_API_BASE_URL ? RAW_API_BASE_URL.replace(/\/$/, "") : "";

type AnalyzeOptions = {
  phase?: "quick" | "full";
};

export async function analyzePortfolio(payload: AnalysisRequest, options?: AnalyzeOptions): Promise<AnalysisResponse> {
  const phase = options?.phase || "full";
  const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/analyze?phase=${phase}` : `/api/analyze?phase=${phase}`;
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Backend error (${response.status})`);
  }

  return (await response.json()) as AnalysisResponse;
}

export async function getDailyBrief(force = false): Promise<DailyBriefResponse> {
  const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/daily-brief?force=${force}` : `/api/daily-brief?force=${force}`;
  const response = await fetch(endpoint, {
    method: "GET",
    headers: { "Content-Type": "application/json" }
  });

  if (!response.ok) {
    throw new Error(`Backend error (${response.status})`);
  }

  return (await response.json()) as DailyBriefResponse;
}

export async function getInvestmentAgent(force = false): Promise<AgentResponse> {
  const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/agent?force=${force}` : `/api/agent?force=${force}`;
  const response = await fetch(endpoint, {
    method: "GET",
    headers: { "Content-Type": "application/json" }
  });

  if (!response.ok) {
    throw new Error(`Backend error (${response.status})`);
  }

  return (await response.json()) as AgentResponse;
}
