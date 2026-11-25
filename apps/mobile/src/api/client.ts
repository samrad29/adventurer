import { ApiError, messageFromBody } from "@/api/errors";
import type { AuthResponse, Credentials, HomeResponse, Registration, SessionResponse } from "@/types/api";

const REQUEST_TIMEOUT_MS = 15_000;

export type FetchLike = (input: string, init: RequestInit) => Promise<{
  ok: boolean;
  status: number;
  statusText: string;
  text(): Promise<string>;
}>;

export function createApiClient({
  baseUrl, fetchImpl = fetch, token, onUnauthorized,
}: {
  baseUrl: string;
  fetchImpl?: FetchLike;
  token?: string;
  onUnauthorized?: () => void;
}) {
  const origin = baseUrl.replace(/\/+$/, "");

  async function request<T>(
    path: string,
    { body, authenticated = true, method = "GET", signal }: {
      body?: unknown; authenticated?: boolean; method?: "GET" | "POST"; signal?: AbortSignal;
    } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token && authenticated) headers.Authorization = `Bearer ${token}`;

    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) abort();
    signal?.addEventListener("abort", abort);
    const timeout = setTimeout(abort, REQUEST_TIMEOUT_MS);
    let response: Awaited<ReturnType<FetchLike>>;
    let text: string;
    try {
      response = await fetchImpl(`${origin}/api/v1${path}`, {
        method, headers, credentials: "omit", signal: controller.signal,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!controller.signal.aborted && response.status === 401 && headers.Authorization) onUnauthorized?.();
      text = await response.text();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError(0, "The Adventurer API could not be reached.");
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new ApiError(response.status, "The API returned a response that was not JSON.");
    }
    if (!response.ok) {
      throw new ApiError(response.status, messageFromBody(parsed) ?? (response.statusText || "Request failed."), parsed);
    }
    return parsed as T;
  }

  return {
    login: (body: Credentials) => request<AuthResponse>("/auth/login", { body, method: "POST", authenticated: false }),
    register: (body: Registration) => request<AuthResponse>("/auth/register", { body, method: "POST", authenticated: false }),
    logout: () => request<{ ok: true }>("/auth/logout", { method: "POST" }),
    session: () => request<SessionResponse>("/session"),
    home: (signal?: AbortSignal) => request<HomeResponse>("/home", { signal }),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
