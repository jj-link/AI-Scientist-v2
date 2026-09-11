import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import {
  discoverEndpointModels,
  type Credential,
  type ModelConfigEditor,
  type ModelConfigEditorEndpoint,
  type ModelConfigEditorRole,
  type ModelEndpointPatch,
  type ModelsView,
} from "./api";
import { ROLE_HELP, CUSTOM_ROLE_HELP } from "./components";

type EndpointDraft = ModelEndpointPatch & {
  provider: ModelConfigEditorEndpoint["provider"];
};

type FieldErrors = Record<string, { message: string; code?: string }>;

interface ModelDiscovery {
  status: "idle" | "loading" | "ok" | "error";
  models: string[];
  error: string | null;
}

const CUSTOM_MODEL_HINT = "Enter the model ID exactly as served by the endpoint.";

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

export default function RoleAssignmentsEditor({
  saved,
  roles: roleDrafts,
  endpoints,
  onChange: setRole,
  disabled: saving = false,
  errors = {},
  display: currentDisplay,
}: {
  saved: ModelConfigEditor;
  roles: Record<string, ModelConfigEditorRole>;
  endpoints?: Record<string, EndpointDraft>;
  onChange: (name: string, patch: Partial<ModelConfigEditorRole>) => void;
  disabled?: boolean;
  errors?: FieldErrors;
  display?: ModelsView;
}) {
  const endpointDrafts = endpoints ?? saved.endpoints;
  const idPrefix = useId();
  const [expandedRoles, setExpandedRoles] = useState<string[]>([]);
  const [discoveries, setDiscoveries] = useState<Record<string, ModelDiscovery>>({});
  const discoveryRequests = useRef(new Map<string, AbortController>());
  const fieldErrors: FieldErrors = { ...errors };
  for (const [name, role] of Object.entries(roleDrafts)) {
    if (!Object.hasOwn(saved.roles, name)) {
      fieldErrors[`roles.${name}`] = { message: `Role "${name}" is no longer configured. This draft is preserved; load a compatible profile or discard changes before saving.` };
    }
    if (role.endpoint && !Object.hasOwn(endpointDrafts, role.endpoint)) {
      fieldErrors[`roles.${name}.endpoint`] = { message: `Endpoint "${role.endpoint}" is unavailable. Select a configured endpoint.` };
    }
  }
  useEffect(() => {
    setDiscoveries({});
    return () => {
      for (const controller of discoveryRequests.current.values()) controller.abort();
      discoveryRequests.current.clear();
    };
  }, [saved.revision]);
  useEffect(() => {
    const invalidRoles = Object.keys(roleDrafts).filter((name) =>
      Object.keys(errors).some((field) => field === `roles.${name}` || field.startsWith(`roles.${name}.`)));
    if (invalidRoles.length) setExpandedRoles((current) => [...new Set([...current, ...invalidRoles])]);
  }, [errors]);
  async function detectRoleModels(roleName: string) {
    const endpoint = roleDrafts[roleName]?.endpoint;
    if (!endpoint || !saved || discoveryRequests.current.has(endpoint)) return;
    const connection = endpointDrafts[endpoint];
    const original = saved.endpoints[endpoint];
    if (!connection || !original ||
        connection.provider !== original.provider ||
        connection.base_url !== original.base_url ||
        connection.api_key_env !== original.api_key_env ||
        connection.timeout !== original.timeout) return;
    const controller = new AbortController();
    discoveryRequests.current.set(endpoint, controller);
    setDiscoveries((current) => ({
      ...current,
      [endpoint]: { status: "loading", models: [], error: null },
    }));
    try {
      const result = await discoverEndpointModels(endpoint, controller.signal);
      if (controller.signal.aborted || result.endpoint !== endpoint) return;
      setDiscoveries((current) => ({
        ...current,
        [endpoint]: {
          status: result.ok ? "ok" : "error",
          models: result.ok ? [...new Set(result.models)] : [],
          error: result.error,
        },
      }));
    } catch {
      if (controller.signal.aborted) return;
      setDiscoveries((current) => ({
        ...current,
        [endpoint]: { status: "error", models: [], error: "Model listing failed. Retry or enter a model ID in Advanced." },
      }));
    } finally {
      if (discoveryRequests.current.get(endpoint) === controller)
        discoveryRequests.current.delete(endpoint);
    }
  }
  return (
    <div className="models-role-list">
              {Object.entries(roleDrafts).map(([name, draft]) => {
                const original = saved.roles[name] ?? draft;
                const codex = endpointDrafts[draft.endpoint ?? ""]?.provider === "openai-codex";
                const help = Object.hasOwn(ROLE_HELP, name) ? ROLE_HELP[name] : CUSTOM_ROLE_HELP;
                const displayRole = currentDisplay?.roles.find(
                  (item) => item.name === name,
                );
                const roleError = fieldErrors[`roles.${name}`]?.message;
                const roleId = (suffix: string) => `${idPrefix}-role-${name}-${suffix}`;
                const errorId = (suffix: string) => `${roleId(suffix)}-error`;
                const discovery =
                  saved.endpoints[draft.endpoint ?? ""]
                    ? discoveries[draft.endpoint ?? ""]
                    : undefined;
                const connection = endpointDrafts[draft.endpoint ?? ""];
                const originalConnection = saved.endpoints[draft.endpoint ?? ""];
                const connectionDirty = Boolean(connection && (!originalConnection ||
                  connection.provider !== originalConnection.provider ||
                   connection.base_url !== originalConnection.base_url ||
                   connection.api_key_env !== originalConnection.api_key_env ||
                   connection.timeout !== originalConnection.timeout));
                const listedModels = connectionDirty ? [] : discovery?.models ?? [];
                const describedBy = (field: string) =>
                  [
                    (["max_tokens", "temperature", "timeout", "api_key_env"].includes(field)
                      ? roleId(`${field}-hint`)
                      : null),
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
                    aria-describedby={[
                      roleId("description"),
                      help.usageNote ? roleId("usage") : null,
                    ].filter(Boolean).join(" ")}
                  >
                    <legend className="sr-only">{help.title} ({name})</legend>
                    <div className="models-role-heading">
                      <div className="models-role-name">
                        <h3>{help.title}</h3>
                        <code className="muted metadata">role/{name}</code>
                      </div>
                      {original.requires.length > 0 && (
                        <span className="muted metadata">
                          Requires {original.requires.join(", ")}
                        </span>
                      )}
                    </div>
                    <p className="models-role-description" id={roleId("description")}>
                      {help.description}
                    </p>
                    {help.usageNote && (
                      <p className="models-role-note" id={roleId("usage")}>
                        {help.usageNote}
                      </p>
                    )}
                    <div className="models-role-primary">
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
                              const codex = endpointDrafts[next ?? ""]?.provider === "openai-codex";
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
                          {draft.endpoint && !Object.hasOwn(endpointDrafts, draft.endpoint) && (
                            <option value={draft.endpoint}>{draft.endpoint} (unavailable)</option>
                          )}
                          {Object.keys(endpointDrafts).map((id) => (
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
                              !originalConnection ||
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
                                    : draft.endpoint && !originalConnection
                                      ? "This endpoint is unavailable in the saved configuration."
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
                        type="button"
                        className="button secondary models-advanced-toggle"
                        aria-expanded={advancedOpen}
                        aria-controls={roleId("advanced")}
                        aria-label={`Advanced settings for ${help.title} (${name})`}
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
                            {endpointDrafts[draft.endpoint ?? ""]
                              ? (endpointDrafts[
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
                          {codex ? "Uses the configured Codex sign-in." : "Blank inherits the endpoint credential. Environment-variable names only."}
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
  );
}
