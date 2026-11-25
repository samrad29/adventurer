import { useEffect, useState } from "react";
import { ActivityIndicator, Button, Text, View } from "react-native";

import type { ApiClient } from "@/api/client";
import { useAuth } from "@/auth/auth-context";
import type { HomeResponse } from "@/types/api";

export default function Home() {
  const { state, logout } = useAuth();
  if (state.status !== "signedIn") return null;
  return (
    <AccountHome key={state.sessionId} api={state.api} name={state.viewer.name} logout={logout} />
  );
}

function AccountHome({ api, name, logout }: { api: ApiClient; name: string; logout: () => Promise<void> }) {
  const [retry, setRetry] = useState(0);
  const [home, setHome] = useState<HomeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api.home(controller.signal).then((data) => {
      if (!controller.signal.aborted) setHome(data);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        setError(failure instanceof Error ? failure.message : "Could not load home.");
      }
    });
    return () => controller.abort();
  }, [api, retry]);

  return (
    <View style={{ padding: 24, gap: 12 }}>
      <Text accessibilityRole="header" style={{ fontSize: 24 }}>Welcome, {name}</Text>
      {error ? (
        <>
          <Text accessibilityRole="alert">{error}</Text>
          <Button title="Retry home" onPress={() => { setError(null); setRetry((value) => value + 1); }} />
        </>
      ) : home ? (
        <>
          <Text>Journey status: {home.state}</Text>
          {home.journey && <Text>{home.journey.route.name}</Text>}
        </>
      ) : <ActivityIndicator accessibilityLabel="Loading home" />}
      <Button title="Sign out" onPress={() => void logout()} />
    </View>
  );
}
