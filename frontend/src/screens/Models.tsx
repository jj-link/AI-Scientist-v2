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
  mutate,
  request,
  useApi,
  type AssistantSettings,
  type Credential,
  type ModelConfigEditor,
  type ModelConfigEditorEndpoint,
  type ModelConfigEditorRole,
  type ModelEndpointPatch,
  type ModelRolePatch,
  type ModelsCheck,
  type ModelsView,
} from "../api";
import { ErrorNotice, PageHeading } from "../components";
import CodexProviderCard from "./CodexProviderCard";
import RoleAssignmentsEditor from "../RoleAssignmentsEditor";
import RoleProfiles from "../RoleProfiles";
import { applyRoleAssignments, validRoleAssignments } from "../roleAssignments";



const DRAFT_STORAGE_KEY = "model-drafts";
const CBORG_BASE_URL = "https://api.cborg.lbl.gov/v1";
const CBORG_API_KEY_ENV = "CBORG_API_KEY";

type EndpointDraft = ModelEndpointPatch & {
  provider: ModelConfigEditorEndpoint["provider"];
};

interface StoredDraft {
  baseRevision: string;
  roles: Record<string, ModelConfigEditorRole>;
  endpoints: Record<string, EndpointDraft>;
}

function readStoredDraft(): StoredDraft | null {
  try {
    const raw = sessionStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.baseRevision === "string") return parsed;
  } catch {
    /* A stale draft must never block editing. */
  }
  return null;
}

function writeStoredDraft(draft: StoredDraft) {
  try {
    sessionStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(draft));
  } catch {
    /* Draft persistence is best effort. */
  }
}

function clearStoredDraft() {
  try {
    sessionStorage.removeItem(DRAFT_STORAGE_KEY);
  } catch {
    /* Draft persistence is best effort. */
  }
}

