import { Stack } from "expo-router";
import { AuthProvider, useAuth } from "@/auth/auth-context";

export default function RootLayout() {
  return <AuthProvider><Routes /></AuthProvider>;
}

function Routes() {
  const { state } = useAuth();
  return (
    <Stack screenOptions={{ title: "The Adventurer" }}>
      <Stack.Screen name="index" />
      <Stack.Protected guard={state.status === "guest"}>
        <Stack.Screen name="login" options={{ title: "Sign in" }} />
        <Stack.Screen name="register" options={{ title: "Create account" }} />
      </Stack.Protected>
      <Stack.Protected guard={state.status === "signedIn"}>
        <Stack.Screen name="home" />
      </Stack.Protected>
    </Stack>
  );
}
