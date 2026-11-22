import * as SecureStore from "expo-secure-store";

import { ACCESS_TOKEN_KEY, memoryTokenStore, secureTokenStore } from "@/auth/token-store";

jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(),
  setItemAsync: jest.fn(),
  deleteItemAsync: jest.fn(),
  WHEN_UNLOCKED_THIS_DEVICE_ONLY: "WHEN_UNLOCKED_THIS_DEVICE_ONLY",
}));

const secureStore = SecureStore as jest.Mocked<typeof SecureStore>;

describe("token stores", () => {
  it("keeps a token in memory until it is cleared", async () => {
    const store = memoryTokenStore();
    expect(await store.read()).toBeNull();
    await store.write("one-time-token");
    expect(await store.read()).toBe("one-time-token");
    await store.clear();
    expect(await store.read()).toBeNull();
  });

  it("writes the access token through the platform secure store", async () => {
    secureStore.setItemAsync.mockResolvedValue();
    secureStore.getItemAsync.mockResolvedValue("stored");
    secureStore.deleteItemAsync.mockResolvedValue();
    const store = secureTokenStore();

    await store.write("stored");
    await expect(store.read()).resolves.toBe("stored");
    await store.clear();

    const options = { keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY };
    expect(secureStore.setItemAsync).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, "stored", options);
    expect(secureStore.getItemAsync).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, options);
    expect(secureStore.deleteItemAsync).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, options);
  });
});
