/**
 * Browser sign-in stays on the signed session cookie. These routes are a
 * proposal for a later backend change. They are not implemented, and the
 * mobile client must not call them.
 *
 * Recommended server behavior, when that change is made:
 * - Issue an opaque token from secrets.token_urlsafe, store only its sha256.
 * - Resolve Authorization: Bearer on /api/v1 and skip CSRF for that request.
 * - Leave cookie sessions and their CSRF check unchanged.
 * - Do not accept the token from a cookie or a query string.
 * - Register should follow /join: enroll in the Global Everest Challenge
 *   unless the account is being created from an invitation.
 */
export const proposedNativeEndpoints = [
  {
    id: "login",
    method: "POST",
    path: "/api/v1/auth/login",
    today: "POST /login is an HTML form. It requires CSRF and answers with a redirect.",
  },
  {
    id: "register",
    method: "POST",
    path: "/api/v1/auth/register",
    today: "POST /join is an HTML form, not a JSON account API.",
  },
  {
    id: "logout",
    method: "POST",
    path: "/api/v1/auth/logout",
    today: "POST /logout clears the browser cookie and redirects.",
  },
  {
    id: "revoke",
    method: "POST",
    path: "/api/v1/auth/revoke",
    today: "A signed session cookie cannot be revoked for one device.",
  },
  {
    id: "my-expeditions",
    method: "GET",
    path: "/api/v1/me/expeditions",
    today: "GET /api/v1/expeditions lists public expeditions only.",
  },
  {
    id: "incoming-invitations",
    method: "GET",
    path: "/api/v1/invitations",
    today: "Incoming invitations are rendered on the activities page, not returned as JSON.",
  },
  {
    id: "username-invitation-action",
    method: "POST",
    path: "/api/v1/invitations/id/:id/accept",
    today: "Username invitation accept and decline are HTML form posts. The JSON routes take a share-link token.",
  },
] as const;

export const proposedNativePaths: readonly string[] = proposedNativeEndpoints.map(
  (endpoint) => endpoint.path,
);
