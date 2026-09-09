import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, Play, ShieldAlert } from "lucide-react";
import { ApiError, isActive, mutate, useApi, type Bootstrap, type IdeaRecord, type ModelsView, type ModelRole } from "../api";
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

const stageLabels: Record<string, string> = {
  stage1: "Build a working implementation",
  stage2: "Tune the baseline",
  stage3: "Explore the research idea",
  stage4: "Run ablation studies",
};

function workloadLabel(config: Bootstrap["bfts_configs"][number]) {
  return config.label === "bfts_config.yaml" ? "Standard research run" : config.label;
}

function ResearchModels({ configId }: { configId: string }) {
  const view = useApi<ModelsView>(`/api/models?config_id=${encodeURIComponent(configId)}`);
  const models = view.data?.config_id === configId ? view.data : undefined;
  const primaryRoles = ["experiment_code", "experiment_feedback", "writeup"];
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
          <dl className="setup-models">{researchRoles.filter((role) => primaryRoles.includes(role.name)).map(assignment)}</dl>
          {researchRoles.some((role) => !primaryRoles.includes(role.name)) && (
            <details className="setup-details">
              <summary>Analysis, figures, citations, and other assignments</summary>
              <dl className="setup-models">{researchRoles.filter((role) => !primaryRoles.includes(role.name)).map(assignment)}</dl>
            </details>
          )}
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
    <form className="setup-page stack" onSubmit={start}>
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
          <section className="card stack" aria-labelledby="setup-research-models">
            <div>
              <h2 id="setup-research-models">Which models do the research?</h2>
              <p>These assignments tell AI-Scientist who writes the experiment code, evaluates its outputs, and writes the paper.</p>
              <p className="setup-distinction"><strong>Research models are not the models being studied.</strong> A model named in your proposal is an experimental subject. Selecting it in the proposal does not load it or assign it to the research roles below.</p>
            </div>
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
          </section>

          <section className="card stack" aria-labelledby="local-readiness">
            <div>
              <h2 id="local-readiness">What has actually been checked?</h2>
              <p>A saved proposal is not proof that its models, datasets, or runtime are ready.</p>
            </div>
            <div className="setup-check-group">
              <h3>Checked here: paper-processing tools</h3>
              <ul className="setup-checks">
                {bootstrap.prerequisites.tools.map((tool) => (
                  <li key={tool.name}>
                    <span><strong>{tool.name === "pdflatex" ? "Build the PDF" : tool.name === "bibtex" ? "Build the bibliography" : tool.name === "pdftotext" ? "Read PDF text" : tool.name}</strong><small>{tool.name}</small></span>
                    <span className={tool.available ? "setup-check-ok" : "setup-check-missing"}>{tool.available ? "Available" : "Missing"}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="setup-check-group">
              <h3>Checked when you press Start</h3>
              <p>The saved configurations and research endpoint model listings. A listed model is not proof that inference will succeed.</p>
            </div>
            <div className="setup-unverified">
              <h3>Not verified by this screen</h3>
              <p>The model and dataset required by your study, GPU memory fit, context capacity, runtime features such as speculative decoding, and benchmark test environments. These requirements stay in the saved design; this page does not install or validate them.</p>
            </div>
            {localBlockers.length > 0 && (
              <div className="error-notice" role="alert">
                <strong>Resolve these before starting</strong>
                <ul>{localBlockers.map((blocker, index) => <li key={index}>{blocker}</li>)}</ul>
                <p>No dependency will be installed automatically by this preparation page.</p>
              </div>
            )}
            <div className="actions">
              <button className="button secondary" type="button" disabled={submitting}
                onClick={() => { refreshBootstrap(); refreshIdea(); }}>Recheck local tools and saved settings</button>
            </div>
            {active && (
              <div className="notice">
                <p>Another job is using the compute slot. Wait for it to finish or stop it before starting this run.</p>
                <Link to={`/experiments/${encodeURIComponent(active.id)}`}>Open active {active.kind === "idea" ? "generation" : "experiment"} job</Link>
              </div>
            )}
          </section>
        </div>

        <aside className="card stack setup-run-settings" aria-labelledby="setup-config">
          <div>
            <h2 id="setup-config">Run settings</h2>
            <p>These limits control AI-Scientist’s research process, not the number of benchmark issues or comparison arms in your study.</p>
          </div>
          <fieldset className="generation-settings stack" disabled={submitting || Boolean(pending)}>
            <label className="field" htmlFor="experiment-config">
              Research workload
              <select id="experiment-config" value={configId} onChange={(event) => setConfigId(event.target.value)} required aria-describedby="workload-help">
                {bootstrap.bfts_configs.map((item) => <option value={item.id} key={item.id}>{workloadLabel(item)}</option>)}
              </select>
            </label>
            <p className="metadata" id="workload-help">Chooses an existing set of run limits. The reduced validation workload is for checking the pipeline, not a publication-quality study.</p>
          </fieldset>
          {workload && (
            <>
              <dl className="setup-limits">
                <div>
                  <dt>Parallel research workers <strong>{workload.num_workers ?? "Not specified"}</strong></dt>
                  <dd>How many research workers can develop and execute experiment attempts concurrently.</dd>
                </div>
                <div>
                  <dt>Repeated evaluations <strong>{workload.num_seeds ?? "Not specified"}</strong></dt>
                  <dd>Random seeds requested for the pipeline’s multi-seed evaluation. This does not set your benchmark sample size.</dd>
                </div>
                <div>
                  <dt>Time limit per code execution <strong>{workload.execution_timeout === null ? "Not specified" : `${workload.execution_timeout} seconds`}</strong></dt>
                  <dd>Limits one generated-code execution, not the total research run. Total runtime and cost are not estimated.</dd>
                </div>
              </dl>
              <details className="setup-details">
                <summary>Advanced: research stages and technical settings</summary>
                <p className="metadata">Configured iteration limits for the research search stages—not the comparisons in your proposal.</p>
                <dl className="setup-limits">
                  {Object.entries(workload.stage_iterations).map(([stage, limit]) => (
                    <div key={stage}><dt>{stageLabels[stage] || stage}<strong>{limit ?? "Not specified"}</strong></dt></div>
                  ))}
                </dl>
                {Object.keys(workload.stage_iterations).length === 0 && <p className="muted">Stage limits are not specified.</p>}
                <dl className="setup-technical">
                  <div><dt>Workload preset</dt><dd>{config?.label}</dd></div>
                  <div><dt>Model preset</dt><dd>{role?.label || "Not selected"}</dd></div>
                  <div><dt>Internal experiment name</dt><dd>{workload.exp_name || "Not specified"}</dd></div>
                  <div><dt>Paper workflow</dt><dd>ICBINB · Writeup and reviews enabled</dd></div>
                  <div><dt>Output directory</dt><dd className="output-path"><code>{output}</code></dd></div>
                </dl>
                <p className="metadata">A new output directory is created after Start; existing results are not reused. The launcher supplies no existing code or dataset reference for this custom proposal.</p>
              </details>
            </>
          )}
        </aside>
      </div>

      <section className="card stack execution-confirmation setup-start" aria-labelledby="execution-permissions">
        <div>
          <h2 id="execution-permissions"><ShieldAlert size={24} aria-hidden="true" /> Start this research run</h2>
          <p>This starts code generation, execution, analysis, and paper writing using the saved study and selected settings above.</p>
        </div>
        <label className="execution-ack" htmlFor="execution-ack">
          <input id="execution-ack" type="checkbox" checked={acknowledged} disabled={submitting || Boolean(pending)}
            onChange={(event) => setAcknowledged(event.target.checked)} required />
          <span>I understand this runs generated Python code on this PC. It can read and write files available to my account. This is not a sandbox.</span>
        </label>
        {pending && <p className="notice">A start request was submitted. Retry keeps its exact saved revision, model configuration, workload, and output directory; it does not launch a duplicate job.</p>}
        <ErrorNotice error={error} />
        {conflictId && <Link to={`/experiments/${encodeURIComponent(conflictId)}`}>Open the job recorded by the server</Link>}
        <div className="actions">
          <button className="button primary" type="submit"
            disabled={submitting || !acknowledged || localBlockers.length > 0 || Boolean(active && active.id !== requestId)}>
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
        {!acknowledged && !pending && <p className="metadata">Acknowledge the code permissions to enable Start. Opening or reviewing this page never launches work.</p>}
      </section>
    </form>
  );
}
