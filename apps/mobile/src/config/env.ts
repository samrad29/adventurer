const DEVELOPMENT_API = "http://127.0.0.1:5000";

/**
 * Flask origin for the mobile client.
 *
 * 127.0.0.1 reaches a Flask process on the same machine. An Android emulator
 * must use http://10.0.2.2:5000. A physical phone must use the computer's
 * LAN address. Set EXPO_PUBLIC_API_BASE_URL for those cases.
 *
 * Only EXPO_PUBLIC_ values are bundled. Never put a database URL or a
 * Supabase service-role key in this app.
 */
export function resolveApiBaseUrl(configured: string | undefined, production: boolean): string {
  const value = configured?.trim().replace(/\/$/, "") ?? "";
  if (value) {
    return value;
  }
  if (production) {
    throw new Error("Set EXPO_PUBLIC_API_BASE_URL before building a release.");
  }
  return DEVELOPMENT_API;
}

export function apiBaseUrl(): string {
  return resolveApiBaseUrl(
    process.env.EXPO_PUBLIC_API_BASE_URL,
    process.env.NODE_ENV === "production",
  );
}
