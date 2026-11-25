import { Link } from "expo-router";
import { useState } from "react";
import { Button, ScrollView, StyleSheet, Text, TextInput } from "react-native";

import { useAuth } from "@/auth/auth-context";

export function AuthForm({ register = false }: { register?: boolean }) {
  const auth = useAuth();
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [error, setError] = useState<string | null>(null);
  const busy = auth.state.status !== "guest" || auth.state.busy;
  const title = register ? "Create account" : "Sign in";

  async function submit() {
    setError(null);
    try {
      if (register) {
        await auth.register({ name: name.trim(), password, invite: invite.trim() || undefined });
      } else {
        await auth.login({ name: name.trim(), password });
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign-in failed. Try again.");
    }
  }

  return (
    <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={styles.form}>
      <Text accessibilityRole="header" style={styles.heading}>{title}</Text>
      <Text>Name</Text>
      <TextInput accessibilityLabel="Name" autoCapitalize="none" autoComplete="username"
        editable={!busy} value={name} onChangeText={setName} style={styles.input} />
      <Text>Password</Text>
      <TextInput accessibilityLabel="Password" secureTextEntry autoCapitalize="none"
        autoComplete={register ? "new-password" : "current-password"}
        editable={!busy} value={password} onChangeText={setPassword} style={styles.input} />
      {register && (
        <>
          <Text>Invite code (if required)</Text>
          <TextInput accessibilityLabel="Invite code" autoCapitalize="none" autoCorrect={false}
            editable={!busy} value={invite} onChangeText={setInvite} style={styles.input} />
        </>
      )}
      {auth.state.status === "guest" && auth.state.notice && <Text accessibilityRole="alert">{auth.state.notice}</Text>}
      {error && <Text accessibilityRole="alert">{error}</Text>}
      <Button title={busy ? "Please wait…" : title}
        disabled={busy || !name.trim() || !password} onPress={() => void submit()} />
      <Link href={register ? "/login" : "/register"} replace>
        {register ? "Already have an account? Sign in" : "Create an account"}
      </Link>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  form: { padding: 24, gap: 12 },
  heading: { fontSize: 24, fontWeight: "600" },
  input: { borderWidth: 1, borderColor: "#808080", borderRadius: 6, padding: 10 },
});
