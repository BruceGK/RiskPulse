/**
 * Tiny stale-while-revalidate cache for API responses.
 *
 * - In-memory map for instant reads within the same tab.
 * - Optional sessionStorage mirror so a tab reload still serves cached data.
 * - Per-key TTL: callers decide how long a payload is considered "fresh".
 *
 * No external dependency; fits the personal-project budget without pulling in SWR.
 */

type Entry<T> = {
  data: T;
  // ms since epoch when the data was stored
  storedAt: number;
};

const _mem = new Map<string, Entry<unknown>>();
const STORAGE_PREFIX = "rp_cache:";

function readSession<T>(key: string): Entry<T> | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(STORAGE_PREFIX + key);
    if (!raw) return null;
    return JSON.parse(raw) as Entry<T>;
  } catch {
    return null;
  }
}

function writeSession<T>(key: string, entry: Entry<T>): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(entry));
  } catch {
    // Quota exceeded or storage disabled — fail open, in-memory still works.
  }
}

export function getCached<T>(key: string): { data: T; ageMs: number } | null {
  const memEntry = _mem.get(key) as Entry<T> | undefined;
  const entry = memEntry ?? readSession<T>(key);
  if (!entry) return null;
  // Hydrate the in-memory map if we read from sessionStorage so subsequent
  // gets in this tab are O(1).
  if (!memEntry && entry) _mem.set(key, entry);
  return { data: entry.data, ageMs: Date.now() - entry.storedAt };
}

export function isFresh(cached: { ageMs: number } | null, maxAgeMs: number): boolean {
  return !!cached && cached.ageMs <= maxAgeMs;
}

export function setCached<T>(key: string, data: T): void {
  const entry: Entry<T> = { data, storedAt: Date.now() };
  _mem.set(key, entry);
  writeSession(key, entry);
}

export function clearCached(key: string): void {
  _mem.delete(key);
  if (typeof window !== "undefined") {
    try {
      window.sessionStorage.removeItem(STORAGE_PREFIX + key);
    } catch {
      // ignore
    }
  }
}

/**
 * Stable key for a JSON-serialisable request payload. Uses sorted keys to
 * avoid spurious cache misses when object key order differs between renders.
 */
export function cacheKey(prefix: string, payload: unknown): string {
  return `${prefix}:${stableStringify(payload)}`;
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(",")}]`;
  }
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(obj[k])}`).join(",")}}`;
}

/**
 * Module-level promise cache so concurrent callers asking for the same key
 * share a single in-flight request instead of stampeding the backend.
 */
const _inflight = new Map<string, Promise<unknown>>();

export async function dedupe<T>(key: string, factory: () => Promise<T>): Promise<T> {
  const existing = _inflight.get(key) as Promise<T> | undefined;
  if (existing) return existing;
  const promise = factory().finally(() => {
    _inflight.delete(key);
  });
  _inflight.set(key, promise);
  return promise;
}
