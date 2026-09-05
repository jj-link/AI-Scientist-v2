import { useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  CircleHelp,
  RefreshCw,
  Server,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import {
  request,
  useApi,
  type Credential,
  type ModelsCheck,
  type ModelsView,
} from "../api";
import { ConfigSelect, ErrorNotice, PageHeading } from "../components";
import { useStudio } from "../studio";
import "./results.css";

function CredentialState({ credential }: { credential: Credential }) {
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

function EndpointUrl({ value }: { value: string }) {
  let safe: URL | null = null;
  try {
    const parsed = new URL(value);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      parsed.username = "";
      parsed.password = "";
      parsed.search = "";
      parsed.hash = "";
      safe = parsed;
    }
  } catch {
    /* A malformed endpoint remains non-clickable. */
  }
  return safe ? (
    <a href={safe.href} target="_blank" rel="noopener noreferrer">
      {safe.href}
    </a>
  ) : (
    <span>HTTP(S) endpoint URL unavailable</span>
  );
}

export default function Models() {
  const { roleConfigId } = useStudio();
  const { data, error, loading, refresh } = useApi<ModelsView>(
    roleConfigId
      ? `/api/models?config_id=${encodeURIComponent(roleConfigId)}`
      : null,
  );
  const [check, setCheck] = useState<ModelsCheck | null>(null);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [checking, setChecking] = useState(false);
  const probe = useRef<AbortController | null>(null);
  const selectedConfig = useRef(roleConfigId);
  selectedConfig.current = roleConfigId;
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
  const view = data?.config_id === roleConfigId ? data : undefined;
  const currentCheck = check?.config_id === roleConfigId ? check : null;
  async function checkAvailability() {
    if (probe.current || !view) return;
    const configId = roleConfigId;
    const controller = new AbortController();
    probe.current = controller;
    setChecking(true);
    setCheckError(null);
    try {
      const result = await request<ModelsCheck>("/api/models/check", {
        method: "POST",
        body: JSON.stringify({ config_id: configId }),
        signal: controller.signal,
      });
      if (
        !controller.signal.aborted &&
        selectedConfig.current === configId &&
        result.config_id === configId
      )
        setCheck(result);
    } catch (failure) {
      if (!controller.signal.aborted && selectedConfig.current === configId)
        setCheckError(failure);
    } finally {
      if (probe.current === controller) {
        probe.current = null;
        setChecking(false);
      }
    }
  }
  return (
    <>
      <PageHeading eyebrow="Local configuration" title="Models">
        <p>
          Inspect model assignments and declared capabilities. Studio does not
          edit configuration files, start models, or change server routes.
        </p>
      </PageHeading>
      <div className="card stack">
        <ConfigSelect id="models-role-config" label="Role configuration" />
        <div className="toolbar">
          <p className="muted">
            Changing the selection only reads its configuration. Availability
            checks happen only when requested.
          </p>
          <button
            className="button secondary"
            onClick={refresh}
            disabled={loading}
          >
            <RefreshCw size={16} aria-hidden="true" />
            Refresh configuration
          </button>
        </div>
      </div>
      <ErrorNotice error={error} />
      {error != null && (
        <div className="notice">
          <p>
            Configuration could not be refreshed. Reconnect to the local server,
            then retry; no availability result has been inferred.
          </p>
          <button className="button secondary" onClick={refresh}>
            Retry configuration
          </button>
        </div>
      )}
      {loading && !view && <p role="status">Loading model configuration…</p>}
      {view && (
        <div className="stack models-content">
          <section className="stack" aria-labelledby="model-roles-heading">
            <div>
              <h2 id="model-roles-heading">Role assignments</h2>
              <p className="muted">
                Token budgets and timeouts come from this configuration. A
                declared capability is not a verified model request.
              </p>
            </div>
            {view.roles.length ? (
              <div className="table-wrap">
                <table className="models-role-table">
                  <thead>
                    <tr>
                      <th scope="col">Role</th>
                      <th scope="col">Endpoint and model ID</th>
                      <th scope="col">Token budget</th>
                      <th scope="col">Timeout</th>
                      <th scope="col">Required capabilities</th>
                      <th scope="col">Credential environment</th>
                    </tr>
                  </thead>
                  <tbody>
                    {view.roles.map((role) => (
                      <tr key={role.name}>
                        <th scope="row">{role.name}</th>
                        <td>
                          <span>
                            {view.endpoints.find(
                              (endpoint) => endpoint.id === role.endpoint,
                            )?.label ||
                              role.endpoint ||
                              "Not configured"}
                          </span>
                          <code>{role.model || "Model not configured"}</code>
                        </td>
                        <td>
                          {role.effective_max_tokens !== null ? (
                            <>
                              {role.effective_max_tokens.toLocaleString()}{" "}
                              effective
                              {role.max_tokens !== null && (
                                <span className="metadata">
                                  {role.max_tokens.toLocaleString()} configured
                                </span>
                              )}
                            </>
                          ) : role.max_tokens !== null ? (
                            role.max_tokens.toLocaleString()
                          ) : (
                            "Not specified"
                          )}
                        </td>
                        <td>{role.timeout} seconds</td>
                        <td>
                          {role.requires.length
                            ? role.requires.join(", ")
                            : "None declared"}
                        </td>
                        <td>
                          <CredentialState credential={role.credential} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="card empty-state">
                <h3>No role assignments found</h3>
                <p>
                  Select another discovered role configuration or inspect the
                  configuration file outside Studio.
                </p>
              </div>
            )}
          </section>
          <section className="stack" aria-labelledby="model-endpoints-heading">
            <div>
              <h2 id="model-endpoints-heading">Configured endpoints</h2>
              <p className="muted">
                Only environment-variable names and presence are shown.
                Credential values are never displayed.
              </p>
            </div>
            <div className="card-grid">
              {view.endpoints.map((endpoint) => (
                <article
                  className="card stack models-endpoint"
                  key={endpoint.id}
                >
                  <div className="card-header">
                    <h3>
                      <Server size={20} aria-hidden="true" />
                      {endpoint.label}
                    </h3>
                    <span className="badge">
                      <ShieldCheck size={14} aria-hidden="true" />
                      Configuration only
                    </span>
                  </div>
                  <dl className="results-record">
                    <dt>Endpoint URL</dt>
                    <dd>
                      <EndpointUrl value={endpoint.url} />
                    </dd>
                    <dt>Timeout</dt>
                    <dd>{endpoint.timeout} seconds</dd>
                    <dt>Capability declared</dt>
                    <dd>
                      {endpoint.capabilities.length
                        ? endpoint.capabilities.join(", ")
                        : "None declared"}
                    </dd>
                    <dt>Credential environment</dt>
                    <dd>
                      <CredentialState credential={endpoint.credential} />
                    </dd>
                  </dl>
                </article>
              ))}
            </div>
            {!view.endpoints.length && (
              <p className="notice">No endpoints are configured.</p>
            )}
          </section>
          <section
            className="card stack"
            aria-labelledby="availability-heading"
          >
            <div className="card-header">
              <div>
                <h2 id="availability-heading">Availability</h2>
                <p className="muted">
                  Explicitly request each endpoint’s model list. This does not
                  send a text or image generation request.
                </p>
              </div>
              <button
                className="button primary"
                onClick={() => void checkAvailability()}
                disabled={checking || !view.endpoints.length}
              >
                <RefreshCw size={16} aria-hidden="true" />
                {checking ? "Checking availability…" : "Check availability"}
              </button>
            </div>
            <p className="notice">
              <strong>Model listed</strong> means the endpoint lists that model
              ID. <strong>Capability declared</strong> means the configuration
              declares the role’s requirements. Neither proves that a text or
              image request works.
            </p>
            <ErrorNotice error={checkError} />
            {checkError != null && (
              <p>
                Check availability again to retry. Any previous results below
                retain their original check time.
              </p>
            )}
            <div role="status" aria-live="polite">
              {checking
                ? "Contacting configured endpoints. Previous results, if present, are shown until this check finishes."
                : currentCheck
                  ? `Check finished: ${currentCheck.ok ? "configured models are listed and required capabilities are declared" : "one or more availability or declaration checks failed"}.`
                  : "Not checked. No endpoint has been contacted by this page."}
            </div>
            {currentCheck && (
              <p className="metadata">
                Last requested:{" "}
                {new Date(currentCheck.checked_at).toLocaleString()}
              </p>
            )}
            <div className="stack">
              {view.endpoints.map((endpoint) => {
                const result = currentCheck?.endpoints.find(
                  (item) => item.id === endpoint.id,
                );
                return (
                  <article className="models-check-result" key={endpoint.id}>
                    <div className="card-header">
                      <h3>{endpoint.label}</h3>
                      <span className="badge">
                        {result ? (
                          result.ok ? (
                            <CheckCircle2 size={16} aria-hidden="true" />
                          ) : (
                            <XCircle size={16} aria-hidden="true" />
                          )
                        ) : (
                          <CircleHelp size={16} aria-hidden="true" />
                        )}
                        {result
                          ? result.ok
                            ? "Listed / declared checks passed"
                            : "Check needs attention"
                          : "Not checked"}
                      </span>
                    </div>
                    {result && (
                      <>
                        <p className="metadata">
                          Checked:{" "}
                          {new Date(result.checked_at).toLocaleString()}
                        </p>
                        {result.error && (
                          <p className="notice">{result.error}</p>
                        )}
                        {result.roles.length > 0 && (
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
                                {result.roles.map((role) => (
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
                            Model IDs returned by endpoint (
                            {result.models.length})
                          </summary>
                          {result.models.length ? (
                            <ul>
                              {result.models.map((model) => (
                                <li key={model}>
                                  <code>{model}</code>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p>No model IDs were returned.</p>
                          )}
                        </details>
                      </>
                    )}
                  </article>
                );
              })}
            </div>
          </section>
        </div>
      )}
    </>
  );
}
