import { useEffect, useRef, useState } from "react";
import { Save } from "lucide-react";
import {
  ApiError,
  mutate,
  useApi,
  type ModelConfigEditor,
  type ModelConfigEditorEndpoint,
  type ModelEndpointPatch,
  type ModelsView,
} from "../api";
import { ErrorNotice, PageHeading } from "../components";

type FieldErrors = Record<string, { message: string; code: string }>;

const DRAFT_STORAGE_KEY = "endpoint-drafts";
const CBORG_BASE_URL = "https://api.cborg.lbl.gov/v1";
const CBORG_API_KEY_ENV = "CBORG_API_KEY";

type EndpointDraft = ModelEndpointPatch & {
  provider: ModelConfigEditorEndpoint["provider"];
};

interface StoredDraft {
  baseRevision: string;
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

function fieldValue(value: number | string | null | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

function parseNumber(value: string): number | null | undefined {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function countChangedEndpoints(
  saved: ModelConfigEditor,
  endpoints: Record<string, EndpointDraft>,
): number {
  let count = 0;
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

export default function Settings() {
  const editor = useApi<ModelConfigEditor>("/api/models/editor");
  const display = useApi<ModelsView>("/api/models");
  const [saved, setSaved] = useState<ModelConfigEditor | null>(null);
  const [endpointDrafts, setEndpointDrafts] = useState<
    Record<string, EndpointDraft>
  >({});
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [apiError, setApiError] = useState<unknown>(null);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [staleDraft, setStaleDraft] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
  const baseRevision = useRef<string | null>(null);
  const changed = saved
    ? countChangedEndpoints(saved, endpointDrafts)
    : 0;
  const dirty = changed > 0;
  const invalidDraft = Object.values(endpointDrafts).some((endpoint) =>
    (endpoint.provider !== "openai-codex" &&
      !(endpoint.base_url ?? (endpoint.provider === "cborg" ? CBORG_BASE_URL : "")).trim()) ||
    !(endpoint.timeout === null ||
      (typeof endpoint.timeout === "number" && Number.isFinite(endpoint.timeout) && endpoint.timeout > 0)));
  const currentDisplay = display.data;
  const loadingEditor = editor.loading && !saved;

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
    setEndpointDrafts(stored?.endpoints ?? { ...loaded.endpoints });
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
  }, [editor.data]);
  useEffect(() => {
    if (!saved) return;
    if (!dirty) {
      clearStoredDraft();
      return;
    }
    writeStoredDraft({
      baseRevision: baseRevision.current ?? saved.revision,
      endpoints: endpointDrafts,
    });
  }, [endpointDrafts, dirty, saved]);

  function setEndpoint(id: string, patch: Partial<ModelEndpointPatch>) {
    setFieldErrors({});
    setEndpointDrafts((current) => ({
      ...current,
      [id]: { ...current[id], ...patch },
    }));
  }
  function addServer() {
    const name = window.prompt("New server name (letters, digits, dash, dot, underscore):", "");
    const trimmed = name?.trim() ?? "";
    if (!trimmed) return;
    if (!/^[\w.-]+$/.test(trimmed)) {
      window.alert("Use letters, digits, dash, dot, or underscore in the server name.");
      return;
    }
    if (saved?.endpoints[trimmed] || endpointDrafts[trimmed]) {
      window.alert(`A server named ${trimmed} already exists.`);
      return;
    }
    setFieldErrors({});
    setEndpointDrafts((current) => ({
      ...current,
      [trimmed]: { provider: "openai", base_url: "", api_key_env: "", timeout: null },
    }));
  }
  function setProvider(id: string, provider: EndpointDraft["provider"]) {
    if (endpointDrafts[id]?.provider === provider) return;
    setEndpoint(id, {
      provider,
      ...(provider === "cborg"
        ? { base_url: CBORG_BASE_URL, api_key_env: CBORG_API_KEY_ENV }
        : provider === "openai-codex"
          ? { base_url: null, api_key_env: null }
          : {}),
    });
  }
  function discardEdits() {
    if (saved && dirty && !window.confirm("Discard unsaved edits?")) return;
    if (dirty) clearStoredDraft();
    if (saved) setEndpointDrafts({ ...saved.endpoints });
    setFieldErrors({});
    setApiError(null);
    setSaveNotice("");
    setStaleDraft(false);
    clearStoredDraft();
  }
  async function saveChanges() {
    if (!saved || !dirty || saving || invalidDraft || revisionConflict || staleDraft) return;
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
    setSaving(true);
    setApiError(null);
    try {
      const result = await mutate<ModelConfigEditor>(
        "/api/models/editor",
        {
          expected_revision: saved.revision,
          roles: {},
          endpoints: endpointPatch,
        },
        "PATCH",
      );
      baseRevision.current = result.revision;
      setSaved(result);
      setEndpointDrafts({ ...result.endpoints });
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
        }
        setApiError(error);
      } else {
        setApiError(error);
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="models-page stack">
      <PageHeading eyebrow="Local configuration" title="Settings">
        <p>
          Endpoint connections for research model servers. Role assignments
          live on the Models page.
        </p>
      </PageHeading>
      {editor.error != null && !revisionConflict && (
        <ErrorNotice error={editor.error} />
      )}
      {loadingEditor && <p role="status">Loading model configuration…</p>}
      {saved && (
        <>
          <section className="stack" aria-labelledby="model-endpoints-heading">
            <div className="toolbar">
              <h2 id="model-endpoints-heading">Endpoint connections</h2>
              <button type="button" className="button" onClick={addServer}>
                Add server
              </button>
            </div>
            <p className="muted">
              Only environment-variable names are editable; credential
              values are never displayed or stored here.
            </p>
            <div className="editor-grid">
              {Object.entries(endpointDrafts).map(([id, draft]) => {
                const original = saved.endpoints[id];
                if (!original) {
                  const endpointId = (field: string) => `endpoint-${id}-${field}`;
                  return (
                    <fieldset key={id} className="card stack editor-card" disabled={saving}>
                      <legend className="editor-entry">{id}</legend>
                      <p className="muted metadata">New server — fill the URL, then Save configuration.</p>
                      <div className="field">
                        <label htmlFor={endpointId("provider")}>Provider</label>
                        <select id={endpointId("provider")} value={draft.provider}
                          onChange={(event) => setProvider(id, event.target.value as EndpointDraft["provider"])}>
                          <option value="openai">OpenAI-compatible API</option>
                          <option value="openai-codex">OpenAI Codex</option>
                          <option value="cborg">CBORG</option>
                        </select>
                      </div>
                      {draft.provider !== "openai-codex" && (
                        <div className="field">
                          <label htmlFor={endpointId("base_url")}>Endpoint URL</label>
                          <input id={endpointId("base_url")} type="url" value={fieldValue(draft.base_url ?? "")}
                            onChange={(event) => setEndpoint(id, { base_url: event.target.value })} />
                        </div>
                      )}
                      <div className="field">
                        <label htmlFor={endpointId("api_key_env")}>Credential environment name</label>
                        <input id={endpointId("api_key_env")} value={fieldValue(draft.api_key_env ?? "")}
                          onChange={(event) => setEndpoint(id, { api_key_env: event.target.value })} />
                      </div>
                    </fieldset>
                  );
                }
                const displayEndpoint = currentDisplay?.endpoints.find(
                  (item) => item.id === id,
                );
                const capabilities = original.provides;
                const codex = draft.provider === "openai-codex";
                const cborg = draft.provider === "cborg";
                const endpointId = (field: string) => `endpoint-${id}-${field}`;
                const endpointErrorId = (field: string) =>
                  `${endpointId(field)}-error`;
                const endpointDescribedBy = (field: string) =>
                  [
                    ["provider", "base_url", "api_key_env"].includes(field)
                      ? endpointId(`${field}-hint`)
                      : null,
                    fieldErrors[`endpoints.${id}.${field}`]
                      ? endpointErrorId(field)
                      : null,
                  ].filter(Boolean).join(" ") || undefined;
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
                    <div className="field" data-changed={draft.provider !== original.provider}>
                      <label htmlFor={endpointId("provider")}>Provider</label>
                      <select
                        id={endpointId("provider")}
                        value={draft.provider}
                        onChange={(event) => setProvider(id, event.target.value as EndpointDraft["provider"])}
                        aria-invalid={!!fieldErrors[`endpoints.${id}.provider`]}
                        aria-describedby={endpointDescribedBy("provider")}
                      >
                        <option value="openai">OpenAI-compatible API</option>
                        <option value="cborg">CBORG</option>
                        <option value="openai-codex">OpenAI Codex</option>
                      </select>
                      <small className="muted" id={endpointId("provider-hint")}>
                        {cborg
                          ? "CBORG uses the existing OpenAI-compatible API. Selecting it supplies its address and credential environment-variable name; custom overrides remain editable."
                          : codex
                            ? "Selecting Codex clears the endpoint address and credential name, plus token, temperature, and credential overrides for its roles, in this draft."
                            : "Use any compatible HTTP(S) API address. An endpoint URL is required."}
                        {" "}Changes apply only after Save configuration.
                      </small>
                      {fieldErrors[`endpoints.${id}.provider`] && (
                        <p className="field-error" role="alert" id={endpointErrorId("provider")}>
                          {fieldErrors[`endpoints.${id}.provider`].message}
                        </p>
                      )}
                    </div>
                    {codex && <p className="muted">Managed Codex server and OAuth credentials. Sign in above; no API key or custom server address is used.</p>}
                    <div
                      className="field"
                      data-changed={draft.base_url !== original.base_url}
                    >
                      <label htmlFor={endpointId("base_url")}>Endpoint URL</label>
                      <input
                        id={endpointId("base_url")}
                        type="text"
                        value={codex ? "Managed by Codex" : draft.base_url ?? ""}
                        placeholder={cborg ? CBORG_BASE_URL : "https://api.example.org/v1"}
                        disabled={codex}
                        onChange={(event) =>
                          setEndpoint(id, {
                            base_url: event.target.value || null,
                          })
                        }
                        aria-invalid={!!fieldErrors[`endpoints.${id}.base_url`]}
                        aria-describedby={endpointDescribedBy("base_url")}
                      />
                      <small className="muted" id={endpointId("base_url-hint")}>
                        {cborg ? `Blank uses ${CBORG_BASE_URL}.` : codex ? "Address managed by Codex." : "Required for an OpenAI-compatible API."}
                      </small>
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
                          placeholder={codex ? "ChatGPT sign-in" : cborg ? CBORG_API_KEY_ENV : undefined}
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
                        <small className="muted" id={endpointId("api_key_env-hint")}>
                          {cborg ? `Blank uses ${CBORG_API_KEY_ENV}.` : codex ? "Credentials managed by Codex." : "Blank uses the keyless endpoint convention."}
                          {" "}Environment-variable names only, never secret values.
                        </small>
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
        </>
      )}
      <div
        className="models-savebar"
        role="status"
        aria-live="polite"
      >
        {(revisionConflict || staleDraft) && (
          <div className="models-save-feedback" role="alert">
            <p>Configuration changed elsewhere. Reload the page before saving.</p>
          </div>
        )}
        {saveNotice && <p>{saveNotice}</p>}
        {!saveNotice && !dirty && !revisionConflict && !staleDraft && (
          <p>No unsaved changes</p>
        )}
        <div className="toolbar">
          <button
            type="button"
            className="button secondary"
            onClick={discardEdits}
            disabled={saving || !dirty}
          >
            Discard changes
          </button>
          <button
            type="button"
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
  );
}
