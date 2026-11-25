import { createContext, useContext, useEffect, useState, useSyncExternalStore, type PropsWithChildren } from "react";

import { createSession } from "@/auth/session";
import { accessTokens } from "@/auth/token-store";
import { apiBaseUrl } from "@/config/env";

const Context = createContext<ReturnType<typeof createSession> | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [session] = useState(() => createSession({ baseUrl: apiBaseUrl(), store: accessTokens }));
  useEffect(() => { void session.restore(); }, [session]);
  return <Context.Provider value={session}>{children}</Context.Provider>;
}

export function useAuth() {
  const session = useContext(Context);
  if (!session) throw new Error("Authentication requires AuthProvider.");
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  return { ...session, state };
}
