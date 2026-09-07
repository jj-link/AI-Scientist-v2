import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  RefreshCw,
  Save,
  XCircle,
} from "lucide-react";
import {
  ApiError,
  discoverEndpointModels,
  mutate,
  request,
  useApi,
  type AssistantSettings,
  type Credential,
  type ModelConfigEditor,
  type ModelConfigEditorRole,
  type ModelEndpointPatch,
  type ModelRolePatch,
  type ModelsCheck,
  type ModelsView,
} from "../api";
import { ConfigSelect, ErrorNotice, PageHeading } from "../components";
import { useStudio } from "../studio";
import CodexProviderCard from "./CodexProviderCard";

interface ModelDiscovery {
  status: "idle" | "loading" | "ok" | "error";
  models: string[];
  error: string | null;
}

const CUSTOM_MODEL_HINT =
  "Enter the model ID exactly as served by the endpoint.";

const DRAFT_STORAGE_PREFIX = "studio.models.draft.";

interface StoredDraft {
  baseRevision: string;
  roles: Record<string, ModelConfigEditorRole>;
  endpoints: Record<string, ModelEndpointPatch>;
}

function draftKey(configId: string) {
  return DRAFT_STORAGE_PREFIX + configId;
}

function readStoredDraft(
  configId: string,
): StoredDraft | null {
  try {
    const raw = sessionStorage.getItem(draftKey(configId));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.baseRevision === "string") return parsed;
  } catch {
    /* A stale draft must never block editing. */
  }
  return null;
}

function writeStoredDraft(configId: string, draft: StoredDraft) {
  try {
    sessionStorage.setItem(draftKey(configId), JSON.stringify(draft));
  } catch {
    /* Draft persistence is best effort. */
  }
}

function clearStoredDraft(configId: string) {
  try {
    sessionStorage.removeItem(draftKey(configId));
  } catch {
    /* Draft persistence is best effort. */
  }
}

function countChangedFields(
  saved: ModelConfigEditor,
  roles: Record<string, ModelConfigEditorRole>,
  endpoints: Record<string, ModelEndpointPatch>,
): number {
  let count = 0;
  for (const [name, draft] of Object.entries(roles)) {
    const original = saved.roles[name];
    if (!original) continue;
    for (const field of [
      "endpoint",
      "model",
      "max_tokens",
      "temperature",
      "timeout",
      "api_key_env",
    ] as const) {
      if (draft[field] !== original[field]) count++;
    }
  }
  for (const [id, draft] of Object.entries(endpoints)) {
    const original = saved.endpoints[id];
    if (!original) continue;
    for (const field of ["base_url", "api_key_env", "timeout"] as const) {
      if (draft[field] !== original[field]) count++;
    }
  }
  return count;
}

function fieldValue(value: number | string | null): string {
  return value === null ? "" : String(value);
}

