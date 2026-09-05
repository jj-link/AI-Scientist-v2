import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  Circle,
  Clock3,
  Download,
  ExternalLink,
  LoaderCircle,
  Square,
} from "lucide-react";
import {
  artifactUrl,
  errorMessage,
  isActive,
  mutate,
  request,
  useApi,
  type Job,
  type JobEvent,
  type RunDetail,
} from "./api";
import { ErrorNotice, Status } from "./components";
import { useStudio } from "./studio";

const phases = [
  ["preparing", "Preparing"],
  ["experiments", "Experiments"],
  ["figures", "Figures"],
  ["citations", "Citations"],
  ["paper", "Paper"],
  ["reviews", "Reviews"],
];
const stages = [
  "Initial implementation",
  "Baseline tuning",
  "Research experiments",
  "Ablation studies",
];
const eventLabels: Record<string, string> = {
  reserved: "Starting worker",
  started: "Worker started",
  attempt_started: "Generating proposal",
  round_started: "Generating proposal",
  search_started: "Searching related work",
  call_finished: "Model or search call returned",
  proposal_finalized: "Proposal saved",
  attempt_failed: "Proposal attempt failed",
  phase_started: "Phase started",
  phase_finished: "Phase finished",
  stage_started: "Stage started",
  stage_finished: "Stage finished",
  step_saved: "Experiment step saved",
  writeup_attempt: "Writing paper",
  stopped: "Job stopped",
  stopping: "Stop requested",
  failed: "Job failed",
  interrupted: "Worker interrupted",
  completed: "Selected work finished",
  partial: "Usable proposals saved; some attempts failed",
};

