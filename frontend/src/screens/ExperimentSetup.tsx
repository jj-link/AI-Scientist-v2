import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Play, ShieldAlert } from "lucide-react";
import { ApiError, isActive, mutate, useApi, type IdeaRecord, type ModelsView, type ModelRole, type RunSettings, type Workload } from "../api";
import {
  ConfigSelect,
  ErrorNotice,
  JsonText,
  PageHeading,
  ROLE_HELP,
  CUSTOM_ROLE_HELP,
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
  run_settings: RunSettings;
};
function savedRequest(ideaId: string): LaunchRequest | null {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(`scientist-studio-launch-${ideaId}`) || "null",
    ) as LaunchRequest | null;
    if (value?.idea_id === ideaId && validRunSettings(value.run_settings)) return value;
    sessionStorage.removeItem(`scientist-studio-launch-${ideaId}`);
    return null;
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
        className="setup-back"
        to={ideaId ? `/ideas/${encodeURIComponent(ideaId)}` : "/ideas"}
      >
        <ArrowLeft size={16} aria-hidden="true" />
        Back to proposal
      </Link>
      <PageHeading eyebrow="Research setup" title="Prepare this experiment">
        <p className="muted">
          Review the study, the research models, and the run limits. Nothing starts on opening this page.
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

const stageLabels = {
  stage1: "Build a working implementation",
  stage2: "Tune the baseline",
  stage3: "Explore the research idea",
  stage4: "Run ablation studies",
} as const;
type Stage = keyof typeof stageLabels;
const stages = Object.keys(stageLabels) as Stage[];
const runFields = ["num_workers", "num_seeds", "execution_timeout", ...stages] as const;
type RunField = (typeof runFields)[number];
type RunDraft = Record<RunField, string>;

function positiveNumber(value: unknown, integer: boolean): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 &&
    (!integer || Number.isInteger(value));
}

function validRunSettings(value: unknown): value is RunSettings {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const settings = value as Record<string, unknown>;
  const limits = settings.stage_iterations;
  return Object.keys(settings).length === 4 &&
    positiveNumber(settings.num_workers, true) &&
    positiveNumber(settings.num_seeds, true) &&
    positiveNumber(settings.execution_timeout, false) &&
    !!limits && typeof limits === "object" && !Array.isArray(limits) &&
    Object.keys(limits).length === 4 &&
    stages.every((stage) => positiveNumber((limits as Record<string, unknown>)[stage], true));
}

function runDraft(settings: Workload | RunSettings | null | undefined): RunDraft {
  return {
    num_workers: String(settings?.num_workers ?? ""),
    num_seeds: String(settings?.num_seeds ?? ""),
    execution_timeout: String(settings?.execution_timeout ?? ""),
    stage1: String(settings?.stage_iterations.stage1 ?? ""),
    stage2: String(settings?.stage_iterations.stage2 ?? ""),
    stage3: String(settings?.stage_iterations.stage3 ?? ""),
    stage4: String(settings?.stage_iterations.stage4 ?? ""),
  };
}

function savedDraft(ideaId: string): { configId: string; values: RunDraft } | null {
  try {
    const draft = JSON.parse(sessionStorage.getItem(`scientist-studio-run-settings-${ideaId}`) || "null");
    return draft && typeof draft.configId === "string" && draft.values &&
      runFields.every((field) => typeof draft.values[field] === "string") ? draft : null;
  } catch {
    return null;
  }
}

