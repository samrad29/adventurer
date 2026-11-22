import { resolveApiBaseUrl } from "./env";

describe("resolveApiBaseUrl", () => {
  it("uses the configured origin and drops a trailing slash", () => {
    expect(resolveApiBaseUrl("http://10.0.2.2:5000/", false)).toBe("http://10.0.2.2:5000");
  });

  it("defaults to the local Flask server outside production", () => {
    expect(resolveApiBaseUrl("  ", false)).toBe("http://127.0.0.1:5000");
  });

  it("refuses a release build that has no origin", () => {
    expect(() => resolveApiBaseUrl(undefined, true)).toThrow(/EXPO_PUBLIC_API_BASE_URL/);
  });
});
