export type TrailNameReveal = {
  name: string;
  route_slug: string;
  checkpoint_slug: string;
};

export type Viewer = {
  id: number;
  name: string;
  trail_name_display: "both" | "trail_only" | "hidden";
  trail_identity: {
    name: string;
    assigned_at: string;
    reroll_available: boolean;
    reveal_acknowledged_at: string | null;
  } | null;
  trail_name_reveal: TrailNameReveal | null;
};

export type Credentials = { name: string; password: string };
export type Registration = Credentials & { invite?: string };
export type AuthResponse = { token: string; csrf: string; you: Viewer };
export type SessionResponse = { csrf: null; you: Viewer };

// The subset of the home contract used by the foundation screen.
export type HomeResponse = {
  you: Viewer;
  state: "active" | "resume" | "completed" | "abandoned" | "ready";
  journey: { id: number; status: "active"; route: { slug: string; name: string } } | null;
  trophy_case: { earned_count: number; pending_reveal_count: number };
};
