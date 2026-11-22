import * as SecureStore from "expo-secure-store";

export const ACCESS_TOKEN_KEY = "adventurer.access-token";

export interface TokenStore {
  read(): Promise<string | null>;
  write(token: string): Promise<void>;
  clear(): Promise<void>;
}

export function memoryTokenStore(initial: string | null = null): TokenStore {
  let current = initial;
  return {
    async read() {
      return current;
    },
    async write(token: string) {
      current = token;
    },
    async clear() {
      current = null;
    },
  };
}

/** Keychain on iOS, encrypted SharedPreferences on Android. Not a login flow. */
export function secureTokenStore(key = ACCESS_TOKEN_KEY): TokenStore {
  const options = {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  };
  return {
    read() {
      return SecureStore.getItemAsync(key, options);
    },
    write(token: string) {
      return SecureStore.setItemAsync(key, token, options);
    },
    clear() {
      return SecureStore.deleteItemAsync(key, options);
    },
  };
}

export const accessTokens = secureTokenStore();
