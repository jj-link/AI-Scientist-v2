import { useCallback, useEffect, useRef, useState } from "react";

export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type Idea = Record<string, Json>;
export interface IdeaRecord {
  id: string;
  job_id: string;
  revision: number;
  idea: Idea;
  original: Idea;
  errors: Record<string, string>;
  created_at: string;
  updated_at: string;
}
export type JobState =
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "completed"
  | "partial"
  | "failed"
  | "interrupted"
  | "unavailable";
export interface Job {
  id: string;
  kind: "idea" | "experiment";
  state: JobState;
  phase: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  run_id: string | null;
  title: string | null;
  idea_id: string | null;
  error: { code: string; message: string } | null;
  result: Record<string, Json> | null;
}
export interface JobEvent {
  sequence: number;
  timestamp: string;
  type: string;
  phase: string | null;
  data: Record<string, Json>;
}
export interface Workload {
  exp_name: string | null;
  num_workers: number | null;
  stage_iterations: Record<string, number | null>;
  num_seeds: number | null;
  execution_timeout: number | null;
  output_directory: string;
}
export interface Bootstrap {
  role_configs: { id: string; label: string }[];
  bfts_configs: {
    id: string;
    label: string;
    settings: Workload | null;
    error: string | null;
  }[];
  selected_role_config_id: string;
  selected_bfts_config_id: string;
  prerequisites: {
    ok: boolean;
    tools: { name: string; available: boolean; error: string | null }[];
  };
  active_job: Job | null;
  request_token: string;
  experiments_directory: string;
}
export interface Artifact {
  id: string;
  name: string;
  relative_path: string;
  kind: string;
  mime: string;
  attachment: boolean;
  size: number;
  updated_at: string;
}
export interface Run {
  id: string;
  directory: string;
  job_id: string | null;
  historical: boolean;
  title: string;
  hypothesis: string;
  state: JobState;
  status_label: string;
  phase: string | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  outputs_available: boolean;
}
export interface Review {
  kind: "paper" | "figures";
  label: string;
  attribution: string;
  artifact_id: string | null;
  data: Record<string, Json> | null;
  error: string | null;
}
export interface RunDetail extends Run {
  idea: Idea | null;
  idea_error: string | null;
  artifacts: Artifact[];
  papers: Artifact[];
  default_paper_id: string | null;
  figures: Artifact[];
  reviews: Review[];
  log_directories: { path: string; artifact_ids: string[] }[];
  summaries: {
    name: string;
    log_directory: string | null;
    artifact_id: string;
    data: Json;
    error: string | null;
  }[];
  stages: Record<string, Json>[];
  missing_outputs: string[];
}
export interface Credential {
  env: string | null;
  present: boolean;
}
export interface ModelRole {
  name: string;
  endpoint: string | null;
  model: string | null;
  max_tokens: number | null;
  effective_max_tokens: number | null;
  timeout: number;
  requires: string[];
  credential: Credential;
}
export interface ModelEndpoint {
  id: string;
  label: string;
  url: string;
  timeout: number;
  capabilities: string[];
  credential: Credential;
}
export interface ModelsView {
  config_id: string;
  roles: ModelRole[];
  endpoints: ModelEndpoint[];
}
export interface ModelsCheck {
  config_id: string;
  checked_at: string;
  ok: boolean;
  endpoints: {
    id: string;
    label: string;
    url: string;
    checked_at: string;
    ok: boolean;
    models: string[];
    roles: {
      name: string;
      model: string;
      listed: boolean;
      capabilities_declared: boolean;
      missing_capabilities: string[];
    }[];
    error: string | null;
  }[];
}

let token = "";
export function setRequestToken(value: string) {
  token = value;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: {
      message?: string;
      errors?: unknown;
      blockers?: string[];
      job_id?: string;
      [key: string]: unknown;
    },
  ) {
    super(detail.message || `Request failed (${status})`);
  }
}
export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "The local server could not be reached.";
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body
        ? { "Content-Type": "application/json", "X-Studio-Token": token }
        : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let detail: ApiError["detail"] = {
      message: `Request failed (${response.status})`,
    };
    try {
      const body = await response.json();
      detail =
        typeof body.detail === "string"
          ? { message: body.detail }
          : body.detail || detail;
    } catch {
      /* Non-JSON connection errors retain their HTTP status. */
    }
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}
export function mutate<T>(
  path: string,
  body: unknown = {},
  method = "POST",
): Promise<T> {
  return request<T>(path, { method, body: JSON.stringify(body) });
}
export function artifactUrl(
  runId: string,
  artifactId: string,
  download = false,
) {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}${download ? "?download=true" : ""}`;
}
export function isActive(state?: string) {
  return state === "starting" || state === "running" || state === "stopping";
}

/** Keep last good data on transient errors; never treat connectivity as job state. */
export function useApi<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [revision, setRevision] = useState(0);
  const lastPath = useRef(path);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    if (lastPath.current !== path) {
      setData(undefined);
      setError(null);
      lastPath.current = path;
    }
    if (!path) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    const load = async () => {
      if (document.hidden) return;
      try {
        const result = await request<T>(path, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setData(result);
          setError(null);
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (refreshMs) timer = window.setTimeout(load, refreshMs);
        }
      }
    };
    const visible = () => {
      if (!document.hidden) {
        clearTimeout(timer);
        void load();
      }
    };
    setLoading(true);
    void load();
    document.addEventListener("visibilitychange", visible);
    return () => {
      controller.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [path, refreshMs, revision]);
  return { data, error, loading, refresh };
}
