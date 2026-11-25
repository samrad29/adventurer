import { Redirect } from "expo-router";
import { ActivityIndicator, Button, Text, View } from "react-native";

import { useAuth } from "@/auth/auth-context";

export default function Entry() {
  const { state, restore, logout } = useAuth();
  if (state.status === "signedIn") return <Redirect href="/home" />;
  if (state.status === "guest") return <Redirect href="/login" />;
  return (
    <View style={{ flex: 1, justifyContent: "center", padding: 24, gap: 12 }}>
      {state.status === "loading" ? (
        <ActivityIndicator accessibilityLabel="Checking session" />
      ) : (
        <>
          <Text accessibilityRole="alert">{state.message}</Text>
          <Button title="Retry" onPress={() => void restore()} />
          <Button title="Sign out on this device" onPress={() => void logout()} />
        </>
      )}
    </View>
  );
}
