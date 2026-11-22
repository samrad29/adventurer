import { proposedNativePaths } from "@/auth/native-auth";
import { createApiClient, type FetchLike } from "@/api/client";
import { ApiError } from "@/api/errors";

function responder(status: number, body: string, statusText = ""): FetchLike {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    statusText,
    text: async () => body,
  });
}

describe("createApiClient", () => {
  it("reads the session JSON", async () => {
    const fetchImpl = jest.fn(responder(200, JSON.stringify({ csrf: "abc", you: null })));
    const client = createApiClient({ baseUrl: "http://127.0.0.1:5000/", fetchImpl });

    await expect(client.session()).resolves.toEqual({ csrf: "abc", you: null });
    expect(fetchImpl).toHaveBeenCalledWith(
      "http://127.0.0.1:5000/api/v1/session",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("uses the Flask error string and status", async () => {
    const client = createApiClient({
      baseUrl: "http://127.0.0.1:5000",
      fetchImpl: responder(401, JSON.stringify({ error: "Log in first." })),
    });

    await expect(client.listActivities()).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      message: "Log in first.",
    });
  });

  it("reports a network failure without a status", async () => {
    const fetchImpl: FetchLike = async () => {
      throw new Error("offline");
    };
    const client = createApiClient({ baseUrl: "http://127.0.0.1:5000", fetchImpl });

    await expect(client.health()).rejects.toBeInstanceOf(ApiError);
    await expect(client.health()).rejects.toMatchObject({ status: 0 });
  });

  it("rejects an HTML body", async () => {
    const client = createApiClient({
      baseUrl: "http://127.0.0.1:5000",
      fetchImpl: responder(400, "<html>expired</html>"),
    });

    await expect(client.session()).rejects.toMatchObject({
      status: 400,
      message: "The API returned a response that was not JSON.",
    });
  });

  it("sends a bearer token and CSRF only on a change", async () => {
    const fetchImpl = jest.fn(responder(200, JSON.stringify({ activities: [] })));
    const client = createApiClient({
      baseUrl: "http://127.0.0.1:5000",
      fetchImpl,
      getAccessToken: async () => "raw-token",
      getCsrfToken: async () => "csrf-token",
    });

    await client.listActivities();
    const getHeaders = fetchImpl.mock.calls[0][1]?.headers;
    expect(getHeaders?.Authorization).toBe("Bearer raw-token");
    expect(getHeaders?.["X-CSRF-Token"]).toBeUndefined();

    fetchImpl.mockImplementation(responder(201, JSON.stringify({ id: 4 })));
    await client.createActivity({ elevation_ft: "100", happened_on: "2026-11-21", note: "Stairs" });
    const postHeaders = fetchImpl.mock.calls[1][1]?.headers;
    expect(postHeaders?.Authorization).toBe("Bearer raw-token");
    expect(postHeaders?.["X-CSRF-Token"]).toBe("csrf-token");
  });

  it("does not call the proposed native auth routes", () => {
    const client = createApiClient({ baseUrl: "http://127.0.0.1:5000", fetchImpl: responder(200, "null") });
    const names = Object.keys(client);

    expect(names).not.toContain("login");
    expect(names).not.toContain("register");
    expect(proposedNativePaths).toEqual(
      expect.arrayContaining([
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/me/expeditions",
      ]),
    );
  });
});
