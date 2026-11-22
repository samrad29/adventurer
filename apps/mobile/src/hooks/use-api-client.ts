import { useMemo } from "react";

import { createApiClient, type ApiClient } from "@/api/client";
import { accessTokens } from "@/auth/token-store";
import { apiBaseUrl } from "@/config/env";

export function useApiClient(): ApiClient {
  return useMemo(
    () =>
      createApiClient({
        baseUrl: apiBaseUrl(),
        getAccessToken: () => accessTokens.read(),
      }),
    [],
  );
}
