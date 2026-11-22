import { Stack } from "expo-router";
import { Text, View } from "react-native";

import { apiBaseUrl } from "@/config/env";

export default function FoundationScreen() {
  return (
    <View style={{ flex: 1, justifyContent: "center", padding: 24, gap: 12 }}>
      <Stack.Screen options={{ title: "The Adventurer" }} />
      <Text>Mobile foundation</Text>
      <Text>Product screens are not part of this build.</Text>
      <Text selectable>API base URL: {apiBaseUrl()}</Text>
    </View>
  );
}
