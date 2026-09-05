import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Plus, Sparkles } from "lucide-react";
import {
  ApiError,
  isActive,
  mutate,
  useApi,
  type IdeaRecord,
  type Job,
  type ModelsView,
} from "../api";
import { ConfigSelect, ErrorNotice, PageHeading } from "../components";
import { useStudio } from "../studio";
import JobMonitor from "../JobMonitor";
import "./ideas.css";

type GenerationRequest = {
  request_id: string;
  research_question: string;
  context: string;
  attempts: number;
  rounds: number;
  role_config_id: string;
};
type SavedForm = {
  question?: string;
  context?: string;
  attempts?: number;
  rounds?: number;
  jobId?: string;
  pending?: GenerationRequest;
};
const storageKey = "scientist-studio-generation";
function storedForm(): SavedForm {
  try {
    return JSON.parse(localStorage.getItem(storageKey) || "{}") as SavedForm;
  } catch {
    return {};
  }
}
const examples = [
  "How does training-data noise affect a small neural network’s generalization?",
  "Can a simpler regularization method improve performance on a limited dataset?",
  "Which features make an optimization method robust across random seeds?",
];
function preview(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(preview).join(" · ");
  if (value && typeof value === "object")
    return Object.entries(value)
      .map(([key, item]) => `${key}: ${preview(item)}`)
      .join(" · ");
  return value == null ? "" : String(value);
}

