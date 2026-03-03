import type { AnalysisRequest, AnalysisResponse } from "@/lib/types";

const RAW_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL;
const API_BASE_URL = RAW_API_BASE_URL ? RAW_API_BASE_URL.replace(/\/$/, "") : "";

export async function analyzePortfolio(payload: AnalysisRequest): Promise<AnalysisResponse> {
  const endpoint = API_BASE_URL ? `${API_BASE_URL}/api/analyze` : "/api/analyze";
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
