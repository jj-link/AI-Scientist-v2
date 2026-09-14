import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import CrashAssistant from "./CrashAssistant";
import ExperimentRunActions from "./ExperimentRunActions";
import {
  CheckCircle2,
  Circle,
  Clock3,
  Download,
  ExternalLink,
  Square,
} from "lucide-react";
import {
  artifactUrl,
  errorMessage,
  isActive,
  mutate,
  request,
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

function ElapsedTime({ job }: { job: Job }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!isActive(job.state)) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [job.id, job.state]);
  const seconds = Math.max(
    0,
    Math.floor(
      ((job.finished_at ? Date.parse(job.finished_at) : isActive(job.state) ? now : Date.parse(job.updated_at)) -
        Date.parse(job.started_at || job.created_at)) / 1000,
    ),
  );
  return (
    <span>
      <Clock3 size={15} aria-hidden="true" />{" "}
      {seconds >= 3600
        ? `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
        : `${Math.floor(seconds / 60)}m ${seconds % 60}s`} elapsed
    </span>
  );
}

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
  const [outputs, setOutputs] = useState<RunDetail>();
  const [outputsError, setOutputsError] = useState<unknown>(null);
  const [outputsChecking, setOutputsChecking] = useState(false);
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
    setOutputs(undefined);
    setOutputsError(null);
    setOutputsChecking(false);
    setActionError(null);
    setLogError(null);
    setStopping(false);
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
        let outputsFailed = false;
        if (next.run_id) {
          setOutputsChecking(true);
          try {
            const detail = await request<RunDetail>(`/api/runs/${next.run_id}`, {
              signal: controller.signal,
            });
            if (controller.signal.aborted) return;
            setOutputs(detail);
            setOutputsError(null);
          } catch (error) {
            if (controller.signal.aborted) return;
            outputsFailed = true;
            setOutputsError(error);
          } finally {
            if (!controller.signal.aborted) setOutputsChecking(false);
          }
        }
        finished = !isActive(next.state) && page.events.length < 500 && !outputsFailed;
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
  const workingImages = useMemo(
    () => outputs?.artifacts.filter((artifact) => artifact.kind === "image") ?? [],
    [outputs],
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
  let saved: JobEvent | undefined;
  let stage: JobEvent | undefined;
  let milestone: JobEvent | undefined;
  for (let index = events.length - 1; index >= 0; index--) {
    const event = events[index];
    if (!saved && event.type === "step_saved") saved = event;
    if (!stage && typeof event.data.stage === "number") stage = event;
    if (!milestone && ["step_saved", "proposal_finalized", "phase_finished", "stage_finished"].includes(event.type))
      milestone = event;
    if (saved && stage && milestone) break;
  }
  const active = isActive(job.state);
  const phaseLabel = phases.find(([key]) => key === job.phase)?.[1] ||
    job.phase?.replaceAll("_", " ") || "Awaiting a recorded phase";
  const quietSince = latest?.timestamp || job.started_at || job.created_at;
  const quietMinutes = Math.max(0, Math.floor((Date.now() - Date.parse(quietSince)) / 60000));
  const missingLabel = outputsError || connectionError ? "Availability unknown" :
    outputsChecking ? "Checking availability…" : active ? "Not saved yet" : "Not saved";
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
            <ElapsedTime job={job} />
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
      <ExperimentRunActions key={job.id} job={job} />
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
      <CrashAssistant job={job} onOpenLog={() => setLogOpen(true)} />
      <div className="monitor-focus">
        {job.state === "running" && !connectionError && (
          <div className="monitor-ambient" aria-hidden="true">
            <span /><span /><span />
          </div>
        )}
        <div className="monitor-phase">
          <p className="eyebrow">Last recorded phase</p>
          <h3>{phaseLabel}</h3>
          {stage && job.phase === "experiments" && (
            <p className="muted">
              Stage {String(stage.data.stage)} ·{" "}
              {stages[Number(stage.data.stage) - 1] ||
                String(stage.data.stage_name || "Experiment stage")}
              {stage.data.substage ? ` · ${String(stage.data.substage)}` : ""}
            </p>
          )}
          <p className="monitor-milestone">
            <strong>Last saved milestone</strong>{" "}
            {milestone ? (
              <>
                {eventLabels[milestone.type] || milestone.type.replaceAll("_", " ")}
                {milestone.phase ? ` · ${phases.find(([key]) => key === milestone.phase)?.[1] || milestone.phase}` : ""}
                {" · "}<time dateTime={milestone.timestamp}>{new Date(milestone.timestamp).toLocaleString()}</time>
              </>
            ) : active ? "None recorded yet" : "None recorded"}
          </p>
          {active && quietMinutes >= 2 && (
            <p className="metadata">
              No newer recorded milestone for {quietMinutes} minutes.
              {" "}This feed does not report in-flight model calls or compute activity.
            </p>
          )}
        </div>
      </div>
      {job.kind === "experiment" && (
        <section className="monitor-evidence" aria-label="Saved so far">
          <div className="row">
            <h3>Saved so far</h3>
            {job.run_id && <Link to={`/results/${job.run_id}`}>View in Results <ExternalLink size={14} aria-hidden="true" /></Link>}
          </div>
          {!job.run_id ? (
            <p className="muted">{active ? "Waiting for a saved run to inspect." : "No saved run is linked to this job."}</p>
          ) : !outputs ? (
            <p className="muted" role="status">
              {outputsError || connectionError ? "Saved outputs could not be loaded. Retrying; availability is unknown." : "Checking saved outputs…"}
            </p>
          ) : (
            <>
              {outputsError || connectionError ? <p className="notice" role="status">Could not refresh saved outputs. Showing the last available snapshot; availability may have changed.</p> : null}
              <dl className="monitor-output-counts">
                <div><dt>Paper PDFs</dt><dd>{outputs.papers.length ? `${outputs.papers.length} saved` : missingLabel}</dd></div>
                <div><dt>Final figures</dt><dd>{outputs.figures.length ? `${outputs.figures.length} saved` : missingLabel}</dd></div>
                <div><dt>Working images</dt><dd>{workingImages.length ? `${workingImages.length} saved` : missingLabel}</dd></div>
              </dl>
              {outputs.papers.length > 0 && (
                <div className="monitor-paper-links">
                  {outputs.papers.map((paper) => (
                    <a key={paper.id} href={artifactUrl(job.run_id!, paper.id)} target="_blank" rel="noopener noreferrer" title={`${paper.relative_path} · ${new Date(paper.updated_at).toLocaleString()}`}>
                      {paper.name}
                    </a>
                  ))}
                </div>
              )}
              {workingImages.length > 0 && (
                <div className="monitor-image-strip">
                  {workingImages.slice(0, compact ? 2 : 3).map((image) => (
                    <figure key={image.id}>
                      <a href={artifactUrl(job.run_id!, image.id)} target="_blank" rel="noopener noreferrer" aria-label={`Open working image: ${image.name}`}>
                        <img key={image.updated_at} src={artifactUrl(job.run_id!, image.id)} alt={`Working image: ${image.name}`} loading="lazy" />
                      </a>
                      <figcaption>
                        <strong>{image.name}</strong>
                        <span>{image.relative_path}</span>
                        <time dateTime={image.updated_at}>{new Date(image.updated_at).toLocaleString()}</time>
                      </figcaption>
                    </figure>
                  ))}
                </div>
              )}
              <p className="metadata">
                Working images are intermediate outputs, not final figures. Saved files do not establish a successful result.
                {workingImages.length > (compact ? 2 : 3) ? " See all working images in Results." : ""}
              </p>
            </>
          )}
        </section>
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
                <Icon size={14} aria-hidden="true" />
                <span>{label}</span>
                <small>
                  {done
                    ? "Finished"
                    : active
                      ? "Last recorded"
                      : job.phase === key && !isActive(job.state)
                        ? job.state
                        : "Not recorded"}
                </small>
              </li>
            );
          })}
        </ol>
      )}
      <div className="monitor-latest">
        <strong>Latest recorded event</strong>
        <span>
          {latest
            ? latest.type === "round_started" && Number(latest.data.round) > 1
              ? "Refining proposal"
              : eventLabels[latest.type] || latest.type.replaceAll("_", " ")
            : active ? "Waiting for the first event" : "No events recorded"}
          {latest?.data.attempt !== undefined &&
            ` · Attempt ${latest.data.attempt}`}
          {latest?.data.round !== undefined && ` · Round ${latest.data.round}`}
          {latest?.data.code === "rounds_exhausted" &&
            " · No proposal finalized within the configured rounds"}
          {latest && <> · <time dateTime={latest.timestamp}>{new Date(latest.timestamp).toLocaleString()}</time></>}
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
      <details
        className="advanced"
        open={logOpen}
        onToggle={(event) => setLogOpen(event.currentTarget.open)}
      >
        <summary>Technical details</summary>
        {stage && (
          <p className="muted">
            Last recorded experiment stage: {String(stage.data.stage)} ·{" "}
            {stages[Number(stage.data.stage) - 1] || String(stage.data.stage_name || "Experiment stage")}
            {stage.data.substage ? ` · ${String(stage.data.substage)}` : ""}
          </p>
        )}
        {saved && (
          <>
            <dl className="counts">
              <div><dt>Saved nodes</dt><dd>{String(saved.data.total_nodes ?? "Not reported")}</dd></div>
              <div><dt>Working nodes</dt><dd>{String(saved.data.good_nodes ?? "Not reported")}</dd></div>
              <div><dt>Error nodes</dt><dd>{String(saved.data.buggy_nodes ?? "Not reported")}</dd></div>
            </dl>
            <p><strong>Reported metric: </strong>{String(saved.data.best_metric ?? "Not reported")}</p>
            <p className="metadata">Node counts include inherited and multi-seed work; they are not a completion percentage.</p>
          </>
        )}
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