export default function Ideas() {
  const { bootstrap, roleConfigId, refreshBootstrap } = useStudio();
  const [initial] = useState(storedForm);
  const [question, setQuestion] = useState(initial.question || "");
  const [context, setContext] = useState(initial.context || "");
  const [attempts, setAttempts] = useState(initial.attempts || 1);
  const [rounds, setRounds] = useState(initial.rounds || 5);
  const [jobId, setJobId] = useState(
    bootstrap.active_job?.kind === "idea"
      ? bootstrap.active_job.id
      : initial.jobId || "",
  );
  const [job, setJob] = useState<Job | null>(
    bootstrap.active_job?.kind === "idea" ? bootstrap.active_job : null,
  );
  const [pending, setPending] = useState<GenerationRequest | undefined>(
    initial.pending,
  );
  const pendingRef = useRef(pending);
  const busyRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const active = isActive(job?.state) || Boolean(jobId && !job);
  const ideas = useApi<{ ideas: IdeaRecord[] }>(
    "/api/ideas",
    active ? 2000 : 0,
  );
  const effectiveRoleId = pending?.role_config_id || roleConfigId;
  const models = useApi<ModelsView>(
    effectiveRoleId
      ? `/api/models?config_id=${encodeURIComponent(effectiveRoleId)}`
      : null,
  );
  const ideation = models.data?.roles.find((role) => role.name === "ideation");
  const lastUpdate = useRef("");
  const onUpdate = useCallback(
    (next: Job) => {
      setJob(next);
      if (lastUpdate.current !== next.updated_at) {
        lastUpdate.current = next.updated_at;
        ideas.refresh();
        if (!isActive(next.state)) refreshBootstrap();
      }
    },
    [ideas.refresh, refreshBootstrap],
  );
  useEffect(() => {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({ question, context, attempts, rounds, jobId, pending }),
      );
    } catch {
      /* Storage may be unavailable; the server retains submitted requests. */
    }
  }, [question, context, attempts, rounds, jobId, pending]);
  async function generate(event: FormEvent) {
    event.preventDefault();
    if (busyRef.current || active) return;
    const payload = pendingRef.current || {
      request_id: crypto.randomUUID(),
      research_question: question,
      context,
      attempts,
      rounds,
      role_config_id: roleConfigId,
    };
    pendingRef.current = payload;
    setPending(payload);
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          question,
          context,
          attempts,
          rounds,
          jobId,
          pending: payload,
        }),
      );
    } catch {
      /* Server persistence remains authoritative. */
    }
    busyRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await mutate<{ job_id: string }>(
        "/api/idea-jobs",
        payload,
      );
      setJob(null);
      setJobId(result.job_id);
      refreshBootstrap();
      ideas.refresh();
    } catch (failure) {
      setError(failure);
    } finally {
      busyRef.current = false;
      setSubmitting(false);
    }
  }
  function newRequest() {
    pendingRef.current = undefined;
    setPending(undefined);
    setJobId("");
    setJob(null);
    setError(null);
  }
  const finished = Boolean(job && !isActive(job.state));
  const finalized =
    typeof job?.result?.finalized === "number"
      ? job.result.finalized
      : ideas.data?.ideas.filter((idea) => idea.job_id === jobId).length || 0;
  const attempted =
    typeof job?.result?.attempted === "number" ? job.result.attempted : null;
  const locked = submitting || active || Boolean(pending);
  const conflictId =
    error instanceof ApiError && typeof error.detail.job_id === "string"
      ? error.detail.job_id
      : null;
  return (
    <div className="stack">
      <PageHeading eyebrow="Ideas" title="What do you want to investigate?" />
      <form className="card stack" onSubmit={generate}>
        <p className="muted">
          Describe a question, hypothesis, or problem. You can edit the
          proposals before running an experiment.
        </p>
        <label className="field" htmlFor="research-question">
          Research question or hypothesis
          <textarea
            id="research-question"
            rows={5}
            required
            maxLength={50000}
            disabled={locked}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="What would you like to investigate?"
          />
        </label>
        <div className="stack example-topics">
          <span className="metadata">
            Example topics — select one to fill the question, not start work
          </span>
          <div className="row">
            {examples.map((example) => (
              <button
                type="button"
                className="button secondary example-topic"
                key={example}
                disabled={locked}
                onClick={() => setQuestion(example)}
              >
                {example}
              </button>
            ))}
          </div>
        </div>
        <details className="advanced">
          <summary>Constraints and context</summary>
          <label className="field" htmlFor="research-context">
            Constraints and context
            <textarea
              id="research-context"
              rows={4}
              maxLength={50000}
              disabled={locked}
              value={context}
              onChange={(event) => setContext(event.target.value)}
            />
          </label>
          <p className="metadata">
            The question and context are sent directly to the proposal generator
            under literal headings.
          </p>
        </details>
        <details className="advanced">
          <summary>Advanced generation settings</summary>
          <fieldset disabled={locked} className="generation-settings stack">
            <div className="split">
              <label className="field" htmlFor="proposal-attempts">
                Proposal attempts
                <input
                  id="proposal-attempts"
                  type="number"
                  min={1}
                  max={10}
                  step={1}
                  required
                  value={Number.isNaN(attempts) ? "" : attempts}
                  onChange={(event) => setAttempts(event.target.valueAsNumber)}
                  aria-describedby="attempt-help"
                />
                <span id="attempt-help" className="metadata">
                  An attempt may not produce a finalized proposal.
                </span>
              </label>
              <label className="field" htmlFor="generation-rounds">
                Generation rounds per attempt
                <input
                  id="generation-rounds"
                  type="number"
                  min={2}
                  max={20}
                  step={1}
                  required
                  value={Number.isNaN(rounds) ? "" : rounds}
                  onChange={(event) => setRounds(event.target.valueAsNumber)}
                  aria-describedby="round-help"
                />
                <span id="round-help" className="metadata">
                  Includes the first model call. The same model is used for all
                  rounds.
                </span>
              </label>
            </div>
            {pending ? (
              <label className="field" htmlFor="generation-role-config">
                Role configuration
                <input
                  id="generation-role-config"
                  readOnly
                  value={
                    bootstrap.role_configs.find(
                      (config) => config.id === effectiveRoleId,
                    )?.label || effectiveRoleId
                  }
                />
              </label>
            ) : (
              <ConfigSelect id="generation-role-config" />
            )}
          </fieldset>
          <ErrorNotice error={models.error} />
          <div className="notice">
            <strong>Model: role/ideation</strong>
            {ideation ? (
              <p>
                {ideation.model || "No model assigned"} ·{" "}
                {ideation.endpoint || "No endpoint assigned"} · Effective token
                budget: {ideation.effective_max_tokens ?? "not specified"}
              </p>
            ) : (
              <p>
                {models.loading
                  ? "Loading the selected model assignment…"
                  : "Ideation assignment unavailable. Check Models before generating."}
              </p>
            )}
            <p className="metadata">
              Assignment from the selected configuration; availability has not
              been probed.
            </p>
          </div>
        </details>
        <ErrorNotice error={error} />
        {conflictId && (
          <Link to={`/experiments/${encodeURIComponent(conflictId)}`}>
            Open the recorded active job{" "}
            <ArrowRight size={16} aria-hidden="true" />
          </Link>
        )}
        {!jobId && (
          <div className="actions">
            <button
              className="button primary"
              type="submit"
              disabled={submitting || !question.trim() || !roleConfigId}
            >
              <Sparkles size={18} aria-hidden="true" />
              {submitting
                ? "Submitting request…"
                : pending
                  ? "Retry submitted request"
                  : "Generate proposals"}
            </button>
            {pending && !submitting && (
              <button
                type="button"
                className="button secondary"
                onClick={newRequest}
              >
                Edit as a new request
              </button>
            )}
          </div>
        )}
        {pending && !jobId && (
          <p className="metadata">
            Retry uses the same saved request ID and inputs; it cannot create a
            duplicate job.
          </p>
        )}
      </form>
      {jobId && (
        <section className="stack" aria-label="Proposal generation">
          <JobMonitor jobId={jobId} onUpdate={onUpdate} />
          {finished && (
            <div className="card stack" aria-live="polite">
              <h2>
                {finalized === 0
                  ? "No proposals were created"
                  : `${finalized} ${finalized === 1 ? "proposal" : "proposals"} created`}
              </h2>
              <p>
                {attempted !== null
                  ? `${finalized} finalized / ${attempted} requested attempts.`
                  : `${finalized} finalized proposals. Attempt count is unavailable.`}{" "}
                {job?.state === "partial"
                  ? "Some attempts did not finish successfully. Saved proposals remain usable."
                  : ""}
              </p>
              {job?.error && <p>{job.error.message}</p>}
              <p className="metadata">
                Open Technical details in the generation monitor for recorded
                errors.
              </p>
              <div className="actions">
                <button
                  type="button"
                  className="button primary"
                  onClick={newRequest}
                >
                  {job?.state === "completed"
                    ? "New generation request"
                    : "Try again"}
                </button>
              </div>
              <p className="metadata">
                This opens an editable new request. Work starts only when you
                select Generate proposals.
              </p>
            </div>
          )}
        </section>
      )}
      <section className="stack" aria-labelledby="saved-proposals">
        <div className="section-heading">
          <h2 id="saved-proposals">Saved proposals</h2>
          <span className="metadata">
            {ideas.data ? `${ideas.data.ideas.length} saved` : ""}
          </span>
        </div>
        <ErrorNotice error={ideas.error} />
        {ideas.loading && !ideas.data && (
          <p role="status">Loading saved proposals…</p>
        )}
        {ideas.data?.ideas.length === 0 && (
          <div className="empty-state">
            <Plus size={26} aria-hidden="true" />
            <h3>Your proposals will appear here</h3>
            <p>
              Generate proposals above, then edit and save one before preparing
              an experiment.
            </p>
          </div>
        )}
        <div className="card-grid proposal-grid">
          {ideas.data?.ideas.map((record, index) => (
            <article
              className={`card stack proposal-card proposal-color-${index % 3}`}
              key={record.id}
            >
              <div className="metadata">
                Saved proposal · Revision {record.revision}
              </div>
              <h3>{preview(record.idea.Title) || "Untitled proposal"}</h3>
              <p className="proposal-excerpt">
                <strong>Hypothesis</strong>
                <br />
                {preview(record.idea["Short Hypothesis"]) ||
                  "No hypothesis saved"}
              </p>
              <p className="proposal-excerpt">
                <strong>Experiment plan</strong>
                <br />
                {preview(record.idea.Experiments) || "No experiment plan saved"}
              </p>
              <Link
                className="button secondary"
                to={`/ideas/${encodeURIComponent(record.id)}`}
              >
                Edit proposal <ArrowRight size={16} aria-hidden="true" />
              </Link>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
