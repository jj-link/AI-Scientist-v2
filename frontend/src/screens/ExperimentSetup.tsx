import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Play, ShieldAlert } from "lucide-react";
import { ApiError, isActive, mutate, useApi, type IdeaRecord } from "../api";
import {
  ConfigSelect,
  ErrorNotice,
  JsonText,
  PageHeading,
} from "../components";
import { useStudio } from "../studio";
import "./ideas.css";

type LaunchRequest = {
  request_id: string;
  idea_id: string;
  idea_revision: number;
  role_config_id: string;
  bfts_config_id: string;
  execution_acknowledged: boolean;
};
function savedRequest(ideaId: string): LaunchRequest | null {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(`scientist-studio-launch-${ideaId}`) || "null",
    ) as LaunchRequest | null;
    return value?.idea_id === ideaId ? value : null;
  } catch {
    return null;
  }
}

export default function ExperimentSetup() {
  const { ideaId } = useParams();
  const idea = useApi<IdeaRecord>(
    ideaId ? `/api/ideas/${encodeURIComponent(ideaId)}` : null,
  );
  return (
    <div className="stack">
      <Link
        className="row"
        to={ideaId ? `/ideas/${encodeURIComponent(ideaId)}` : "/ideas"}
      >
        <ArrowLeft size={16} aria-hidden="true" />
        Back to proposal
      </Link>
      <PageHeading eyebrow="Experiment setup" title="Review before starting">
        <p className="muted">
          Opening this page does not start work. Review the saved proposal,
          workload, and local execution permissions.
        </p>
      </PageHeading>
      <ErrorNotice error={idea.error} />
      {idea.data && idea.data.id === ideaId ? (
        <Setup key={idea.data.id} idea={idea.data} refreshIdea={idea.refresh} />
      ) : idea.loading ? (
        <p role="status">Loading saved proposal…</p>
      ) : null}
    </div>
  );
}
function Setup({
  idea,
  refreshIdea,
}: {
  idea: IdeaRecord;
  refreshIdea: () => void;
}) {
  const { bootstrap, roleConfigId, setRoleConfigId, refreshBootstrap } =
    useStudio();
  const navigate = useNavigate();
  const [restored] = useState(() => savedRequest(idea.id));
  const [requestId, setRequestId] = useState(
    () => restored?.request_id || crypto.randomUUID(),
  );
  const [configId, setConfigId] = useState(
    restored?.bfts_config_id || bootstrap.selected_bfts_config_id,
  );
  const [acknowledged, setAcknowledged] = useState(
    restored?.execution_acknowledged || false,
  );
  const [pending, setPending] = useState<LaunchRequest | null>(restored);
  const pendingRef = useRef<LaunchRequest | null>(restored);
  const busy = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const selectedConfigId = pending?.bfts_config_id || configId;
  const selectedRoleId = pending?.role_config_id || roleConfigId;
  const config = bootstrap.bfts_configs.find(
    (item) => item.id === selectedConfigId,
  );
  const role = bootstrap.role_configs.find(
    (item) => item.id === selectedRoleId,
  );
  const workload = config?.settings;
  const output = `${bootstrap.experiments_directory.replace(/[\\/]$/, "")}/ui_${requestId}`;
  const localBlockers = [
    ...bootstrap.prerequisites.tools
      .filter((tool) => !tool.available)
      .map(
        (tool) =>
          `${tool.name} is unavailable.${tool.error ? ` ${tool.error}` : ""}`,
      ),
    ...Object.entries(idea.errors).map(
      ([field, message]) => `${field}: ${message}`,
    ),
    ...(!config
      ? ["Select an available experiment configuration."]
      : config.error
        ? [config.error]
        : !workload
          ? ["The selected workload could not be read."]
          : workload.exp_name !== "run"
            ? [
                "The experiment configuration must use exp_name: run for this workflow.",
              ]
            : []),
    ...(!role ? ["Select an available role configuration."] : []),
    ...(pending && pending.idea_revision !== idea.revision
      ? [
          `This request uses saved revision ${pending.idea_revision}, but revision ${idea.revision} is now saved. Prepare a new request to use the latest revision.`,
        ]
      : []),
  ];
  const active =
    bootstrap.active_job && isActive(bootstrap.active_job.state)
      ? bootstrap.active_job
      : null;
  const conflictId =
    error instanceof ApiError && typeof error.detail.job_id === "string"
      ? error.detail.job_id
      : null;
  async function start(event: FormEvent) {
    event.preventDefault();
    if (busy.current || localBlockers.length || !acknowledged) return;
    const payload = pendingRef.current || {
      request_id: requestId,
      idea_id: idea.id,
      idea_revision: idea.revision,
      role_config_id: roleConfigId,
      bfts_config_id: configId,
      execution_acknowledged: acknowledged,
    };
    pendingRef.current = payload;
    setPending(payload);
    try {
      sessionStorage.setItem(
        `scientist-studio-launch-${idea.id}`,
        JSON.stringify(payload),
      );
    } catch {
      /* The same request is retained in memory when browser storage is disabled. */
    }
    busy.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await mutate<{ job_id: string; run_id: string }>(
        "/api/experiments",
        payload,
      );
      try {
        sessionStorage.removeItem(`scientist-studio-launch-${idea.id}`);
      } catch {
        /* No scientific data is stored here. */
      }
      refreshBootstrap();
      navigate(`/experiments/${encodeURIComponent(result.job_id)}`);
    } catch (failure) {
      setError(failure);
      refreshBootstrap();
    } finally {
      busy.current = false;
      setSubmitting(false);
    }
  }
  function newRequest() {
    pendingRef.current = null;
    setPending(null);
    setRequestId(crypto.randomUUID());
    setAcknowledged(false);
    setError(null);
    try {
      sessionStorage.removeItem(`scientist-studio-launch-${idea.id}`);
    } catch {
      /* In-memory state is sufficient for this request. */
    }
    refreshIdea();
    refreshBootstrap();
  }
  return (
    <form className="stack" onSubmit={start}>
      <section className="card stack" aria-labelledby="setup-proposal">
        <div className="metadata">
          Saved proposal · Revision {idea.revision}
        </div>
        <h2 id="setup-proposal">
          {typeof idea.idea.Title === "string" && idea.idea.Title.trim()
            ? idea.idea.Title
            : "Untitled proposal"}
        </h2>
        <p className="prose">
          {typeof idea.idea["Short Hypothesis"] === "string" &&
          idea.idea["Short Hypothesis"].trim()
            ? idea.idea["Short Hypothesis"]
            : "No hypothesis saved"}
        </p>
        <details className="advanced">
          <summary>Review saved experiment plan</summary>
          <JsonText value={idea.idea.Experiments ?? null} />
        </details>
        <p className="metadata">
          The experiment snapshots exactly the requested saved revision. Later
          edits cannot change a running experiment.
        </p>
        <Link to={`/ideas/${encodeURIComponent(idea.id)}`}>Edit proposal</Link>
      </section>
      <section className="card stack" aria-labelledby="setup-config">
        <h2 id="setup-config">Configurations and workload</h2>
        <fieldset
          className="generation-settings stack"
          disabled={submitting || Boolean(pending)}
        >
          {pending ? (
            <label className="field" htmlFor="submitted-role-config">
              Role configuration
              <input
                id="submitted-role-config"
                readOnly
                value={role?.label || selectedRoleId}
              />
            </label>
          ) : (
            <ConfigSelect id="setup-role-config" />
          )}
          <label className="field" htmlFor="experiment-config">
            Experiment configuration
            <select
              id="experiment-config"
              value={configId}
              onChange={(event) => setConfigId(event.target.value)}
              required
            >
              {bootstrap.bfts_configs.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
        {pending && (
          <p className="notice">
            Submitted request: {role?.label || selectedRoleId} ·{" "}
            {config?.label || selectedConfigId}. Retry keeps these exact
            settings and saved revision.
          </p>
        )}
        <p className="metadata">
          Role configuration: {role?.label || "Not selected"}. The reduced
          validation workload, if available, is an explicit optional preset—not
          a publication-quality setting.
        </p>
        {workload && (
          <div className="workload">
            <dl className="key-value">
              <div>
                <dt>Worker count</dt>
                <dd>{workload.num_workers ?? "Not specified"}</dd>
              </div>
              <div>
                <dt>Seeds</dt>
                <dd>{workload.num_seeds ?? "Not specified"}</dd>
              </div>
              <div>
                <dt>Execution timeout</dt>
                <dd>
                  {workload.execution_timeout === null
                    ? "Not specified"
                    : `${workload.execution_timeout} seconds`}
                </dd>
              </div>
              <div>
                <dt>Experiment name</dt>
                <dd>{workload.exp_name || "Not specified"}</dd>
              </div>
            </dl>
            <h3>Stage iteration limits</h3>
            <dl className="key-value">
              {Object.entries(workload.stage_iterations).map(
                ([stage, limit]) => (
                  <div key={stage}>
                    <dt>{stage.replaceAll("_", " ")}</dt>
                    <dd>{limit ?? "Not specified"}</dd>
                  </div>
                ),
              )}
            </dl>
            {Object.keys(workload.stage_iterations).length === 0 && (
              <p className="muted">
                No stage iteration limits were reported by this configuration.
              </p>
            )}
          </div>
        )}
        <div className="notice">
          <strong>Output directory</strong>
          <p className="output-path">
            <code>{output}</code>
          </p>
          <p className="metadata">
            A fresh exclusive directory is allocated only after Start. No
            existing results are reused.
          </p>
        </div>
        <p>ICBINB paper workflow · Writeup and reviews enabled</p>
        <p className="metadata">
          Custom proposals run without loading existing code or adding a dataset
          reference. No runtime or cost estimate is available.
        </p>
      </section>
      <section className="card stack" aria-labelledby="local-readiness">
        <h2 id="local-readiness">Local readiness</h2>
        <ul className="prerequisite-list">
          {bootstrap.prerequisites.tools.map((tool) => (
            <li key={tool.name}>
              <strong>{tool.name}</strong> —{" "}
              {tool.available ? "Available" : "Unavailable"}
            </li>
          ))}
        </ul>
        {localBlockers.length > 0 && (
          <div className="error-notice" role="alert">
            <strong>Start is blocked</strong>
            <ul>
              {localBlockers.map((blocker, index) => (
                <li key={index}>{blocker}</li>
              ))}
            </ul>
            <p>No dependency will be installed automatically.</p>
          </div>
        )}
        <div className="actions">
          <button
            type="button"
            className="button secondary"
            disabled={submitting}
            onClick={() => {
              refreshBootstrap();
              refreshIdea();
            }}
          >
            Refresh local readiness
          </button>
          <Link to="/models">
            Review model assignments and availability{" "}
            <ArrowRight size={16} aria-hidden="true" />
          </Link>
        </div>
        <p className="metadata">
          Start validates the selected configurations and checks endpoint model
          availability before reserving the job. Model listing does not prove
          that a text or image request will succeed.
        </p>
        {active && (
          <div className="notice">
            <p>
              Another job currently holds the single compute slot. Stop it or
              wait for it to finish before starting new work.
            </p>
            <Link to={`/experiments/${encodeURIComponent(active.id)}`}>
              Open active {active.kind === "idea" ? "generation" : "experiment"}{" "}
              job
            </Link>
          </div>
        )}
      </section>
      <section
        className="card stack execution-confirmation"
        aria-labelledby="execution-permissions"
      >
        <h2 id="execution-permissions">
          <ShieldAlert size={24} aria-hidden="true" />
          Generated code permissions
        </h2>
        <label className="execution-ack" htmlFor="execution-ack">
          <input
            id="execution-ack"
            type="checkbox"
            checked={acknowledged}
            disabled={submitting || Boolean(pending)}
            onChange={(event) => setAcknowledged(event.target.checked)}
            required
          />
          <span>
            This runs generated Python code on this PC. It can read and write
            files available to your account.
          </span>
        </label>
        <p className="metadata">
          This is not a sandbox. Nothing starts until you select Start
          experiment.
        </p>
        <ErrorNotice error={error} />
        {conflictId && (
          <Link to={`/experiments/${encodeURIComponent(conflictId)}`}>
            Open the job recorded by the server
          </Link>
        )}
        <div className="actions">
          <button
            className="button primary"
            type="submit"
            disabled={
              submitting ||
              !acknowledged ||
              localBlockers.length > 0 ||
              Boolean(active && active.id !== requestId)
            }
          >
            <Play size={18} aria-hidden="true" />
            {submitting
              ? "Validating and starting…"
              : pending
                ? "Retry Start experiment"
                : "Start experiment"}
          </button>
          {pending && !submitting && (
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                if (
                  window.confirm(
                    "Create a new request with a new output directory? The previous request may already exist on the server; check Experiments first if its result was unclear.",
                  )
                ) {
                  if (pending.role_config_id !== roleConfigId)
                    setRoleConfigId(pending.role_config_id);
                  newRequest();
                }
              }}
            >
              Prepare a new request
            </button>
          )}
        </div>
        {pending && (
          <p className="metadata">
            Retry uses request ID {requestId}. Repeated submissions return the
            same job; they do not launch a second experiment.
          </p>
        )}
      </section>
    </form>
  );
}