export default function JobMonitor({
  jobId,
  compact = false,
  onUpdate,
}: {
  jobId: string;
  compact?: boolean;
  onUpdate?: (job: Job) => void;
}) {
  const { refreshBootstrap } = useStudio();
  const [job, setJob] = useState<Job>();
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [connectionError, setConnectionError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [stopping, setStopping] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  const [log, setLog] = useState<string>();
  const [logError, setLogError] = useState<unknown>(null);
  const [outputsOpen, setOutputsOpen] = useState(false);
  const callback = useRef(onUpdate);
  callback.current = onUpdate;
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    let sequence = 0;
    let pending = false;
    let finished = false;
    setJob(undefined);
    setEvents([]);
    setConnectionError(null);
    setLogOpen(false);
    setLog(undefined);
    const poll = async () => {
      if (document.hidden || pending || finished) return;
      pending = true;
      try {
        const [next, page] = await Promise.all([
          request<Job>(`/api/jobs/${jobId}`, { signal: controller.signal }),
          request<{ events: JobEvent[]; last_sequence: number }>(
            `/api/jobs/${jobId}/events?after=${sequence}`,
            { signal: controller.signal },
          ),
        ]);
        if (controller.signal.aborted) return;
        setJob(next);
        setConnectionError(null);
        callback.current?.(next);
        if (page.events.length) {
          setEvents((current) => [...current, ...page.events]);
          sequence = page.last_sequence;
        }
        finished = !isActive(next.state) && page.events.length < 500;
      } catch (error) {
        if (!controller.signal.aborted) setConnectionError(error);
      } finally {
        pending = false;
        if (!controller.signal.aborted && !finished)
          timer = window.setTimeout(poll, 2000);
      }
    };
    const visible = () => {
      if (!document.hidden) {
        window.clearTimeout(timer);
        void poll();
      }
    };
    void poll();
    document.addEventListener("visibilitychange", visible);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [jobId]);
  useEffect(() => {
    if (!logOpen) return;
    const controller = new AbortController();
    setLogError(null);
    fetch(`/api/jobs/${jobId}/log`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok)
          throw new Error("Technical log is not available yet.");
        const text = await response.text();
        if (!controller.signal.aborted) setLog(text);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLogError(error);
      });
    return () => controller.abort();
  }, [logOpen, jobId]);
  const outputs = useApi<RunDetail>(
    outputsOpen && job?.run_id ? `/api/runs/${job.run_id}` : null,
    job && isActive(job.state) ? 2000 : 0,
  );
  const stop = async () => {
    if (
      stopping ||
      !window.confirm(
        `Stop this ${job?.kind === "idea" ? "generation" : "experiment"}? Saved proposals and outputs will be retained. There is no computational resume.`,
      )
    )
      return;
    setStopping(true);
    setActionError(null);
    try {
      setJob(await mutate<Job>(`/api/jobs/${jobId}/stop`));
      refreshBootstrap();
    } catch (error) {
      setActionError(error);
    } finally {
      setStopping(false);
    }
  };
  if (!job)
    return (
      <section className="card">
        <p role="status">
          {connectionError
            ? "Connection lost—reconnecting."
            : "Connecting to job…"}
        </p>
      </section>
    );
  const latest = events.at(-1);
  const saved = [...events]
    .reverse()
    .find((event) => event.type === "step_saved");
  const stage = [...events]
    .reverse()
    .find((event) => typeof event.data.stage === "number");
  const call = [...events]
    .reverse()
    .find((event) =>
      ["round_started", "search_started", "call_finished"].includes(event.type),
    );
  const calling =
    isActive(job.state) &&
    job.state !== "stopping" &&
    call &&
    call.type !== "call_finished";
  const seconds = Math.max(
    0,
    Math.floor(
      ((job.finished_at ? Date.parse(job.finished_at) : Date.now()) -
        Date.parse(job.started_at || job.created_at)) /
        1000,
    ),
  );
  const elapsed =
    seconds >= 3600
      ? `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
      : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return (
    <section
      className={`card job-monitor ${compact ? "compact" : ""}`}
      aria-label={
        job.kind === "idea"
          ? "Proposal generation progress"
          : "Experiment progress"
      }
    >
      <div className="row job-header">
        <div>
          <p className="eyebrow">
            {job.kind === "idea" ? "Proposal generation" : "Experiment"}
          </p>
          <h2>{job.title || "Research experiment"}</h2>
          <div className="metadata">
            <Status state={job.state} />
            <span>
              <Clock3 size={15} aria-hidden="true" /> {elapsed} elapsed
            </span>
          </div>
        </div>
        {isActive(job.state) && (
          <button
            className="button danger"
            disabled={stopping || job.state === "stopping"}
            onClick={() => void stop()}
          >
            <Square size={16} aria-hidden="true" />
            {job.state === "stopping"
              ? "Stopping…"
              : job.kind === "idea"
                ? "Stop generation"
                : "Stop experiment"}
          </button>
        )}
      </div>
      {connectionError ? (
        <p className="notice" role="status">
          Connection lost—reconnecting. The last recorded job state is shown.
        </p>
      ) : null}
      <ErrorNotice error={actionError} />
      {job.error && (
        <div className="error-notice" role="alert">
          <div>
            <strong>
              {job.result?.finalized === 0
                ? "No proposals were created"
                : job.error.code === "no_proposals"
                  ? "No proposals were created"
                  : "This job did not finish successfully"}
            </strong>
            <p>{job.error.message}</p>
            <button className="text-button" onClick={() => setLogOpen(true)}>
              Open technical details
            </button>
          </div>
        </div>
      )}
      {job.kind === "experiment" && (
        <ol className="timeline" aria-label="Pipeline phases">
          {phases.map(([key, label]) => {
            const done = events.some(
              (event) => event.phase === key && event.type === "phase_finished",
            );
            const active = job.phase === key && isActive(job.state);
            const Icon = done ? CheckCircle2 : Circle;
            return (
              <li key={key} className={done ? "done" : active ? "current" : ""}>
                <Icon size={20} aria-hidden="true" />
                <span>{label}</span>
                <small>
                  {done
                    ? "Finished"
                    : active
                      ? "In progress"
                      : job.phase === key && !isActive(job.state)
                        ? job.state
                        : "Not recorded"}
                </small>
              </li>
            );
          })}
        </ol>
      )}
      {stage && (
        <div className="stage-progress">
          <p className="eyebrow">
            Experiments · Stage {String(stage.data.stage)}
          </p>
          <h3>
            {stages[Number(stage.data.stage) - 1] ||
              String(stage.data.stage_name || "Experiment stage")}
          </h3>
          <p className="muted">
            Substage: {String(stage.data.substage || "Not reported")}
          </p>
        </div>
      )}
      {saved && (
        <>
          <dl className="counts">
            <div>
              <dt>Saved nodes</dt>
              <dd>{String(saved.data.total_nodes ?? "Not reported")}</dd>
            </div>
            <div>
              <dt>Working nodes</dt>
              <dd>{String(saved.data.good_nodes ?? "Not reported")}</dd>
            </div>
            <div>
              <dt>Error nodes</dt>
              <dd>{String(saved.data.buggy_nodes ?? "Not reported")}</dd>
            </div>
          </dl>
          <p>
            <strong>Reported metric: </strong>
            {String(saved.data.best_metric ?? "Not reported")}
          </p>
          <p className="metadata">
            Node counts include inherited and multi-seed work; they are not a
            completion percentage.
          </p>
        </>
      )}
      <div className="latest-event" role="status">
        {calling ? (
          <LoaderCircle className="call-spinner" size={18} aria-hidden="true" />
        ) : (
          <Circle size={14} aria-hidden="true" />
        )}
        <span>
          {latest
            ? latest.type === "round_started" && Number(latest.data.round) > 1
              ? "Refining proposal"
              : eventLabels[latest.type] || latest.type.replaceAll("_", " ")
            : "Waiting for the first event"}
          {latest?.data.attempt !== undefined &&
            ` · Attempt ${latest.data.attempt}`}
          {latest?.data.round !== undefined && ` · Round ${latest.data.round}`}
          {latest?.data.code === "rounds_exhausted" &&
            " · No proposal finalized within the configured rounds"}
        </span>
      </div>
      {job.kind === "idea" && job.result && (
        <p>
          {String(job.result.finalized ?? 0)} finalized /{" "}
          {String(job.result.attempted ?? "unknown")} attempted
          {job.result.failed_attempts !== undefined
            ? ` · ${job.result.failed_attempts} unsuccessful attempts`
            : ""}
        </p>
      )}
      <p className="metadata">
        Started {new Date(job.started_at || job.created_at).toLocaleString()} ·
        Last update {new Date(job.updated_at).toLocaleString()}
      </p>
      {job.state === "completed" && (
        <p className="notice">
          Completed means the selected work finished. It does not establish
          novelty, confirm a hypothesis, or indicate paper acceptance.
        </p>
      )}
      <div className="actions">
        {compact && (
          <Link className="button secondary" to={`/experiments/${jobId}`}>
            Open job details
          </Link>
        )}
        {job.run_id && (
          <Link className="button secondary" to={`/results/${job.run_id}`}>
            <ExternalLink size={16} aria-hidden="true" />
            Browse saved outputs
          </Link>
        )}
      </div>
      {job.run_id && (
        <details
          className="advanced"
          open={outputsOpen}
          onToggle={(event) => setOutputsOpen(event.currentTarget.open)}
        >
          <summary>Available outputs</summary>
          <ErrorNotice error={outputs.error} />
          {outputs.data ? (
            <>
              <div className="output-strip">
                {outputs.data.papers.map((paper) => (
                  <a
                    key={paper.id}
                    href={artifactUrl(job.run_id!, paper.id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {paper.name}
                  </a>
                ))}
              </div>
              <p>{outputs.data.figures.length} saved figures</p>
              {outputs.data.missing_outputs.length > 0 && (
                <p className="muted">
                  Not available: {outputs.data.missing_outputs.join(", ")}
                </p>
              )}
            </>
          ) : (
            <p>Loading saved outputs…</p>
          )}
        </details>
      )}
      <details
        className="advanced"
        open={logOpen}
        onToggle={(event) => setLogOpen(event.currentTarget.open)}
      >
        <summary>Technical details</summary>
        <p className="muted">
          Recent log preview. May include generated research content; configured
          credentials are redacted.
        </p>
        {logError ? (
          <p role="alert">{errorMessage(logError)}</p>
        ) : (
          <pre className="technical-log">{log ?? "Loading technical log…"}</pre>
        )}
        <a
          className="button secondary"
          href={`/api/jobs/${jobId}/log?download=true`}
        >
          <Download size={16} aria-hidden="true" />
          Download technical log
        </a>
      </details>
    </section>
  );
}
