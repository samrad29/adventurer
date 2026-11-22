import { ApiError, messageFromBody } from "@/api/errors";
import type {
  Activity,
  ActivityInput,
  ChallengeResponse,
  ChallengeSummary,
  Expedition,
  Invitation,
  LeaderboardResponse,
  LogCreated,
  OkResponse,
  Progress,
  PublicExpedition,
  RouteDetail,
  RouteSummary,
  SessionResponse,
  ShareLink,
} from "@/types/api";

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

type ResponseLike = {
  ok: boolean;
  status: number;
  statusText: string;
  text(): Promise<string>;
};

export type FetchLike = (input: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => Promise<ResponseLike>;

export type ApiClientOptions = {
  baseUrl: string;
  fetchImpl?: FetchLike;
  getAccessToken?: () => Promise<string | null>;
  getCsrfToken?: () => Promise<string | null>;
};

export type RequestOptions = {
  method?: HttpMethod;
  body?: unknown;
};

function joinUrl(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/$/, "");
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

function segment(value: string | number): string {
  return encodeURIComponent(String(value));
}

/**
 * Talks only to the Flask JSON API that already exists.
 *
 * A stored access token is sent as Authorization: Bearer. Today's Flask
 * ignores that header and still requires the session cookie plus CSRF.
 * Pass getCsrfToken only when a cookie session is already in hand.
 */
export function createApiClient(options: ApiClientOptions) {
  const fetchImpl = options.fetchImpl ?? (fetch as unknown as FetchLike);

  async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
    const method = init.method ?? "GET";
    const headers: Record<string, string> = { Accept: "application/json" };
    if (init.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const token = options.getAccessToken ? await options.getAccessToken() : null;
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    if (method !== "GET" && options.getCsrfToken) {
      const csrf = await options.getCsrfToken();
      if (csrf) {
        headers["X-CSRF-Token"] = csrf;
      }
    }

    let response: ResponseLike;
    try {
      response = await fetchImpl(joinUrl(options.baseUrl, path), {
        method,
        headers,
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
      });
    } catch (error) {
      throw new ApiError(0, "The Adventurer API could not be reached.", error);
    }

    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text) as unknown;
      } catch {
        throw new ApiError(response.status, "The API returned a response that was not JSON.", text);
      }
    }
    if (!response.ok) {
      const message = messageFromBody(parsed) ?? (response.statusText || "Request failed.");
      throw new ApiError(response.status, message, parsed);
    }
    return parsed as T;
  }

  return {
    request,
    health: () => request<{ ok: boolean }>("/health"),
    session: () => request<SessionResponse>("/api/v1/session"),
    listRoutes: () => request<{ routes: RouteSummary[] }>("/api/v1/routes"),
    route: (slug: string) => request<RouteDetail>(`/api/v1/routes/${segment(slug)}`),
    listExpeditions: () => request<{ expeditions: PublicExpedition[] }>("/api/v1/expeditions"),
    expedition: (slug: string) => request<Expedition>(`/api/v1/expeditions/${segment(slug)}`),
    createExpedition: (body: { route_slug: string; name: string; description: string }) =>
      request<Expedition>("/api/v1/expeditions", {
        method: "POST",
        body: { ...body, visibility: "private" },
      }),
    updateExpedition: (
      slug: string,
      body: { name?: string; description?: string; owner_username?: string },
    ) => request<Expedition>(`/api/v1/expeditions/${segment(slug)}`, { method: "PATCH", body }),
    archiveExpedition: (slug: string) =>
      request<Expedition>(`/api/v1/expeditions/${segment(slug)}/archive`, { method: "POST" }),
    joinExpedition: (slug: string) =>
      request<Expedition>(`/api/v1/expeditions/${segment(slug)}/participation`, { method: "POST" }),
    leaveExpedition: (slug: string) =>
      request<OkResponse>(`/api/v1/expeditions/${segment(slug)}/participation`, { method: "DELETE" }),
    expeditionLeaderboard: (slug: string) =>
      request<LeaderboardResponse>(`/api/v1/expeditions/${segment(slug)}/leaderboard`),
    listInvitations: (slug: string) =>
      request<{ invitations: Invitation[] }>(`/api/v1/expeditions/${segment(slug)}/invitations`),
    inviteUsername: (slug: string, username: string) =>
      request<Invitation>(`/api/v1/expeditions/${segment(slug)}/invitations`, {
        method: "POST",
        body: { username },
      }),
    createShareLink: (slug: string) =>
      request<ShareLink>(`/api/v1/expeditions/${segment(slug)}/invitations`, {
        method: "POST",
        body: { kind: "link" },
      }),
    revokeInvitation: (slug: string, invitationId: number) =>
      request<OkResponse>(`/api/v1/expeditions/${segment(slug)}/invitations/${invitationId}`, {
        method: "DELETE",
      }),
    acceptShareLink: (token: string) =>
      request<{ slug: string }>(`/api/v1/invitations/${segment(token)}/accept`, { method: "POST" }),
    declineShareLink: (token: string) =>
      request<OkResponse>(`/api/v1/invitations/${segment(token)}/decline`, { method: "POST" }),
    listChallenges: () => request<ChallengeSummary[]>("/api/v1/challenges"),
    challenge: (slug: string) => request<ChallengeResponse>(`/api/v1/challenges/${segment(slug)}`),
    challengeLeaderboard: (slug: string) =>
      request<LeaderboardResponse>(`/api/v1/challenges/${segment(slug)}/leaderboard`),
    addChallengeLog: (slug: string, body: ActivityInput) =>
      request<LogCreated>(`/api/v1/challenges/${segment(slug)}/logs`, { method: "POST", body }),
    removeChallengeLog: (slug: string, logId: number) =>
      request<{ progress: Progress }>(`/api/v1/challenges/${segment(slug)}/logs/${logId}`, {
        method: "DELETE",
      }),
    listActivities: () => request<{ activities: Activity[] }>("/api/v1/activities"),
    createActivity: (body: ActivityInput & { expedition_slug?: string }) =>
      request<{ id: number }>("/api/v1/activities", { method: "POST", body }),
    updateActivity: (activityId: number, body: ActivityInput) =>
      request<OkResponse>(`/api/v1/activities/${activityId}`, { method: "PATCH", body }),
    deleteActivity: (activityId: number) =>
      request<OkResponse>(`/api/v1/activities/${activityId}`, { method: "DELETE" }),
    addCredit: (activityId: number, expeditionSlug: string) =>
      request<OkResponse>(`/api/v1/activities/${activityId}/credits`, {
        method: "POST",
        body: { expedition_slug: expeditionSlug },
      }),
    removeCredit: (activityId: number, expeditionSlug: string) =>
      request<OkResponse>(`/api/v1/activities/${activityId}/credits/${segment(expeditionSlug)}`, {
        method: "DELETE",
      }),
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