function parseNumber(value: string): number | null | undefined {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function CredentialState({ credential }: { credential: Credential }) {
  if (credential.method === "codex") return <span>ChatGPT OAuth · see Codex sign-in</span>;
  return (
    <span>
      {credential.env ? (
        <>
          <code>{credential.env}</code> ·{" "}
          {credential.present ? "Value present" : "Value not present"}
        </>
      ) : (
        "No credential environment variable configured"
      )}
    </span>
  );
}

type FieldErrors = Record<string, { message: string; code: string }>;

function CrashAssistantCard({
  configurationRevision,
}: {
  configurationRevision: string | null;
}) {
  const { bootstrap } = useStudio();
  const settings = useApi<AssistantSettings>(
    "/api/crash-assistant/settings",
  );
  const [configId, setConfigId] = useState("");
  const [role, setRole] = useState("");
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [savedView, setSavedView] = useState<AssistantSettings | null>(null);
  const view = useApi<ModelsView>(
    configId ? `/api/models?config_id=${encodeURIComponent(configId)}` : null,
  );
  const currentView = view.data?.config_id === configId ? view.data : undefined;
  const textRoles = (currentView?.roles ?? []).filter(
    (candidate) =>
      candidate.endpoint &&
      currentView?.endpoints.find(
        (endpoint) =>
          endpoint.id === candidate.endpoint &&
          endpoint.capabilities.includes("text"),
      ),
  );
  const loaded = settings.data;
  useEffect(() => {
    if (!touched && loaded?.config_id) {
      setConfigId(loaded.config_id);
      setRole(loaded.role ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded?.config_id, loaded?.role]);
  useEffect(() => {
    if (configurationRevision !== null) {
      settings.refresh();
      view.refresh();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configurationRevision]);
  const shown = savedView ?? loaded;
  async function save(enabled: boolean) {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await mutate<AssistantSettings>(
        "/api/crash-assistant/settings",
        enabled
          ? { enabled: true, config_id: configId, role }
          : { enabled: false, config_id: null, role: null },
        "PUT",
      );
      setSavedView(result);
      settings.refresh();
    } catch (error) {
      setSaveError(error);
    } finally {
      setSaving(false);
    }
  }
  const canEnable = Boolean(configId && role && textRoles.some((item) => item.name === role));
  return (
    <section className="card stack" aria-labelledby="assistant-heading">
      <div className="card-header">
        <div>
          <h2 id="assistant-heading">Crash assistant</h2>
          <p className="muted">
            Saved separately from the configuration above. When enabled, a
            failed job sends one bounded, credential-redacted Technical
            details excerpt to the selected endpoint for newly started jobs
            only; redaction may not remove private research content. GitHub
            reports are public on <code>jj-link/AI-Scientist-v2</code> and
            require separate exact-content approval.
          </p>
          <p className="muted">After editing a role above, click Save assistant settings to capture its new assignment. Existing job snapshots do not change.</p>
        </div>
      </div>
      <div className="field">
        <label htmlFor="assistant-enabled">Enable crash assistant</label>
        <select
          id="assistant-enabled"
          value={shown?.enabled ? "on" : "off"}
          onChange={(event) => void save(event.target.value === "on")}
          disabled={saving || !loaded}
        >
          <option value="off">Disabled</option>
          <option value="on" disabled={!canEnable && !shown?.enabled}>
            Enabled
          </option>
        </select>
        {!canEnable && (
          <small className="muted">
            {shown?.enabled
              ? "Disabling stays available even if the saved preset is broken."
              : "Enable requires a preset with a role whose endpoint declares text capability. Use this page's configuration editor to assign one, then click Save assistant settings."}
          </small>
        )}
      </div>
      {loaded ? (
        <>
          <div className="field">
            <label htmlFor="assistant-preset">Preset</label>
            <select
              id="assistant-preset"
              value={configId}
              onChange={(event) => {
                setTouched(true);
                setConfigId(event.target.value);
                setRole("");
              }}
            >
              {!configId && <option value="">Select a preset</option>}
              {bootstrap.role_configs.map((config) => (
                <option key={config.id} value={config.id}>
                  {config.label}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="assistant-role">Role — model — endpoint</label>
            <select
              id="assistant-role"
              value={role}
              onChange={(event) => {
                setTouched(true);
                setRole(event.target.value);
              }}
            >
              {!role && <option value="">Select a text role</option>}
              {role &&
                !textRoles.some((item) => item.name === role) && (
                  <option value={role}>
                    {role} (saved, currently not text capable)
                  </option>
                )}
              {textRoles.map((candidate) => (
                <option key={candidate.name} value={candidate.name}>
                  {candidate.name} — {candidate.model || "model not set"} —{" "}
                  {currentView?.endpoints.find(
                    (endpoint) => endpoint.id === candidate.endpoint,
                  )?.label ?? candidate.endpoint}
                </option>
              ))}
            </select>
          </div>
          <div className="toolbar">
            <button
              className="button primary"
              onClick={() => void save(true)}
              disabled={saving || !canEnable}
            >
              Save assistant settings
            </button>
            <button
              className="button secondary"
              onClick={() => void save(false)}
              disabled={saving}
            >
              Disable
            </button>
          </div>
        </>
      ) : null}
      <ErrorNotice error={saveError} />
      {shown && (
        <dl className="results-record metadata">
          <dt>Saved assignment</dt>
          <dd>
            {shown.enabled && shown.config_id
              ? `${shown.role} · ${shown.model} · ${shown.endpoint}`
              : "Disabled"}
          </dd>
          <dt>Effective limits</dt>
          <dd>
            {shown.enabled && shown.provider === "openai-codex"
              ? `Codex-managed output limits · ${shown.timeout} s timeout`
              : shown.enabled && shown.max_tokens
                ? `${shown.max_tokens.toLocaleString()} tokens · ${shown.timeout} s timeout`
                : "Not applicable"}
          </dd>
          <dt>Credential</dt>
          <dd>
            {shown.credential ? (
              <CredentialState credential={shown.credential} />
            ) : (
              "None"
            )}
          </dd>
        </dl>
      )}
    </section>
  );
}

const REVISION_CHANGED_MESSAGE =
  "The preset changed after your last edit; reload before saving.";

export default function Models() {
  const { bootstrap, roleConfigId } = useStudio();
  const editor = useApi<ModelConfigEditor>(
    roleConfigId
      ? `/api/models/editor?config_id=${encodeURIComponent(roleConfigId)}`
      : null,
  );
  const display = useApi<ModelsView>(
    roleConfigId
      ? `/api/models?config_id=${encodeURIComponent(roleConfigId)}`
      : null,
  );
  const [saved, setSaved] = useState<ModelConfigEditor | null>(null);
  const [roleDrafts, setRoleDrafts] = useState<
    Record<string, ModelConfigEditorRole>
  >({});
  const [endpointDrafts, setEndpointDrafts] = useState<
    Record<string, ModelEndpointPatch>
  >({});
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [apiError, setApiError] = useState<unknown>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [staleDraft, setStaleDraft] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
  const [expandedRoles, setExpandedRoles] = useState<string[]>([]);
  const [check, setCheck] = useState<ModelsCheck | null>(null);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const [discoveries, setDiscoveries] = useState<
    Record<string, ModelDiscovery>
  >({});
  const discoveryRequests = useRef(new Map<string, AbortController>());
  const clearAvailability = useCallback(() => {
    for (const controller of discoveryRequests.current.values()) controller.abort();
    discoveryRequests.current.clear();
    setDiscoveries({});
    setCheck(null);
  }, []);
  const probe = useRef<AbortController | null>(null);
  const baseRevision = useRef<string | null>(null);
  const selectedConfig = useRef(roleConfigId);
  selectedConfig.current = roleConfigId;
  const changed = saved
    ? countChangedFields(saved, roleDrafts, endpointDrafts)
    : 0;
  const dirty = changed > 0;
  const positive = (value: unknown) => value === null ||
    (typeof value === "number" && Number.isFinite(value) && value > 0);
  const invalidDraft = Object.values(roleDrafts).some((role) =>
    !role.endpoint || !role.model?.trim() ||
    !positive(role.timeout) || !positive(role.max_tokens) ||
    (role.max_tokens !== null && !Number.isInteger(role.max_tokens)) ||
    (role.temperature !== null && (typeof role.temperature !== "number" ||
      !Number.isFinite(role.temperature) || role.temperature < 0 || role.temperature > 2))) ||
    Object.entries(endpointDrafts).some(([id, endpoint]) =>
      (saved?.endpoints[id]?.provider !== "openai-codex" && !endpoint.base_url?.trim()) || !positive(endpoint.timeout));
  const savingRequest = useRef(false);
  const editorSurface = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setDiscoveries({});
    return () => {
      for (const controller of discoveryRequests.current.values()) controller.abort();
      discoveryRequests.current.clear();
    };
  }, [roleConfigId, saved?.revision]);
  useEffect(() => {
    const loaded = editor.data;
    if (!loaded || loaded.config_id !== roleConfigId) return;
    if (saved?.config_id === loaded.config_id) {
      if (loaded.revision === saved.revision) return;
      if (dirty) {
        setStaleDraft(true);
        return;
      }
    }
    const stored = readStoredDraft(roleConfigId);
    baseRevision.current = stored?.baseRevision ?? loaded.revision;
    setStaleDraft(Boolean(stored && stored.baseRevision !== loaded.revision));
    setSaved(loaded);
    setRoleDrafts(stored?.roles ?? { ...loaded.roles });
    setEndpointDrafts(stored?.endpoints ?? { ...loaded.endpoints });
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
  }, [editor.data, roleConfigId]);
  useEffect(() => {
    probe.current?.abort();
    probe.current = null;
    setCheck(null);
    setCheckError(null);
    setChecking(false);
    return () => {
      probe.current?.abort();
      probe.current = null;
    };
  }, [roleConfigId]);
  useEffect(() => {
    if (!saved || saved.config_id !== roleConfigId) return;
    if (!dirty) {
      clearStoredDraft(roleConfigId);
      return;
    }
    writeStoredDraft(roleConfigId, {
      baseRevision: baseRevision.current ?? saved.revision,
      roles: roleDrafts,
      endpoints: endpointDrafts,
    });
  }, [roleDrafts, endpointDrafts, dirty, roleConfigId, saved]);
  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  function setRole(name: string, patch: Partial<ModelConfigEditorRole>) {
    setFieldErrors({});
    setRoleDrafts((current) => ({
      ...current,
      [name]: { ...current[name], ...patch },
    }));
  }
  function setEndpoint(id: string, patch: Partial<ModelEndpointPatch>) {
    setFieldErrors({});
    setEndpointDrafts((current) => ({
      ...current,
      [id]: { ...current[id], ...patch },
    }));
  }
  function switchPreset(nextId: string): boolean {
    if (dirty && !window.confirm("Discard unsaved edits to this preset?"))
      return false;
    if (roleConfigId && dirty) clearStoredDraft(roleConfigId);
    resetEditor();
    // ConfigSelect owns the selected preset update.
    return true;
  }
  function resetEditor() {
    baseRevision.current = null;
    setSaved(null);
    setRoleDrafts({});
    setEndpointDrafts({});
    setFieldErrors({});
    setApiError(null);
    setRevisionConflict(false);
    setStaleDraft(false);
    setSaveNotice("");
    setExpandedRoles([]);
    setCheck(null);
    setDiscoveries({});
    for (const controller of discoveryRequests.current.values()) controller.abort();
    discoveryRequests.current.clear();
  }
  async function detectRoleModels(roleName: string) {
    const endpoint = roleDrafts[roleName]?.endpoint;
    if (!endpoint || !saved || discoveryRequests.current.has(endpoint)) return;
    const connection = endpointDrafts[endpoint];
    const original = saved.endpoints[endpoint];
    if (!connection || !original ||
        connection.base_url !== original.base_url ||
        connection.api_key_env !== original.api_key_env ||
        connection.timeout !== original.timeout) return;
    const configId = saved.config_id;
    const controller = new AbortController();
    discoveryRequests.current.set(endpoint, controller);
    setDiscoveries((current) => ({
      ...current,
      [endpoint]: { status: "loading", models: [], error: null },
    }));
    try {
      const result = await discoverEndpointModels(configId, endpoint, controller.signal);
      if (controller.signal.aborted || selectedConfig.current !== configId ||
          result.config_id !== configId || result.endpoint !== endpoint) return;
      setDiscoveries((current) => ({
        ...current,
        [endpoint]: {
          status: result.ok ? "ok" : "error",
          models: result.ok ? [...new Set(result.models)] : [],
          error: result.error,
        },
      }));
    } catch {
      if (controller.signal.aborted || selectedConfig.current !== configId) return;
      setDiscoveries((current) => ({
        ...current,
        [endpoint]: { status: "error", models: [], error: "Model listing failed. Retry or enter a model ID in Advanced." },
      }));
    } finally {
      if (discoveryRequests.current.get(endpoint) === controller)
        discoveryRequests.current.delete(endpoint);
    }
  }
  async function saveChanges() {
    if (!saved || !dirty || savingRequest.current || invalidDraft || revisionConflict || staleDraft) return;
    for (const input of editorSurface.current?.querySelectorAll("input") ?? []) {
      if (!input.reportValidity()) return;
    }
    savingRequest.current = true;
    setSaving(true);
    setApiError(null);
    setFieldErrors({});
    setSaveNotice("");
    const rolePatch: Record<string, ModelRolePatch> = {};
    for (const [name, draft] of Object.entries(roleDrafts)) {
      const original = saved.roles[name];
      if (!original) continue;
      const patch: ModelRolePatch = {};
      if (draft.endpoint !== original.endpoint) (patch.endpoint = draft.endpoint);
      if (draft.model !== original.model) (patch.model = draft.model);
      if (draft.max_tokens !== original.max_tokens) (patch.max_tokens = draft.max_tokens);
      if (draft.temperature !== original.temperature) (patch.temperature = draft.temperature);
      if (draft.timeout !== original.timeout) (patch.timeout = draft.timeout);
      if (draft.api_key_env !== original.api_key_env) (patch.api_key_env = draft.api_key_env);
      if (Object.keys(patch).length) rolePatch[name] = patch;
    }
    const endpointPatch: Record<string, ModelEndpointPatch> = {};
    for (const [id, draft] of Object.entries(endpointDrafts)) {
      const original = saved.endpoints[id];
      if (!original) continue;
      const patch: ModelEndpointPatch = {};
      if (draft.base_url !== original.base_url) (patch.base_url = draft.base_url);
      if (draft.api_key_env !== original.api_key_env) (patch.api_key_env = draft.api_key_env);
      if (draft.timeout !== original.timeout) (patch.timeout = draft.timeout);
      if (Object.keys(patch).length) endpointPatch[id] = patch;
    }
    const payload = {
      config_id: roleConfigId,
      expected_revision: saved.revision,
      roles: rolePatch,
      endpoints: endpointPatch,
    };
    try {
      const result = await mutate<ModelConfigEditor>(
        "/api/models/editor",
        payload,
        "PATCH",
      );
      if (result.config_id !== selectedConfig.current) return;
      baseRevision.current = result.revision;
      setSaved(result);
      setRoleDrafts({ ...result.roles });
      setEndpointDrafts({ ...result.endpoints });
      setCheck(null);
      setSaveNotice("Configuration saved");
      if (roleConfigId) clearStoredDraft(roleConfigId);
      display.refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setRevisionConflict(true);
        setApiError(null);
        setFieldErrors({});
      } else if (error instanceof ApiError && error.status === 422) {
        const errors = error.detail?.errors;
        if (Array.isArray(errors)) {
          const mapped: FieldErrors = {};
          for (const issue of errors) {
            if (issue?.field && issue?.message)
              mapped[issue.field] = {
                message: issue.message,
                code: issue.code || "invalid",
              };
          }
          setFieldErrors(mapped);
          setExpandedRoles((current) => [
            ...new Set([
              ...current,
              ...Object.keys(saved.roles).filter((name) =>
                Object.keys(mapped).some((field) =>
                  field.startsWith(`roles.${name}.`),
                ),
              ),
            ]),
          ]);
        }
        setApiError(error);
      } else {
        setApiError(error);
      }
    } finally {
      savingRequest.current = false;
      setSaving(false);
    }
  }
  function discardEdits() {
    if (
      saved &&
      dirty &&
      !window.confirm("Discard unsaved edits?")
    )
      return;
    if (roleConfigId && dirty) clearStoredDraft(roleConfigId);
    if (saved) {
      setRoleDrafts({ ...saved.roles });
      setEndpointDrafts({ ...saved.endpoints });
    }
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
    setCheck(null);
    if (roleConfigId) clearStoredDraft(roleConfigId);
  }
  function reloadConfiguration() {
    if (saved && dirty && !window.confirm("Discard unsaved edits?")) return;
    if (roleConfigId && dirty) clearStoredDraft(roleConfigId);
    resetEditor();
    editor.refresh();
    display.refresh();
  }
  async function checkAvailability() {
    if (probe.current || !saved || dirty || roleConfigId == null) return;
    const controller = new AbortController();
    probe.current = controller;
    setChecking(true);
    setCheckError(null);
    try {
      const result = await request<ModelsCheck>("/api/models/check", {
        method: "POST",
        body: JSON.stringify({ config_id: roleConfigId }),
        signal: controller.signal,
      });
      if (
        !controller.signal.aborted &&
        selectedConfig.current === roleConfigId &&
        result.config_id === roleConfigId
      )
        setCheck(result);
    } catch (failure) {
      if (!controller.signal.aborted && selectedConfig.current === roleConfigId)
        setCheckError(failure);
    } finally {
      if (probe.current === controller) {
        probe.current = null;
        setChecking(false);
      }
    }
  }
  const currentDisplay =
    display.data?.config_id === roleConfigId ? display.data : undefined;
  const currentCheck = check?.config_id === roleConfigId ? check : null;
  const loadingEditor = editor.loading && !saved;
  const presetLabel =
    bootstrap.role_configs.find((config) => config.id === roleConfigId)
      ?.label ?? "Selected preset";
  return (
    <div className="models-page stack" ref={editorSurface}>
      <PageHeading eyebrow="Local configuration" title="Models">
        <p>
          Choose the model for each research role. Changes save to the
          selected YAML preset for future Studio jobs; running jobs keep
          their snapshots. Saving does not start or restart models.
        </p>
      </PageHeading>
      <div className="card models-preset">
        <ConfigSelect
          id="models-role-config"
          label="Selected preset"
          disabled={saving}
          onBeforeChange={switchPreset}
        />
        <button
          className="button secondary"
          onClick={reloadConfiguration}
          disabled={loadingEditor || saving}
        >
          <RefreshCw size={16} aria-hidden="true" />
          Reload configuration
        </button>
      </div>
      <CodexProviderCard onAuthChange={clearAvailability} />
      {loadingEditor && (
        <p role="status">Loading model configuration…</p>
      )}
      {editor.error != null && !revisionConflict && (
        <>
          <ErrorNotice error={editor.error} />
          <div className="notice">
            <p>
              Your entered values are preserved. Reconnect or fix the
              problem, then save or reload.
            </p>
          </div>
        </>
      )}
      {display.error != null && (
        <div className="notice">
          <p>
            Configuration could not be refreshed. Reconnect to the local
            server, then retry; no availability result has been inferred.
          </p>
          <button className="button secondary" onClick={() => display.refresh()}>
            Retry configuration
          </button>
        </div>
      )}
      {saved && (
        <div className="stack models-content">
          <section className="stack" aria-labelledby="model-roles-heading">
            <div>
              <h2 id="model-roles-heading">Role assignments</h2>
              <p className="muted">
                Set a model and endpoint for each role. Open Advanced for
                token, temperature, timeout, and credential overrides.
              </p>
            </div>
            <div className="models-role-list">
              {Object.entries(saved.roles).map(([name, original]) => {
                const draft = roleDrafts[name];
                if (!draft) return null;
                const codex = saved.endpoints[draft.endpoint ?? ""]?.provider === "openai-codex";
                const displayRole = currentDisplay?.roles.find(
                  (item) => item.name === name,
                );
                const roleError = fieldErrors[`roles.${name}`]?.message;
                const roleId = (suffix: string) => `role-${name}-${suffix}`;
                const errorId = (suffix: string) => `${roleId(suffix)}-error`;
                const discovery =
                  saved.endpoints[draft.endpoint ?? ""]
                    ? discoveries[draft.endpoint ?? ""]
                    : undefined;
                const connection = endpointDrafts[draft.endpoint ?? ""];
                const originalConnection = saved.endpoints[draft.endpoint ?? ""];
                const connectionDirty = Boolean(connection && originalConnection &&
                  (connection.base_url !== originalConnection.base_url ||
                   connection.api_key_env !== originalConnection.api_key_env ||
                   connection.timeout !== originalConnection.timeout));
                const listedModels = connectionDirty ? [] : discovery?.models ?? [];
                const describedBy = (field: string) =>
                  [
                    ["max_tokens", "temperature", "timeout", "api_key_env"].includes(field)
                      ? roleId(`${field}-hint`)
                      : null,
                    fieldErrors[`roles.${name}.${field}`] ? errorId(field) : null,
                  ]
                    .filter(Boolean)
                    .join(" ") || undefined;
                const advancedOpen = expandedRoles.includes(name);
                return (
                  <fieldset
                    key={name}
                    className="models-role"
                    disabled={saving}
                  >
                    <legend className="sr-only">{name}</legend>
                    <div className="models-role-primary">
                      <div className="models-role-name">
                        <h3>{name}</h3>
                        <span className="muted metadata">
                          {original.requires.length
                            ? `Requires ${original.requires.join(", ")}`
                            : "No required capabilities"}
                        </span>
                      </div>
                      <div
                        className="field"
                        data-changed={draft.endpoint !== original.endpoint}
                      >
                        <label htmlFor={roleId("endpoint")}>Endpoint</label>
                        <select
                          id={roleId("endpoint")}
                          value={draft.endpoint ?? ""}
                          onChange={(event) => {
                            const next = event.target.value || null;
                            if ((draft.endpoint ?? null) !== next) {
                              const codex = saved.endpoints[next ?? ""]?.provider === "openai-codex";
                              setRole(name, {
                                endpoint: next, model: null,
                                ...(codex ? { max_tokens: null, temperature: null, api_key_env: null } : {}),
                              });
                            }
                          }}
                          aria-invalid={
                            !!fieldErrors[`roles.${name}.endpoint`]
                          }
                          aria-describedby={describedBy("endpoint")}
                        >
                          <option value="">Not assigned</option>
                          {Object.keys(saved.endpoints).map((id) => (
                            <option key={id} value={id}>
                              {currentDisplay?.endpoints.find(
                                (endpoint) => endpoint.id === id,
                              )?.label ?? id}
                            </option>
                          ))}
                        </select>
                        {fieldErrors[`roles.${name}.endpoint`] && (
                          <p
                            className="field-error"
                            role="alert"
                            id={errorId("endpoint")}
                          >
                            {fieldErrors[`roles.${name}.endpoint`].message}
                          </p>
                        )}
                      </div>
                      <div
                        className="field"
                        data-changed={draft.model !== original.model}
                      >
                        <label htmlFor={roleId("model")}>Model</label>
                        <select
                          id={roleId("model")}
                          value={draft.model ?? ""}
                          onChange={(event) => setRole(name, { model: event.target.value || null })}
                          aria-invalid={!!fieldErrors[`roles.${name}.model`]}
                          aria-describedby={describedBy("model")}
                        >
                          <option value="">Not selected</option>
                          {draft.model && !listedModels.includes(draft.model) && (
                            <option value={draft.model}>{draft.model} (current, not listed)</option>
                          )}
                          {listedModels.map((model) => <option key={model} value={model}>{model}</option>)}
                        </select>
                        <div className="models-discovery-row">
                          <button
                            type="button"
                            className="button secondary models-discover-button"
                            onClick={() => void detectRoleModels(name)}
                            disabled={
                              saving ||
                              !draft.endpoint ||
                              connectionDirty ||
                              discovery?.status === "loading"
                            }
                          >
                            <RefreshCw size={14} aria-hidden="true" />
                            {discovery?.status === "loading"
                              ? "Detecting models…"
                              : discovery ? "Refresh models" : "Detect models"}
                          </button>
                          <span
                            className="muted metadata"
                            role="status"
                            aria-live="polite"
                          >
                            {connectionDirty
                              ? "Save endpoint changes before detecting models."
                              : discovery?.status === "loading"
                                ? "Detecting models…"
                                : discovery?.status === "ok"
                                  ? listedModels.length
                                    ? `${listedModels.length} models listed.`
                                    : "No models listed. Enter a model ID in Advanced."
                                  : discovery?.status === "error"
                                    ? discovery.error || "Detection failed. Retry or enter a model ID in Advanced."
                                    : "Detect models to choose from this endpoint."}
                          </span>
                        </div>
                        {fieldErrors[`roles.${name}.model`] && (
                          <p
                            className="field-error"
                            role="alert"
                            id={errorId("model")}
                          >
                            {fieldErrors[`roles.${name}.model`].message}
                          </p>
                        )}
                      </div>
                      <button
                        className="button secondary models-advanced-toggle"
                        aria-expanded={advancedOpen}
                        aria-controls={roleId("advanced")}
                        aria-label={`Advanced settings for ${name}`}
                        onClick={() =>
                          setExpandedRoles((current) =>
                            advancedOpen
                              ? current.filter((item) => item !== name)
                              : [...current, name],
                          )
                        }
                      >
                        Advanced
                        <ChevronDown size={16} aria-hidden="true" />
                      </button>
                    </div>
                    {roleError && (
                      <p className="field-error" role="alert">
                        {roleError}
                      </p>
                    )}
                    <div
                      id={roleId("advanced")}
                      className="models-role-advanced"
                      hidden={!advancedOpen}
                    >
                      <div className="field" data-changed={draft.model !== original.model}>
                        <label htmlFor={roleId("custom-model")}>Custom model ID</label>
                        <input
                          id={roleId("custom-model")}
                          value={draft.model ?? ""}
                          onChange={(event) => setRole(name, { model: event.target.value || null })}
                          aria-invalid={!!fieldErrors[`roles.${name}.model`]}
                          aria-describedby={[roleId("custom-model-hint"), describedBy("model")].filter(Boolean).join(" ")}
                        />
                        <small className="muted" id={roleId("custom-model-hint")}>{CUSTOM_MODEL_HINT}</small>
                      </div>
                      {codex && <p className="notice">Codex manages output-token limits, sampling, and ChatGPT credentials. These overrides are cleared when selecting Codex; timeout still applies.</p>}
                      <div className="field-row">
                        <div
                          className="field"
                          data-changed={draft.max_tokens !== original.max_tokens}
                        >
                          <label htmlFor={roleId("max_tokens")}>
                            Output token limit
                          </label>
                          <input
                            id={roleId("max_tokens")}
                            disabled={codex}
                            type="number"
                            min={1}
                            step={1}
                            value={fieldValue(draft.max_tokens)}
                            onChange={(event) =>
                              setRole(name, {
                                max_tokens: parseNumber(event.target.value)
                                  ?? (event.target.value.trim()
                                    ? undefined
                                    : null),
                              })
                            }
                            aria-invalid={
                              !!fieldErrors[`roles.${name}.max_tokens`]
                            }
                            aria-describedby={describedBy("max_tokens")}
                          />
                          <small
                            className="muted"
                            id={roleId("max_tokens-hint")}
                          >
                            {codex ? "Managed by Codex." : "Blank uses the calling task's token limit."}
                          </small>
                          {fieldErrors[`roles.${name}.max_tokens`] && (
                            <p
                              className="field-error"
                              role="alert"
                              id={errorId("max_tokens")}
                            >
                              {fieldErrors[`roles.${name}.max_tokens`].message}
                            </p>
                          )}
                        </div>
                        <div
                          className="field"
                          data-changed={draft.temperature !== original.temperature}
                        >
                          <label htmlFor={roleId("temperature")}>
                            Temperature (0–2)
                          </label>
                          <input
                            id={roleId("temperature")}
                            disabled={codex}
                            type="number"
                            min={0}
                            max={2}
                            step="any"
                            value={fieldValue(draft.temperature)}
                            onChange={(event) =>
                              setRole(name, {
                                temperature: parseNumber(event.target.value)
                                  ?? (event.target.value.trim()
                                    ? undefined
                                    : null),
                              })
                            }
                            aria-invalid={
                              !!fieldErrors[`roles.${name}.temperature`]
                            }
                            aria-describedby={describedBy("temperature")}
                          />
                          <small
                            className="muted"
                            id={roleId("temperature-hint")}
                          >
                            {codex ? "Managed by Codex." : "Blank uses the calling task's temperature."}
                          </small>
                          {fieldErrors[`roles.${name}.temperature`] && (
                            <p
                              className="field-error"
                              role="alert"
                              id={errorId("temperature")}
                            >
                              {
                                fieldErrors[`roles.${name}.temperature`]
                                  .message
                              }
                            </p>
                          )}
                        </div>
                        <div
                          className="field"
                          data-changed={draft.timeout !== original.timeout}
                        >
                          <label htmlFor={roleId("timeout")}>
                            Timeout (seconds)
                          </label>
                          <input
                            id={roleId("timeout")}
                            type="number"
                            min={0}
                            step="any"
                            value={fieldValue(draft.timeout)}
                            onChange={(event) =>
                              setRole(name, {
                                timeout: parseNumber(event.target.value)
                                  ?? (event.target.value.trim()
                                    ? undefined
                                    : null),
                              })
                            }
                            aria-invalid={
                              !!fieldErrors[`roles.${name}.timeout`]
                            }
                            aria-describedby={describedBy("timeout")}
                          />
                          <small className="muted" id={roleId("timeout-hint")}>
                            Blank inherits{" "}
                            {saved.endpoints[draft.endpoint ?? ""]
                              ? (saved.endpoints[
                                  draft.endpoint ?? ""
                                ]?.timeout ?? 600)
                              : 600}{" "}
                            seconds from the endpoint.
                          </small>
                          {fieldErrors[`roles.${name}.timeout`] && (
                            <p
                              className="field-error"
                              role="alert"
                              id={errorId("timeout")}
                            >
                              {fieldErrors[`roles.${name}.timeout`].message}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="field">
                        <label htmlFor={roleId("api_key_env")}>
                          Credential environment name
                        </label>
                        <input
                          id={roleId("api_key_env")}
                          disabled={codex}
                          type="text"
                          value={draft.api_key_env ?? ""}
                          onChange={(event) =>
                            setRole(name, {
                              api_key_env: event.target.value || null,
                            })
                          }
                          aria-invalid={
                            !!fieldErrors[`roles.${name}.api_key_env`]
                          }
                          aria-describedby={describedBy("api_key_env")}
                        />
                        <small
                          className="muted"
                          id={roleId("api_key_env-hint")}
                        >
                          {codex ? "Uses the Codex sign-in above." : "Blank inherits the endpoint credential. Environment-variable names only."}
                          {displayRole && !codex && (
                            <>
                              {" "}
                              Saved credential:{" "}
                              <CredentialState
                                credential={displayRole.credential}
                              />
                            </>
                          )}
                        </small>
                        {fieldErrors[`roles.${name}.api_key_env`] && (
                          <p
                            className="field-error"
                            role="alert"
                            id={errorId("api_key_env")}
                          >
                            {fieldErrors[`roles.${name}.api_key_env`].message}
                          </p>
                        )}
                      </div>
                    </div>
                  </fieldset>
                );
              })}
            </div>
          </section>
          <section className="stack" aria-labelledby="model-endpoints-heading">
            <div>
              <h2 id="model-endpoints-heading">Endpoint connections</h2>
              <p className="muted">
                Only environment-variable names are editable; credential
                values are never displayed or stored here.
              </p>
            </div>
            <div className="editor-grid">
              {Object.entries(saved.endpoints).map(([id, original]) => {
                const draft = endpointDrafts[id];
                if (!draft) return null;
                const displayEndpoint = currentDisplay?.endpoints.find(
                  (item) => item.id === id,
                );
                const capabilities = displayEndpoint?.capabilities ?? [];
                const codex = original.provider === "openai-codex";
                const endpointId = (field: string) => `endpoint-${id}-${field}`;
                const endpointErrorId = (field: string) =>
                  `${endpointId(field)}-error`;
                const endpointDescribedBy = (field: string) =>
                  fieldErrors[`endpoints.${id}.${field}`]
                    ? endpointErrorId(field)
                    : undefined;
                return (
                  <fieldset
                    key={id}
                    className="card stack editor-card"
                    disabled={saving}
                  >
                    <legend className="editor-entry">{displayEndpoint?.label ?? id}</legend>
                    <p className="muted metadata">
                      Provides:{" "}
                      {capabilities.length ? capabilities.join(", ") : "None declared"}
                    </p>
                    {codex && <p className="muted">Managed Codex server and OAuth credentials. Sign in above; no API key or custom server address is used.</p>}
                    <div
                      className="field"
                      data-changed={draft.base_url !== original.base_url}
                    >
                      <label htmlFor={endpointId("base_url")}>Endpoint URL</label>
                      <input
                        id={endpointId("base_url")}
                        type="text"
                        value={codex ? displayEndpoint?.url ?? "Managed by Codex" : draft.base_url ?? ""}
                        disabled={codex}
                        onChange={(event) =>
                          setEndpoint(id, {
                            base_url: event.target.value || null,
                          })
                        }
                        aria-invalid={!!fieldErrors[`endpoints.${id}.base_url`]}
                        aria-describedby={endpointDescribedBy("base_url")}
                      />
                      {fieldErrors[`endpoints.${id}.base_url`] && (
                        <p
                          className="field-error"
                          role="alert"
                          id={endpointErrorId("base_url")}
                        >
                          {fieldErrors[`endpoints.${id}.base_url`].message}
                        </p>
                      )}
                    </div>
                    <div className="field-row">
                      <div
                        className="field"
                        data-changed={draft.api_key_env !== original.api_key_env}
                      >
                        <label htmlFor={endpointId("api_key_env")}>
                          Credential environment name
                        </label>
                        <input
                          id={endpointId("api_key_env")}
                          disabled={codex}
                          placeholder={codex ? "ChatGPT sign-in" : undefined}
                          type="text"
                          value={draft.api_key_env ?? ""}
                          onChange={(event) =>
                            setEndpoint(id, {
                              api_key_env: event.target.value || null,
                            })
                          }
                          aria-invalid={
                            !!fieldErrors[`endpoints.${id}.api_key_env`]
                          }
                          aria-describedby={endpointDescribedBy("api_key_env")}
                        />
                        {fieldErrors[`endpoints.${id}.api_key_env`] && (
                          <p
                            className="field-error"
                            role="alert"
                            id={endpointErrorId("api_key_env")}
                          >
                            {fieldErrors[`endpoints.${id}.api_key_env`].message}
                          </p>
                        )}
                      </div>
                      <div
                        className="field"
                        data-changed={draft.timeout !== original.timeout}
                      >
                        <label htmlFor={endpointId("timeout")}>
                          Timeout seconds (blank = 600)
                        </label>
                        <input
                          id={endpointId("timeout")}
                          type="number"
                          min={0}
                          step="any"
                          value={
                            draft.timeout === undefined
                              ? ""
                              : fieldValue(draft.timeout)
                          }
                          onChange={(event) =>
                            setEndpoint(id, {
                              timeout: parseNumber(event.target.value)
                                ?? (event.target.value.trim()
                                  ? undefined
                                  : null),
                            })
                          }
                          aria-invalid={!!fieldErrors[`endpoints.${id}.timeout`]}
                          aria-describedby={endpointDescribedBy("timeout")}
                        />
                        {fieldErrors[`endpoints.${id}.timeout`] && (
                          <p
                            className="field-error"
                            role="alert"
                            id={endpointErrorId("timeout")}
                          >
                            {fieldErrors[`endpoints.${id}.timeout`].message}
                          </p>
                        )}
                      </div>
                    </div>
                  </fieldset>
                );
              })}
            </div>
            {!Object.keys(saved.endpoints).length && (
              <p className="notice">No endpoints are configured.</p>
            )}
          </section>
          <section className="card stack" aria-labelledby="availability-heading">
            <div className="card-header">
              <div>
                <h2 id="availability-heading">Availability</h2>
                <p className="muted">
                  Checks the saved configuration. This page's edits are
                  checked only after saving.
                </p>
              </div>
              <button
                className="button primary"
                onClick={() => void checkAvailability()}
                disabled={checking || dirty || !saved}
              >
                <RefreshCw size={16} aria-hidden="true" />
                {checking ? "Checking availability…" : "Check availability"}
              </button>
            </div>
            <p className="notice">
              A failed probe never disables configuration saving.
            </p>
            <div role="status" aria-live="polite">
              {checking
                ? "Contacting configured endpoints. Previous results, if present, are shown until this check finishes."
                : currentCheck
                  ? currentCheck.ok
                    ? "Configured models are listed and required capabilities are declared."
                    : "One or more availability or declaration checks failed."
                  : "Not checked. No endpoint has been contacted by this page."}
            </div>
            <ErrorNotice error={checkError} />
            {currentCheck && (
              <div className="stack">
                {currentCheck.endpoints.map((endpoint) => (
                  <article className="models-check-result" key={endpoint.id}>
                    <div className="card-header">
                      <h3>{endpoint.label}</h3>
                      <span className="badge">
                        {endpoint.ok ? (
                          <CheckCircle2 size={16} aria-hidden="true" />
                        ) : (
                          <XCircle size={16} aria-hidden="true" />
                        )}
                        {endpoint.ok
                          ? "Listed / declared checks passed"
                          : "Check needs attention"}
                      </span>
                    </div>
                    {endpoint.error && (
                      <p className="notice">{endpoint.error}</p>
                    )}
                    {endpoint.roles.length > 0 && (
                      <div className="table-wrap">
                        <table>
                          <thead>
                            <tr>
                              <th scope="col">Role / model ID</th>
                              <th scope="col">Model listed</th>
                              <th scope="col">Capability declared</th>
                            </tr>
                          </thead>
                          <tbody>
                            {endpoint.roles.map((role) => (
                              <tr key={role.name}>
                                <th scope="row">
                                  {role.name}
                                  <code>{role.model}</code>
                                </th>
                                <td>
                                  {role.listed
                                    ? "Yes — listed by endpoint"
                                    : "No — not listed"}
                                </td>
                                <td>
                                  {role.capabilities_declared
                                    ? "Yes — required capabilities declared"
                                    : `No — missing declarations: ${role.missing_capabilities.join(", ")}`}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <details className="advanced">
                      <summary>
                        Model IDs returned by endpoint ({endpoint.models.length})
                      </summary>
                      {endpoint.models.length ? (
                        <ul>
                          {endpoint.models.map((model) => (
                            <li key={model}>
                              <code>{model}</code>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p>No model IDs were returned.</p>
                      )}
                    </details>
                  </article>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
      <div
        className="models-savebar"
        data-dirty={dirty}
        aria-label="Configuration save controls"
      >
        {(revisionConflict || staleDraft) && (
          <div className="models-save-feedback" role="alert">
            <p>
              {revisionConflict
                ? "Configuration changed elsewhere. Reload before saving."
                : REVISION_CHANGED_MESSAGE}
            </p>
            <button
              className="button secondary"
              onClick={reloadConfiguration}
              disabled={saving}
            >
              Reload configuration
            </button>
          </div>
        )}
        <ErrorNotice error={apiError} />
        <div className="models-save-main">
          <p className="models-save-context">
            <span>{presetLabel}</span>
          </p>
          <p
            className="models-save-status"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            {saving
              ? "Saving configuration…"
              : dirty
                ? `${changed} unsaved ${changed === 1 ? "change" : "changes"}`
                : saveNotice || "No unsaved changes"}
          </p>
          <div className="models-save-buttons">
            <button
              className="button secondary"
              onClick={discardEdits}
              disabled={saving || !dirty}
            >
              Discard changes
            </button>
            <button
              className="button primary"
              onClick={() => void saveChanges()}
              disabled={saving || !dirty || invalidDraft || Object.keys(fieldErrors).length > 0 || revisionConflict || staleDraft}
            >
              <Save size={16} aria-hidden="true" />
              {saving ? "Saving…" : "Save configuration"}
            </button>
          </div>
        </div>
      </div>
      <CrashAssistantCard
        configurationRevision={(saved?.revision ?? null)}
      />
    </div>
  );
}
