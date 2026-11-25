import { createApiClient, type FetchLike } from "@/api/client";

function reply(status: number, body: string): Awaited<ReturnType<FetchLike>> {
  return { ok: status < 400, status, statusText: "", text: async () => body };
}

const baseUrl = "http://127.0.0.1:5000/";

it("uses current native auth and home endpoints with bearer-only requests", async () => {
  const fetchImpl = jest.fn<ReturnType<FetchLike>, Parameters<FetchLike>>(async () => reply(200, "{}"));
  const api = createApiClient({ baseUrl, fetchImpl, token: "opaque" });
  await api.login({ name: "Ada", password: "secret" });
  await api.register({ name: "Ada", password: "secret", invite: "code" });
  await api.session();
  await api.home();
  await api.logout();
  expect(fetchImpl.mock.calls.map(([url, options]) => [url, options.method])).toEqual([
    [`${baseUrl}api/v1/auth/login`, "POST"], [`${baseUrl}api/v1/auth/register`, "POST"],
    [`${baseUrl}api/v1/session`, "GET"], [`${baseUrl}api/v1/home`, "GET"],
    [`${baseUrl}api/v1/auth/logout`, "POST"],
  ]);
  expect(JSON.parse(fetchImpl.mock.calls[1][1].body as string)).toEqual({ name: "Ada", password: "secret", invite: "code" });
  expect(fetchImpl.mock.calls.slice(0, 2).every(([, opts]) => !("Authorization" in (opts.headers ?? {})))).toBe(true);
  for (const [, opts] of fetchImpl.mock.calls.slice(2)) {
    expect(opts.credentials).toBe("omit");
    expect(opts.headers).toEqual({ Accept: "application/json", Authorization: "Bearer opaque" });
  }
});

it("distinguishes bad login credentials from an unauthorized bearer request", async () => {
  const onUnauthorized = jest.fn();
  const api = createApiClient({
    baseUrl, token: "opaque", onUnauthorized,
    fetchImpl: async () => reply(401, '{"error":"Sign in first.","code":"unauthenticated"}'),
  });
  await expect(api.login({ name: "Ada", password: "bad" })).rejects.toMatchObject({
    status: 401, message: "Sign in first.", body: { code: "unauthenticated" },
  });
  expect(onUnauthorized).not.toHaveBeenCalled();
  await expect(api.home()).rejects.toMatchObject({ status: 401 });
  expect(onUnauthorized).toHaveBeenCalledTimes(1);
});

it("reports transport and malformed response failures", async () => {
  const offline = createApiClient({ baseUrl, fetchImpl: async () => { throw new Error("offline"); } });
  await expect(offline.session()).rejects.toMatchObject({ status: 0 });
  const bad = createApiClient({ baseUrl, fetchImpl: async () => reply(502, "<html>bad gateway</html>") });
  await expect(bad.session()).rejects.toMatchObject({ status: 502, message: expect.stringContaining("not JSON") });
});

it("times out a request without invalidating its bearer session", async () => {
  jest.useFakeTimers();
  try {
    const onUnauthorized = jest.fn();
    const api = createApiClient({
      baseUrl, token: "opaque", onUnauthorized,
      fetchImpl: async (_, { signal }) => new Promise((_, reject) => {
        signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
    });
    const failure = expect(api.home()).rejects.toMatchObject({ status: 0 });
    jest.advanceTimersByTime(15_000);
    await failure;
    expect(onUnauthorized).not.toHaveBeenCalled();
  } finally {
    jest.useRealTimers();
  }
});
