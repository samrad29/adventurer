import { createApiClient, type ApiClient, type FetchLike } from "@/api/client";
import { ApiError } from "@/api/errors";
import type { TokenStore } from "@/auth/token-store";
import type { Credentials, Registration, Viewer } from "@/types/api";

export type AuthState =
  | { status: "loading" }
  | { status: "recovery"; message: string }
  | { status: "guest"; busy: boolean; notice?: string }
  | { status: "signedIn"; viewer: Viewer; api: ApiClient; sessionId: number };

export function createSession({
  baseUrl, store, fetchImpl,
}: { baseUrl: string; store: TokenStore; fetchImpl?: FetchLike }) {
  let state: AuthState = { status: "loading" };
  let token: string | null = null;
  let clearRequired = false;
  let busy = false;
  let sessionId = 0;
  const listeners = new Set<() => void>();
  const anonymous = createApiClient({ baseUrl, fetchImpl });

  function publish(next: AuthState) {
    state = next;
    listeners.forEach((listener) => listener());
  }

  async function clear(notice?: string) {
    token = null;
    clearRequired = true;
    try {
      await store.clear();
      clearRequired = false;
      publish({ status: "guest", busy: false, notice });
    } catch {
      publish({ status: "recovery", message: "Could not clear the saved session. Try again." });
    }
  }

  async function discard(notice?: string) {
    if (busy) return;
    busy = true;
    token = null;
    publish({ status: "loading" });
    try {
      await clear(notice);
    } finally {
      busy = false;
    }
  }

  function accept(value: string, viewer: Viewer) {
    token = value;
    const id = ++sessionId;
    const api = createApiClient({
      baseUrl, fetchImpl, token: value,
      onUnauthorized() {
        if (state.status === "signedIn" && state.sessionId === id) {
          void discard("Your session expired. Sign in again.");
        }
      },
    });
    publish({ status: "signedIn", viewer, api, sessionId: id });
  }

  async function restore() {
    if (busy || (state.status !== "loading" && state.status !== "recovery")) return;
    busy = true;
    publish({ status: "loading" });
    try {
      if (clearRequired) {
        await clear();
        return;
      }
      const stored = await store.read();
      if (!stored) {
        publish({ status: "guest", busy: false });
        return;
      }
      token = stored;
      try {
        const { you } = await createApiClient({ baseUrl, fetchImpl, token: stored }).session();
        if (!you) throw new ApiError(401, "Your session expired.");
        accept(stored, you);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          await clear("Your session expired. Sign in again.");
        } else {
          publish({ status: "recovery", message: "Could not verify the saved session. Check your connection and retry." });
        }
      }
    } catch {
      publish({ status: "recovery", message: "Could not read the saved session. Try again." });
    } finally {
      busy = false;
    }
  }

  async function authenticate(mode: "login" | "register", credentials: Registration) {
    if (busy || state.status !== "guest") return;
    busy = true;
    publish({ status: "guest", busy: true });
    try {
      const { token: issued, you } = await anonymous[mode](credentials);
      if (typeof issued !== "string" || !issued || !you) {
        throw new Error("The server did not return a session.");
      }
      try {
        await store.write(issued);
      } catch {
        // A failed write may have persisted the value. Block sign-in until it can be cleared.
        await clear("Could not save the session. Sign in again.");
        void createApiClient({ baseUrl, fetchImpl, token: issued }).logout().catch(() => {});
        return;
      }
      accept(issued, you);
    } catch (error) {
      publish({ status: "guest", busy: false });
      throw error;
    } finally {
      busy = false;
    }
  }

  async function logout() {
    if (busy) return;
    const previousToken = token;
    await discard();
    if (!previousToken) return;
    const guest = getSnapshot();
    try {
      await createApiClient({ baseUrl, fetchImpl, token: previousToken }).logout();
    } catch (error) {
      if (guest.status === "guest" && state === guest && !(error instanceof ApiError && error.status === 401)) {
        publish({ ...guest, notice: "Signed out on this device. The server could not confirm revocation." });
      }
    }
  }

  function getSnapshot() {
    return state;
  }

  return {
    getSnapshot,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    restore,
    login: (credentials: Credentials) => authenticate("login", credentials),
    register: (credentials: Registration) => authenticate("register", credentials),
    logout,
  };
}
