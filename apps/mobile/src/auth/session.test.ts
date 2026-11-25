import type { FetchLike } from "@/api/client";
import { createSession } from "@/auth/session";
import { memoryTokenStore } from "@/auth/token-store";
import type { Viewer } from "@/types/api";

const baseUrl = "http://localhost:5000";
const ada: Viewer = {
  id: 1, name: "Ada", trail_identity: null, trail_name_display: "both", trail_name_reveal: null,
};
const bob = { ...ada, id: 2, name: "Bob" };
const response = (status: number, body: unknown) => ({
  ok: status < 400, status, statusText: "", text: async () => JSON.stringify(body),
});
const bearer = (opts: RequestInit) => (opts.headers as Record<string, string>)?.Authorization;

it("registers and restores a saved native session", async () => {
  const store = memoryTokenStore();
  const fetchImpl: FetchLike = async (url, opts) => url.endsWith("/register")
    ? response(201, { you: ada, token: "ada-token", csrf: "unused" })
    : response(bearer(opts) === "Bearer ada-token" ? 200 : 401, { you: ada, csrf: null });
  const session = createSession({ baseUrl, store, fetchImpl });
  expect(session.getSnapshot().status).toBe("loading");
  await session.restore();
  await session.register({ name: "Ada", password: "long password", invite: "test" });
  expect(await store.read()).toBe("ada-token");
  const restored = createSession({ baseUrl, store, fetchImpl });
  await restored.restore();
  expect(restored.getSnapshot()).toMatchObject({ status: "signedIn", viewer: ada });
});

it("keeps the token on an offline restart, then removes it if the server rejects it", async () => {
  const store = memoryTokenStore("expired");
  let online = false;
  const session = createSession({
    baseUrl, store, fetchImpl: async () => {
      if (!online) throw new Error("offline");
      return response(401, { error: "Expired." });
    },
  });
  await session.restore();
  expect(session.getSnapshot().status).toBe("recovery");
  expect(await store.read()).toBe("expired");
  online = true;
  await session.restore();
  expect(session.getSnapshot()).toMatchObject({ status: "guest", notice: expect.stringContaining("expired") });
  expect(await store.read()).toBeNull();
});

it("signs out locally even if revocation is offline and isolates late responses from a new account", async () => {
  const store = memoryTokenStore("ada-token");
  let reply: (value: Awaited<ReturnType<FetchLike>>) => void = () => {};
  const late = new Promise<Awaited<ReturnType<FetchLike>>>((resolve) => { reply = resolve; });
  const session = createSession({
    baseUrl, store, fetchImpl: async (url, opts) => {
      if (url.endsWith("/session")) return response(200, { you: ada, csrf: null });
      if (url.endsWith("/logout")) {
        expect(bearer(opts)).toBe("Bearer ada-token");
        throw new Error("offline");
      }
      if (url.endsWith("/login")) return response(200, { you: bob, token: "bob-token" });
      if (bearer(opts) === "Bearer ada-token") return late;
      return response(401, { error: "Revoked." });
    },
  });
  await session.restore();
  const first = session.getSnapshot();
  if (first.status !== "signedIn") throw new Error("Expected the restored session.");
  const oldRequest = first.api.home().catch((error) => error);
  await session.logout();
  expect(await store.read()).toBeNull();
  expect(session.getSnapshot()).toMatchObject({ status: "guest", notice: expect.stringContaining("revocation") });
  await session.login({ name: "Bob", password: "long password" });
  reply(response(401, {}));
  await oldRequest;
  const next = session.getSnapshot();
  expect(next).toMatchObject({ status: "signedIn", viewer: bob });
  expect(await store.read()).toBe("bob-token");
  if (next.status !== "signedIn") throw new Error("Expected the new session.");
  await expect(next.api.home()).rejects.toMatchObject({ status: 401 });
  expect(session.getSnapshot().status).toBe("guest");
  expect(await store.read()).toBeNull();
});

it("retries failed secure deletion without restoring the previous account", async () => {
  const store = memoryTokenStore("ada-token");
  const clear = jest.spyOn(store, "clear").mockRejectedValueOnce(new Error("locked"));
  const session = createSession({
    baseUrl, store, fetchImpl: async (url) => url.endsWith("/logout")
      ? response(200, { ok: true }) : response(200, { you: ada, csrf: null }),
  });
  await session.restore();
  await session.logout();
  expect(session.getSnapshot().status).toBe("recovery");
  await session.login({ name: "Bob", password: "long password" });
  await session.restore();
  expect(clear).toHaveBeenCalledTimes(2);
  expect(session.getSnapshot().status).toBe("guest");
  expect(await store.read()).toBeNull();
});

it("keeps failed credentials and a failed secure write out of the authenticated state", async () => {
  const store = memoryTokenStore();
  jest.spyOn(store, "write").mockImplementationOnce(async () => { throw new Error("keychain locked"); });
  const session = createSession({
    baseUrl, store, fetchImpl: async (url, opts) => {
      if (url.endsWith("/logout")) return response(200, { ok: true });
      return JSON.parse(opts.body as string).password === "bad"
        ? response(401, { error: "Wrong password." })
        : response(200, { you: ada, token: "ada-token" });
    },
  });
  await session.restore();
  await expect(session.login({ name: "Ada", password: "bad" })).rejects.toMatchObject({ status: 401 });
  expect(session.getSnapshot()).toMatchObject({ status: "guest", busy: false });
  await session.login({ name: "Ada", password: "good" });
  expect(session.getSnapshot()).toMatchObject({ status: "guest", notice: expect.stringContaining("save") });
  expect(await store.read()).toBeNull();
});