function ResearchModels({ configId }: { configId: string }) {
  const view = useApi<ModelsView>(`/api/models?config_id=${encodeURIComponent(configId)}`);
  const models = view.data?.config_id === configId ? view.data : undefined;
  const researchRoles = models?.roles.filter((role) => role.name !== "ideation") || [];
  function assignment(role: ModelRole) {
    const help = Object.hasOwn(ROLE_HELP, role.name) ? ROLE_HELP[role.name] : CUSTOM_ROLE_HELP;
    const endpoint = models?.endpoints.find((item) => item.id === role.endpoint);
    return (
      <div className="setup-model" key={role.name}>
        <dt>{help.title}</dt>
        <dd>
          <strong>{role.model || "No model assigned"}</strong>
          <span className="metadata">{endpoint?.label || role.endpoint || "No endpoint assigned"}</span>
          <span className="metadata">{help.description}</span>
        </dd>
      </div>
    );
  }
  return (
    <>
      <ErrorNotice error={view.error} />
      {Boolean(view.error) && <button className="button secondary" type="button" onClick={view.refresh}>Reload model assignments</button>}
      {!models && !view.error && <p role="status">Loading saved model assignments…</p>}
      {models && (
        <>
          {researchRoles.length === 0 && <p className="notice">No research roles are configured in this preset. Review the assignments in Models.</p>}
          <dl className="setup-models">{researchRoles.map(assignment)}</dl>
        </>
      )}
    </>
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
  const [storedDraft] = useState(() => savedDraft(idea.id));
  const [requestId, setRequestId] = useState(
    () => restored?.request_id || crypto.randomUUID(),
  );
  const [configId] = useState(
    restored?.bfts_config_id || storedDraft?.configId || bootstrap.selected_bfts_config_id,
  );
  const [values, setValues] = useState<RunDraft>(() => restored
    ? runDraft(restored.run_settings)
    : storedDraft?.values || runDraft(bootstrap.bfts_configs.find((item) => item.id === configId)?.settings));
  useEffect(() => {
    try {
      sessionStorage.setItem(`scientist-studio-run-settings-${idea.id}`, JSON.stringify({ configId, values }));
    } catch {
      /* Keep the draft in memory when browser storage is disabled. */
    }
  }, [configId, idea.id, values]);
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
  const fieldErrors: Partial<Record<RunField, string>> = {};
  for (const field of runFields) {
    const integer = field !== "execution_timeout";
    if (!values[field].trim() || !positiveNumber(Number(values[field]), integer)) {
      fieldErrors[field] = integer
        ? "Enter a positive whole number."
        : "Enter a finite number greater than zero.";
    }
  }
  const invalidSettings = Object.keys(fieldErrors).length > 0;
  if (error instanceof ApiError && Array.isArray(error.detail.errors)) {
    for (const issue of error.detail.errors) {
      if (!issue || typeof issue.field !== "string" || typeof issue.message !== "string") continue;
      const field = issue.field.replace(/^run_settings\.(stage_iterations\.)?/, "");
      if (runFields.includes(field as RunField)) fieldErrors[field as RunField] = issue.message;
    }
  }
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
      ? ["The experiment configuration is unavailable. Restore it before starting."]
      : config.error
        ? [config.error]
        : !workload
          ? ["The experiment configuration could not be read."]
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
    if (busy.current || localBlockers.length || invalidSettings || !acknowledged ||
      (active && active.id !== requestId)) return;
    const payload = pendingRef.current || {
      request_id: requestId,
      idea_id: idea.id,
      idea_revision: idea.revision,
      role_config_id: roleConfigId,
      bfts_config_id: configId,
      execution_acknowledged: acknowledged,
      run_settings: {
        num_workers: Number(values.num_workers),
        num_seeds: Number(values.num_seeds),
        execution_timeout: Number(values.execution_timeout),
        stage_iterations: {
          stage1: Number(values.stage1),
          stage2: Number(values.stage2),
          stage3: Number(values.stage3),
          stage4: Number(values.stage4),
        },
      },
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
        sessionStorage.removeItem(`scientist-studio-run-settings-${idea.id}`);
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
  function settingInput(field: RunField, label: string, help?: string) {
    const id = `run-${field}`;
    const message = fieldErrors[field];
    return (
      <div className="setup-run-field" key={field}>
        <label className="field" htmlFor={id}>
          {label}
          <input id={id} type="number" min={field === "execution_timeout" ? 0 : 1}
            step={field === "execution_timeout" ? "any" : 1} required value={values[field]}
            onChange={(event) => setValues((current) => ({ ...current, [field]: event.target.value }))}
            aria-invalid={Boolean(message)}
            aria-describedby={[help ? `${id}-help` : null, message ? `${id}-error` : null].filter(Boolean).join(" ") || undefined} />
        </label>
        {help && <p className="metadata" id={`${id}-help`}>{help}</p>}
        {message && <p className="field-error" id={`${id}-error`} role="alert">{message}</p>}
      </div>
    );
  }
  return (
    <form className="setup-page stack" onSubmit={start} noValidate>
      <section className="card setup-study stack" aria-labelledby="setup-proposal">
        <div className="metadata">Your saved study · Revision {idea.revision}</div>
        <h2 id="setup-proposal">
          {typeof idea.idea.Title === "string" && idea.idea.Title.trim() ? idea.idea.Title : "Untitled proposal"}
        </h2>
        <p className="setup-hypothesis">
          {typeof idea.idea["Short Hypothesis"] === "string" && idea.idea["Short Hypothesis"].trim()
            ? idea.idea["Short Hypothesis"] : "No hypothesis saved"}
        </p>
        <div className="setup-launch-description">
          <h3>What Start experiment launches</h3>
          <p>AI-Scientist uses this saved design to write and debug experiment code, execute it, analyze the results, and produce a paper with figures, citations, and reviews. This starts the research pipeline—not an already-built benchmark runner.</p>
        </div>
        <div>
          <h3>Planned comparisons and procedures</h3>
          <div className="setup-comparisons">
            {(Array.isArray(idea.idea.Experiments) ? idea.idea.Experiments : [idea.idea.Experiments ?? null]).map((experiment, index) => {
              const fields = experiment && typeof experiment === "object" && !Array.isArray(experiment) ? experiment : null;
              const titleField = fields && typeof fields.Title === "string" ? "Title" : fields && typeof fields.Name === "string" ? "Name" : null;
              return (
                <details className="setup-details" key={index}>
                  <summary>{fields && titleField ? String(fields[titleField]) : `Experiment ${index + 1}`}</summary>
                  <JsonText value={fields && titleField ? Object.fromEntries(Object.entries(fields).filter(([key]) => key !== titleField)) : experiment} />
                </details>
              );
            })}
          </div>
        </div>
        <details className="setup-details">
          <summary>Study details: models being studied, data, requirements, and open decisions</summary>
          <p className="metadata">These are the requirements in your saved design, not a report of installed or verified resources.</p>
          <JsonText value={Object.fromEntries(Object.entries(idea.idea).filter(([field]) =>
            !["Name", "Title", "Short Hypothesis", "Experiments"].includes(field)
          ))} />
        </details>
        <div className="actions">
          <Link to={`/ideas/${encodeURIComponent(idea.id)}`}>Discuss or refine this study <ArrowRight size={16} aria-hidden="true" /></Link>
        </div>
        <p className="metadata">The run takes a snapshot of this saved revision. Later discussion cannot change a running experiment.</p>
      </section>

      <div className="setup-columns">
        <div className="stack">
          <details className="card setup-role-assignments">
            <summary id="setup-research-models">Role assignments</summary>
            <div className="stack">
            <ResearchModels key={selectedRoleId} configId={selectedRoleId} />
            <Link to="/models">Edit research model assignments <ArrowRight size={16} aria-hidden="true" /></Link>
            <details className="setup-details">
              <summary>Advanced: select a saved model configuration</summary>
              <fieldset className="generation-settings stack" disabled={submitting || Boolean(pending)}>
                {pending ? (
                  <label className="field" htmlFor="submitted-role-config">
                    Submitted model configuration
                    <input id="submitted-role-config" readOnly value={role?.label || selectedRoleId} />
                  </label>
                ) : <ConfigSelect id="setup-role-config" label="Saved model configuration" />}
              </fieldset>
              <p className="metadata">Changes to model assignments are made in Models. This selector chooses which saved configuration the run will copy.</p>
            </details>
            </div>
          </details>
        </div>

        <aside className="card stack setup-run-settings" aria-labelledby="setup-config">
          <div>
            <h2 id="setup-config">Run settings</h2>
            <p>These limits control AI-Scientist’s research process, not the number of benchmark issues or comparison arms in your study.</p>
          </div>
          <p className="metadata">Edit limits for this run only. Defaults come from the selected experiment configuration; starting copies these values into the new job without changing configuration files.</p>
          <fieldset className="generation-settings stack" disabled={submitting || Boolean(pending)}>
            <legend className="sr-only">Run limits</legend>
            {settingInput("num_workers", "Parallel research workers",
              "How many research workers can develop and execute experiment attempts concurrently.")}
            {settingInput("num_seeds", "Repeated evaluations (seeds)",
              "Random seeds requested for the pipeline’s multi-seed evaluation. This does not set your benchmark sample size.")}
            {settingInput("execution_timeout", "Time limit per code execution (seconds)",
              "Limits one generated-code execution, not the total research run. Fractional seconds are allowed. Total runtime and cost are not estimated.")}
            <details className="setup-details">
              <summary>Advanced: research stages and technical settings{stages.some((stage) => fieldErrors[stage]) ? " — check stage limits" : ""}</summary>
              <p className="metadata">Iteration limits for the research search stages—not the comparisons in your proposal.</p>
              <div className="setup-stage-inputs">
                {stages.map((stage) => settingInput(stage, `${stage.replace("stage", "Stage ")}: ${stageLabels[stage]} (iterations)`))}
              </div>
              <dl className="setup-technical">
                <div><dt>Model preset</dt><dd>{role?.label || "Not selected"}</dd></div>
                <div><dt>Internal experiment name</dt><dd>{workload?.exp_name || "Not specified"}</dd></div>
                <div><dt>Paper workflow</dt><dd>ICBINB · Writeup and reviews enabled</dd></div>
                <div><dt>Output directory</dt><dd className="output-path"><code>{output}</code></dd></div>
              </dl>
              <p className="metadata">A new output directory is created after Start; existing results are not reused. The launcher supplies no existing code or dataset reference for this custom proposal.</p>
            </details>
          </fieldset>
        </aside>
      </div>

      <section className="card stack execution-confirmation setup-start" aria-labelledby="execution-permissions">
        <div>
          <h2 id="execution-permissions"><ShieldAlert size={24} aria-hidden="true" /> Start this research run</h2>
          <p>This starts code generation, execution, analysis, and paper writing using the saved study and selected settings above.</p>
        </div>
            {localBlockers.length > 0 && (
              <div className="error-notice" role="alert">
                <strong>Resolve these before starting</strong>
                <ul>{localBlockers.map((blocker, index) => <li key={index}>{blocker}</li>)}</ul>
                <p>No dependency will be installed automatically by this preparation page.</p>
            <div className="actions">
              <button className="button secondary" type="button" disabled={submitting}
                onClick={() => { refreshBootstrap(); refreshIdea(); }}>Recheck local tools and saved settings</button>
            </div>
              </div>
            )}
            {active && (
              <div className="notice">
                <p>Another job is using the compute slot. Wait for it to finish or stop it before starting this run.</p>
                <Link to={`/experiments/${encodeURIComponent(active.id)}`}>Open active {active.kind === "idea" ? "generation" : "experiment"} job</Link>
              </div>
            )}
        <label className="execution-ack" htmlFor="execution-ack">
          <input id="execution-ack" type="checkbox" checked={acknowledged} disabled={submitting || Boolean(pending)}
            onChange={(event) => setAcknowledged(event.target.checked)} required />
          <span>I understand this runs generated Python code on this PC. It can read and write files available to my account. This is not a sandbox.</span>
        </label>
        {pending && <p className="notice">A start request was submitted. Retry keeps its exact saved revision, model configuration, run settings, and output directory; it does not launch a duplicate job. Prepare a new request to edit these settings.</p>}
        <ErrorNotice error={error} />
        {conflictId && <Link to={`/experiments/${encodeURIComponent(conflictId)}`}>Open the job recorded by the server</Link>}
        <div className="actions">
          <button className="button primary" type="submit"
            disabled={submitting || !acknowledged || invalidSettings || localBlockers.length > 0 || Boolean(active && active.id !== requestId)}>
            <Play size={18} aria-hidden="true" /> {submitting ? "Validating and starting…" : pending ? "Retry Start experiment" : "Start experiment"}
          </button>
          {pending && !submitting && (
            <button className="button secondary" type="button" onClick={() => {
              if (window.confirm("Create a new request with a new output directory? The previous request may already exist on the server; check Experiments first if its result was unclear.")) {
                if (pending.role_config_id !== roleConfigId) setRoleConfigId(pending.role_config_id);
                newRequest();
              }
            }}>Prepare a new request</button>
          )}
        </div>
        {pending && <p className="metadata">Request ID: {requestId}</p>}
        {invalidSettings && <p className="field-error" role="alert">Correct the highlighted run settings before starting, including any limits under Advanced.</p>}
        {!acknowledged && !pending && <p className="metadata">Acknowledge the code permissions to enable Start. Opening or reviewing this page never launches work.</p>}
      </section>
    </form>
  );
}
