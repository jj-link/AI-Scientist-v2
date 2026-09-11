import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowRight, FlaskConical } from "lucide-react";
import { useApi, type Job } from "../api";
import { ErrorNotice, PageHeading, Status } from "../components";
import JobMonitor from "../JobMonitor";
import FailedRunActions from "../FailedRunActions";

export default function Experiments() {
  const { jobId } = useParams();
  const [deletedJobIds, setDeletedJobIds] = useState<Set<string>>(() => new Set());
  const { data, error, loading, refresh } = useApi<{ jobs: Job[] }>(
    jobId ? null : "/api/jobs",
    2000,
  );
  if (jobId)
    return (
      <div className="stack">
        <Link className="back-link" to="/experiments">
          <ArrowLeft size={17} aria-hidden="true" />
          All experiments
        </Link>
        <PageHeading eyebrow="Experiments" title="Follow the recorded work">
          <p>Live progress and saved outputs from the worker on this PC.</p>
        </PageHeading>
        <JobMonitor key={jobId} jobId={jobId} />
      </div>
    );
  const jobs = data?.jobs.filter(
    (job) => job.kind === "experiment" && !deletedJobIds.has(job.id),
  ) || [];
  return (
    <div className="stack">
      <PageHeading eyebrow="Experiments" title="Experiments">
        <p>
          Start with a saved proposal. Follow each phase, then inspect the
          outputs.
        </p>
      </PageHeading>
      <ErrorNotice error={error} />
      {Boolean(error) && (
        <button className="button secondary" onClick={refresh}>
          Reconnect
        </button>
      )}
      {loading && !data && <p role="status">Loading recorded experiments…</p>}
      {data && jobs.length === 0 ? (
        <section className="card empty-state">
          <span className="empty-icon teal">
            <FlaskConical size={32} aria-hidden="true" />
          </span>
          <h2>No experiments started</h2>
          <p>
            Generate a proposal, edit it, then prepare an experiment. Nothing
            runs until you select Start experiment.
          </p>
          <Link className="button primary" to="/ideas">
            Go to Ideas
            <ArrowRight size={18} aria-hidden="true" />
          </Link>
        </section>
      ) : (
        <div className="stack">
          {jobs.map((job) => {
            const seconds = Math.max(
              0,
              Math.floor(
                ((job.finished_at ? Date.parse(job.finished_at) : Date.now()) -
                  Date.parse(job.started_at || job.created_at)) /
                  1000,
              ),
            );
            return (
              <article className="card experiment-card" key={job.id}>
                <div className="row">
                  <Status state={job.state} />
                  <span className="metadata">
                    {Math.floor(seconds / 60)}m {seconds % 60}s elapsed
                  </span>
                </div>
                <h2>
                  <Link to={`/experiments/${job.id}`}>
                    {job.title || "Research experiment"}
                  </Link>
                </h2>
                <p>
                  Current phase:{" "}
                  <strong>
                    {job.phase
                      ? job.phase[0].toUpperCase() + job.phase.slice(1)
                      : "Not recorded"}
                  </strong>
                </p>
                <p className="metadata">
                  Started{" "}
                  {new Date(job.started_at || job.created_at).toLocaleString()}
                </p>
                <div className="actions">
                  <Link
                    className="button secondary"
                    to={`/experiments/${job.id}`}
                  >
                    Open experiment
                    <ArrowRight size={16} aria-hidden="true" />
                  </Link>
                  {job.run_id && (
                    <Link to={`/results/${job.run_id}`}>Saved outputs</Link>
                  )}
                </div>
                <FailedRunActions
                  job={job}
                  onDeleted={() => {
                    setDeletedJobIds((current) => new Set(current).add(job.id));
                    refresh();
                  }}
                />
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
