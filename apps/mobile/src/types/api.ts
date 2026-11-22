export type Viewer = {
  id: number;
  name: string;
};

export type SessionResponse = {
  csrf: string;
  you: Viewer | null;
};

export type RouteSummary = {
  slug: string;
  name: string;
  region: string;
  summary: string;
  summit_ft: number;
};

export type RouteCheckpoint = {
  slug: string;
  name: string;
  elevation_ft: number;
  place: string;
};

export type RouteDetail = RouteSummary & {
  checkpoints: RouteCheckpoint[];
  expeditions: {
    slug: string;
    name: string;
    description: string;
  }[];
};

export type PublicExpedition = {
  slug: string;
  name: string;
  description: string;
  visibility: "public";
  status: "active";
  route: {
    slug: string;
    name: string;
    region: string;
    summit_ft: number;
  };
};

export type Expedition = {
  slug: string;
  name: string;
  description: string;
  visibility: string;
  status: string;
  starts_at: string | null;
  ends_at: string | null;
  route: {
    slug: string;
    name: string;
    region: string;
    summit_ft: number;
  };
  participation_status: string | null;
  is_owner: boolean;
  can_join: boolean;
  can_log: boolean;
  can_manage: boolean;
  checkpoints?: {
    slug: string;
    name: string;
    elevation_ft: number;
  }[];
  progress?: Progress;
  you?: Viewer | null;
};

export type ProgressStop = {
  slug: string;
  name: string;
  elevation_ft: number;
  place: string;
  summary: string;
  unlocked: boolean;
  unlocks: unknown[];
};

export type Progress = {
  slug: string;
  name: string;
  local_names: string[];
  region: string;
  route: string;
  summit_ft: number;
  summary: string;
  total_ft: number;
  percent: number;
  summited: boolean;
  remaining_ft: number;
  current: ProgressStop | null;
  next: ProgressStop | null;
  to_next_ft: number | null;
  stops: ProgressStop[];
};

export type ChallengeSummary = {
  slug: string;
  name: string;
  region: string;
  summit_ft: number;
};

export type ChallengeResponse = {
  progress: Progress;
  you: Viewer | null;
};

export type LeaderboardEntry = {
  id: number;
  name: string;
  total_ft: number;
  last_on: string | null;
  camp: string;
  percent: number;
};

export type LeaderboardResponse = {
  leaderboard: LeaderboardEntry[];
};

export type ActivityInput = {
  elevation_ft: string;
  distance_km?: string;
  happened_on?: string;
  note?: string;
};

export type ActivityCredit = {
  expedition_slug: string;
  expedition_name: string;
  elevation_ft: number;
};

export type Activity = {
  id: number;
  activity_date: string;
  happened_on: string;
  elevation_ft: number;
  distance_km: number | null;
  note: string;
  created_at: string;
  source: string;
  activity_type: string;
  verification_status: string;
  credits: ActivityCredit[];
};

export type Invitation = {
  id: number;
  status: string;
  expires_at: string;
  username: string | null;
  kind: "link" | "username";
};

export type ShareLink = {
  id: number;
  expires_at: string;
  token: string;
  kind: "link";
};

export type OkResponse = {
  ok: true;
};

export type LogCreated = {
  progress: Progress;
  opened: string[];
};