function countChangedFields(
  saved: ModelConfigEditor,
  roles: Record<string, ModelConfigEditorRole>,
  endpoints: Record<string, EndpointDraft>,
): number {
  let count = 0;
  for (const [name, draft] of Object.entries(roles)) {
    const original = saved.roles[name];
    if (!original) {
      count++;
      continue;
    }
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
    if (!original) {
      if (id.trim()) count++;
      continue;
    }
    for (const field of ["provider", "base_url", "api_key_env", "timeout"] as const) {
      if (draft[field] !== original[field]) count++;
    }
  }
  return count;
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
  const settings = useApi<AssistantSettings>(
    "/api/crash-assistant/settings",
  );
  const [role, setRole] = useState("");
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [savedView, setSavedView] = useState<AssistantSettings | null>(null);
  const view = useApi<ModelsView>("/api/models");
  const currentView = view.data;
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
    if (!touched && loaded) {
      setRole(loaded.role ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded?.role]);
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
          ? { enabled: true, role }
          : { enabled: false },
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
  const canEnable = Boolean(role && textRoles.some((item) => item.name === role));
  return (
    <section className="card stack models-utility-card" aria-labelledby="assistant-heading">
      <div className="models-utility-header">
        <h2 id="assistant-heading">Crash assistant</h2>
      </div>
      <p className="muted">Help diagnose failed jobs. Excerpts go to the selected model; public reports require separate approval.</p>
      <div className="models-assistant-controls">
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
              ? "Disabling stays available even if the saved assignment is broken."
              : "Enable requires a role whose endpoint declares text capability. Use this page's configuration editor to assign one, then click Save assistant settings."}
          </small>
        )}
      </div>
      {loaded ? (
        <>
          <div className="field">
            <label htmlFor="assistant-role">Assistant role</label>
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
                  {candidate.name}
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
      </div>
      <ErrorNotice error={saveError} />
      <details className="models-utility-details">
        <summary>Details</summary>
          <p className="muted">
            Saved separately from the configuration above. When enabled, a
            failed job sends one bounded, credential-redacted Technical
            details excerpt to the selected endpoint for newly started jobs
            only; redaction may not remove private research content. GitHub
            reports are public on <code>jj-link/AI-Scientist-v2</code> and
            require separate exact-content approval.
          </p>
          <p className="muted">After editing a role above, click Save assistant settings to capture its new assignment. Existing job snapshots do not change.</p>
      {shown && (
        <dl className="results-record metadata">
          <dt>Saved assignment</dt>
          <dd>
            {shown.enabled
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
      </details>
    </section>
  );
}

const REVISION_CHANGED_MESSAGE =
  "The configuration changed after your last edit; reload before saving.";

export default function Models() {
  const editor = useApi<ModelConfigEditor>("/api/models/editor");
  const display = useApi<ModelsView>("/api/models");
  const [saved, setSaved] = useState<ModelConfigEditor | null>(null);
  const [roleDrafts, setRoleDrafts] = useState<
    Record<string, ModelConfigEditorRole>
  >({});
  const [endpointDrafts, setEndpointDrafts] = useState<
    Record<string, EndpointDraft>
  >({});
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [apiError, setApiError] = useState<unknown>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [staleDraft, setStaleDraft] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
  const [rolesOpen, setRolesOpen] = useState(false);
  const [discoveryGeneration, setDiscoveryGeneration] = useState(0);
  const [check, setCheck] = useState<ModelsCheck | null>(null);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const clearAvailability = useCallback(() => {
    setDiscoveryGeneration((current) => current + 1);
    setCheck(null);
  }, []);
  const probe = useRef<AbortController | null>(null);
  const baseRevision = useRef<string | null>(null);
  const changed = saved
    ? countChangedFields(saved, roleDrafts, endpointDrafts)
    : 0;
  const dirty = changed > 0;
  const positive = (value: unknown) => value === null ||
    (typeof value === "number" && Number.isFinite(value) && value > 0);
  const unknownRoles = saved
    ? Object.keys(roleDrafts).filter((name) => !Object.hasOwn(saved.roles, name))
    : [];
  const profileEndpointProblems = saved
    ? [...new Set(Object.values(roleDrafts).flatMap((role) => {
      if (!role.endpoint) return [];
      const original = saved.endpoints[role.endpoint];
      const draft = endpointDrafts[role.endpoint];
      if (!original || !draft) return [`Endpoint "${role.endpoint}" is unavailable in the saved configuration.`];
      if (draft.provider !== original.provider || draft.base_url !== original.base_url ||
        draft.api_key_env !== original.api_key_env || draft.timeout !== original.timeout) {
        return [`Endpoint "${role.endpoint}" has unsaved server edits. Save or discard those edits before saving a profile.`];
      }
      return [];
    }))]
    : [];
  const invalidDraft = unknownRoles.length > 0 ||
    !validRoleAssignments(roleDrafts, endpointDrafts) ||
    Object.values(endpointDrafts).some((endpoint) =>
      (endpoint.provider !== "openai-codex" &&
        !(endpoint.base_url ?? (endpoint.provider === "cborg" ? CBORG_BASE_URL : "")).trim()) ||
      !positive(endpoint.timeout));
  const savingRequest = useRef(false);
  const editorSurface = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const loaded = editor.data;
    if (!loaded) return;
    if (saved && loaded.revision === saved.revision) return;
    if (dirty) {
      setStaleDraft(true);
      return;
    }
    const stored = readStoredDraft();
    baseRevision.current = stored?.baseRevision ?? loaded.revision;
    setStaleDraft(Boolean(stored && stored.baseRevision !== loaded.revision));
    setSaved(loaded);
    setRoleDrafts(stored?.roles ?? { ...loaded.roles });
    setEndpointDrafts(stored?.endpoints ?? { ...loaded.endpoints });
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
  }, [editor.data]);
  useEffect(() => {
    probe.current?.abort();
    probe.current = null;
    return () => {
      probe.current?.abort();
      probe.current = null;
    };
  }, []);
  useEffect(() => {
    if (!saved) return;
    if (!dirty) {
      clearStoredDraft();
      return;
    }
    writeStoredDraft({
      baseRevision: baseRevision.current ?? saved.revision,
      roles: roleDrafts,
      endpoints: endpointDrafts,
    });
  }, [roleDrafts, endpointDrafts, dirty, saved]);
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
    setCheck(null);
    setDiscoveryGeneration((current) => current + 1);
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
      if (!original) {
        if (id.trim()) endpointPatch[id] = {
          provider: draft.provider,
          base_url: draft.base_url,
          api_key_env: draft.api_key_env,
          timeout: draft.timeout,
        };
        continue;
      }
      const patch: ModelEndpointPatch = {};
      if (draft.provider !== original.provider) (patch.provider = draft.provider);
      if (draft.base_url !== original.base_url) (patch.base_url = draft.base_url);
      if (draft.api_key_env !== original.api_key_env) (patch.api_key_env = draft.api_key_env);
      if (draft.timeout !== original.timeout) (patch.timeout = draft.timeout);
      if (Object.keys(patch).length) endpointPatch[id] = patch;
    }
    const payload = {
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
      baseRevision.current = result.revision;
      setSaved(result);
      setRoleDrafts({ ...result.roles });
      setEndpointDrafts({ ...result.endpoints });
      setCheck(null);
      setSaveNotice("Configuration saved");
      clearStoredDraft();
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
          setRolesOpen(true);
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
    if (dirty) clearStoredDraft();
    if (saved) {
      setRoleDrafts({ ...saved.roles });
      setEndpointDrafts({ ...saved.endpoints });
    }
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
    setCheck(null);
    clearStoredDraft();
  }
  function reloadConfiguration() {
    if (saved && dirty && !window.confirm("Discard unsaved edits?")) return;
    if (dirty) clearStoredDraft();
    resetEditor();
    editor.refresh();
    display.refresh();
  }
  async function checkAvailability() {
    if (probe.current || !saved || dirty) return;
    const controller = new AbortController();
    probe.current = controller;
    setChecking(true);
    setCheckError(null);
    try {
      const result = await request<ModelsCheck>("/api/models/check", {
        method: "POST",
        signal: controller.signal,
      });
    } catch (failure) {
      if (!controller.signal.aborted) setCheckError(failure);
    } finally {
      if (probe.current === controller) {
        probe.current = null;
        setChecking(false);
      }
    }
  }
  const currentCheck = check;
  const loadingEditor = editor.loading && !saved;
  return (
    <div className="models-page stack" ref={editorSurface}>
      <PageHeading eyebrow="Local configuration" title="Models">
        <p>
          Choose the model for each research role. Changes save for future
          Studio jobs; running jobs keep their snapshots. Saving does not
          start or restart models.
        </p>
      </PageHeading>
      <div className="card models-preset">
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
              <button
                type="button"
                className="button secondary models-roles-toggle"
                aria-expanded={rolesOpen}
                aria-controls="models-role-list"
                onClick={() => setRolesOpen((current) => !current)}
              >
                {rolesOpen ? "Hide role assignments" : "Show role assignments"}
                <ChevronDown size={16} aria-hidden="true" />
              </button>
              <p className="muted">
                Choose which AI model handles each task. One model can serve
                several roles. Tasks can reuse a model with different
                contexts; a role selects a model and settings, not a separate
                agent or a shared conversation. Open Advanced for token,
                temperature, timeout, and credential overrides.
              </p>
            </div>
            <RoleProfiles
              roles={roleDrafts}
              showSave={rolesOpen}
              disabled={saving}
              saveDisabled={unknownRoles.length > 0 || profileEndpointProblems.length > 0 || revisionConflict || staleDraft}
              onLoad={(assignments) => {
                setRoleDrafts(applyRoleAssignments(saved.roles, assignments));
                setFieldErrors({});
                setApiError(null);
                setSaveNotice("");
                setCheck(null);
                setRolesOpen(true);
              }}
            />
            {(unknownRoles.length > 0 || profileEndpointProblems.length > 0) && (
              <div className="notice" role="alert">
                {unknownRoles.length > 0 && <p>Unsupported profile roles: {unknownRoles.join(", ")}. Your draft is preserved. Load a compatible profile or discard changes before saving defaults or a profile.</p>}
                {profileEndpointProblems.map((problem) => <p key={problem}>{problem}</p>)}
              </div>
            )}
            <div className="models-role-list" id="models-role-list" hidden={!rolesOpen}>
              <RoleAssignmentsEditor
                key={discoveryGeneration}
                saved={saved}
                roles={roleDrafts}
                endpoints={endpointDrafts}
                onChange={setRole}
                disabled={saving}
                errors={fieldErrors}
                display={display.data}
              />
            </div>
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
          </section>
        </div>
      )}
      <CrashAssistantCard
        configurationRevision={(saved?.revision ?? null)}
      />
    </div>
  );
}
