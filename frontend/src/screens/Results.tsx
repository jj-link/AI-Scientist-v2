import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Download,
  ExternalLink,
  FileText,
  FolderOpen,
  Images,
  MessageSquare,
  RefreshCw,
  X,
} from "lucide-react";
import {
  artifactUrl,
  useApi,
  type Artifact,
  type Json,
  type Review,
  type Run,
  type RunDetail,
} from "../api";
import { ErrorNotice, JsonText, PageHeading, Status } from "../components";
import "./results.css";

function savedTime(value: string | null) {
  if (!value) return "Not recorded";
  const time = new Date(value);
  return Number.isNaN(time.valueOf()) ? value : time.toLocaleString();
}

function DownloadLink({
  runId,
  artifact,
  label,
}: {
  runId: string;
  artifact: Artifact;
  label?: string;
}) {
  return (
    <a
      className="results-download"
      href={artifactUrl(runId, artifact.id, true)}
      download={artifact.name}
    >
      <Download size={16} aria-hidden="true" />
      <span>{label || artifact.relative_path}</span>
    </a>
  );
}

function SourceList({
  runId,
  artifacts,
}: {
  runId: string;
  artifacts: Artifact[];
}) {
  return (
    <ul className="results-sources">
      {artifacts.map((artifact) => (
        <li key={artifact.id}>
          <DownloadLink runId={runId} artifact={artifact} />
          {artifact.kind === "tree_download" && (
            <span className="metadata">HTML · Download only</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function ResultsIndex() {
  const { data, error, loading, refresh } = useApi<{ runs: Run[] }>(
    "/api/runs",
  );
  return (
    <>
      <PageHeading eyebrow="Saved research" title="Results">
        <p>
          Browse papers, figures, reviews, and experiment records. Outputs
          remain available when a run fails or stops.
        </p>
      </PageHeading>
      <div className="toolbar">
        <p className="muted">
          Historical runs have saved outputs, not a verified execution status.
        </p>
        <button
          className="button secondary"
          onClick={refresh}
          disabled={loading}
        >
          <RefreshCw size={16} aria-hidden="true" />
          Refresh results
        </button>
      </div>
      <ErrorNotice error={error} />
      {loading && !data && <p role="status">Loading saved results…</p>}
      {data?.runs.length === 0 && (
        <div className="card empty-state">
          <FolderOpen size={32} aria-hidden="true" />
          <h2>No saved runs yet</h2>
          <p>
            Prepare an experiment from a saved proposal to create a new run.
          </p>
          <Link className="button primary" to="/ideas">
            Go to Ideas
          </Link>
        </div>
      )}
      <div className="card-grid results-index">
        {data?.runs.map((run) => (
          <article className="card results-run" key={run.id}>
            <Status
              state={run.historical ? "unavailable" : run.state}
              label={
                run.historical
                  ? "Status unavailable · Saved outputs"
                  : run.status_label
              }
            />
            <h2>
              <Link to={`/results/${encodeURIComponent(run.id)}`}>
                {run.title}
              </Link>
            </h2>
            <p className="results-hypothesis">
              {run.hypothesis || "No saved hypothesis is available."}
            </p>
            <dl className="metadata results-record">
              <dt>Saved directory</dt>
              <dd>{run.directory}</dd>
              <dt>Last update</dt>
              <dd>{savedTime(run.updated_at)}</dd>
            </dl>
            {!run.outputs_available && (
              <p className="muted">No outputs are currently available.</p>
            )}
            <Link
              className="button secondary"
              to={`/results/${encodeURIComponent(run.id)}`}
            >
              View outputs
              <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </article>
        ))}
      </div>
    </>
  );
}

function PaperPanel({ run }: { run: RunDetail }) {
  const [chosenId, setChosenId] = useState<string | null>(null);
  const chosen = run.papers.find(
    (paper) => paper.id === (chosenId || run.default_paper_id),
  );
  if (!run.papers.length)
    return (
      <div className="card empty-state">
        <h2>Paper not available</h2>
        <p>
          No saved PDF was found in this run. Other available outputs can still
          be viewed.
        </p>
      </div>
    );
  return (
    <section className="card stack" aria-labelledby="paper-heading">
      <div>
        <h2 id="paper-heading">Paper</h2>
        <p className="muted">
          {run.historical
            ? "The default is an available saved document, not a verification of finality or acceptance."
            : "The pipeline-recorded PDF is selected when available. Other saved PDFs are listed below."}
        </p>
      </div>
      <label className="field">
        Saved PDF
        <select
          value={chosen?.id || ""}
          onChange={(event) => setChosenId(event.target.value)}
        >
          {!chosen && (
            <option value="" disabled>
              Select an available PDF
            </option>
          )}
          {run.papers.map((paper) => (
            <option key={paper.id} value={paper.id}>
              {paper.relative_path}
              {paper.id === run.default_paper_id ? " — Default document" : ""}
            </option>
          ))}
        </select>
      </label>
      {chosen && (
        <>
          <div className="actions">
            <a
              className="button primary"
              href={artifactUrl(run.id, chosen.id)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={16} aria-hidden="true" />
              Open paper
            </a>
            <DownloadLink
              runId={run.id}
              artifact={chosen}
              label="Download selected PDF"
            />
          </div>
          <p className="metadata">
            If the embedded preview is unavailable, use Open paper or download
            the PDF.
          </p>
          <iframe
            className="results-pdf"
            src={artifactUrl(run.id, chosen.id)}
            title={`PDF preview: ${chosen.name}`}
          />
        </>
      )}
      <details className="advanced">
        <summary>All saved PDFs ({run.papers.length})</summary>
        <SourceList runId={run.id} artifacts={run.papers} />
      </details>
    </section>
  );
}

function FigureDialog({
  runId,
  figures,
  selectedId,
  onSelect,
  onClose,
}: {
  runId: string;
  figures: Artifact[];
  selectedId: string;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const index = figures.findIndex((figure) => figure.id === selectedId);
  const figure = figures[index];
  const move = useCallback(
    (direction: number) => {
      if (figures.length)
        onSelect(
          figures[
            (Math.max(0, index) + direction + figures.length) % figures.length
          ].id,
        );
    },
    [figures, index, onSelect],
  );
  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    const keepFocus = (event: FocusEvent) => {
      if (
        event.target instanceof Node &&
        !dialog.current?.contains(event.target)
      )
        closeButton.current?.focus();
    };
    document.addEventListener("focusin", keepFocus);
    return () => {
      document.removeEventListener("focusin", keepFocus);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        move(-1);
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        move(1);
      }
      if (event.key === "Tab") {
        const elements = dialog.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex="0"]',
        );
        if (!elements?.length) return;
        const first = elements[0],
          last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [move, onClose]);
  return createPortal(
    <div
      className="modal-backdrop results-lightbox-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="figure-dialog-title"
        aria-describedby="figure-dialog-help"
        className="modal results-lightbox"
      >
        <div className="card-header">
          <h2 id="figure-dialog-title">
            {figure?.name || "Figure no longer available"}
          </h2>
          <button
            ref={closeButton}
            className="button secondary"
            onClick={onClose}
          >
            <X size={18} aria-hidden="true" />
            Close
          </button>
        </div>
        <p id="figure-dialog-help" className="metadata">
          Use Left and Right arrows to browse. Escape closes the enlarged view.
        </p>
        {figure ? (
          <img
            className="results-enlarged"
            src={artifactUrl(runId, figure.id)}
            alt={figure.name}
          />
        ) : (
          <p>The saved figure is no longer in the current manifest.</p>
        )}
        <div className="results-lightbox-actions">
          <button
            className="button secondary"
            onClick={() => move(-1)}
            disabled={figures.length < 2}
          >
            <ArrowLeft size={16} aria-hidden="true" />
            Previous
          </button>
          <span role="status">
            {index >= 0
              ? `${index + 1} of ${figures.length}`
              : `${figures.length} figures`}
          </span>
          <button
            className="button secondary"
            onClick={() => move(1)}
            disabled={figures.length < 2}
          >
            Next
            <ArrowRight size={16} aria-hidden="true" />
          </button>
          {figure && (
            <DownloadLink
              runId={runId}
              artifact={figure}
              label="Download figure"
            />
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function FiguresPanel({ run }: { run: RunDetail }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const close = useCallback(() => setSelectedId(null), []);
  return (
    <section className="stack" aria-labelledby="figures-heading">
      <div>
        <h2 id="figures-heading">
          Figures <span className="badge">{run.figures.length}</span>
        </h2>
        <p className="muted">
          All saved figures in the artifact directory. They do not necessarily
          all appear in the paper.
        </p>
      </div>
      {!run.figures.length && (
        <div className="card empty-state">
          <h3>Figures not available</h3>
          <p>No saved PNG or JPEG figures were found.</p>
        </div>
      )}
      <div className="figure-grid">
        {run.figures.map((figure) => (
          <figure className="figure-card results-figure" key={figure.id}>
            <button
              className="results-figure-open"
              onClick={() => setSelectedId(figure.id)}
              aria-label={`Enlarge ${figure.name}`}
            >
              <img
                src={artifactUrl(run.id, figure.id)}
                alt={figure.name}
                loading="lazy"
              />
            </button>
            <figcaption>
              <span>{figure.name}</span>
              <DownloadLink
                runId={run.id}
                artifact={figure}
                label="Download figure"
              />
            </figcaption>
          </figure>
        ))}
      </div>
      {selectedId && (
        <FigureDialog
          runId={run.id}
          figures={run.figures}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onClose={close}
        />
      )}
    </section>
  );
}

function ReviewCard({ runId, review }: { runId: string; review: Review }) {
  return (
    <article className="card stack results-review">
      <div className="card-header">
        <div>
          <p className="eyebrow">AI-generated review</p>
          <h3>{review.label}</h3>
        </div>
        {review.artifact_id && (
          <a
            className="results-download"
            href={artifactUrl(runId, review.artifact_id, true)}
            download
          >
            <Download size={16} aria-hidden="true" />
            Download original review
          </a>
        )}
      </div>
      {review.error || review.data === null ? (
        <p className="notice">
          {review.error ||
            "The saved review is empty or unavailable. Download the original review if available."}
        </p>
      ) : Object.entries(review.data).length === 0 ? (
        <p className="notice">
          The saved review contains no fields. The original remains
          downloadable.
        </p>
      ) : (
        Object.entries(review.data).map(([label, value]) => (
          <section key={label} className="results-review-field">
            <h4>{label}</h4>
            <JsonText value={value} />
          </section>
        ))
      )}
    </article>
  );
}

function ReviewsPanel({ run }: { run: RunDetail }) {
  return (
    <section className="stack" aria-labelledby="reviews-heading">
      <div>
        <h2 id="reviews-heading">Reviews</h2>
        <p className="muted">
          AI-generated assessments, not peer review or evidence of acceptance.
          Ratings and decisions below are the saved values.
        </p>
      </div>
      {run.reviews.length ? (
        run.reviews.map((review) => (
          <ReviewCard key={review.kind} runId={run.id} review={review} />
        ))
      ) : (
        <div className="card empty-state">
          <h3>Reviews not available</h3>
          <p>No saved paper or figure review was found.</p>
        </div>
      )}
    </section>
  );
}

const progressLabels: Record<string, string> = {
  total_nodes: "Saved nodes",
  good_nodes: "Working nodes",
  buggy_nodes: "Error nodes",
  best_metric: "Reported metric",
  current_findings: "Generated findings",
};
function text(value: Json | undefined, fallback: string) {
  return typeof value === "string" ? value : fallback;
}

function DetailsPanel({ run }: { run: RunDetail }) {
  const sources = run.artifacts.filter(
    (artifact) =>
      !["paper", "figure", "image", "review"].includes(artifact.kind),
  );
  const groupedStagePaths = new Set(
    run.log_directories.map((directory) => directory.path),
  );
  const orphanStages = run.stages.filter(
    (stage) => !groupedStagePaths.has(text(stage.log_directory, "")),
  );
  const stageCard = (stage: Record<string, Json>, index: number) => {
    const progress =
      stage.progress &&
      typeof stage.progress === "object" &&
      !Array.isArray(stage.progress)
        ? stage.progress
        : {};
    const ids = new Set(
      Array.isArray(stage.artifact_ids)
        ? stage.artifact_ids.filter(
            (id): id is string => typeof id === "string",
          )
        : [],
    );
    const stageSources = sources.filter((artifact) => ids.has(artifact.id));
    return (
      <article
        className="results-stage"
        key={text(stage.directory, String(index))}
      >
        <h4>{text(stage.label, "Saved stage")}</h4>
        <p className="metadata">
          Substage: {text(stage.substage, "Not recorded")}
        </p>
        <p className="metadata results-path">{text(stage.directory, "")}</p>
        <p className="metadata">
          Last saved: {savedTime(text(stage.updated_at, ""))}
        </p>
        {typeof stage.error === "string" && (
          <p className="notice">
            {stage.error} Saved data may be temporarily unavailable while it is
            being written.
          </p>
        )}
        {Object.keys(progress).length ? (
          <dl className="results-progress">
            {Object.entries(progress).map(([key, value]) => (
              <div key={key}>
                <dt>{progressLabels[key] || key}</dt>
                <dd>
                  <JsonText value={value} />
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="muted">
            No readable saved progress summary for this stage.
          </p>
        )}
        {typeof stage.artifact_id === "string" && (
          <a
            className="results-download"
            href={artifactUrl(run.id, stage.artifact_id, true)}
            download
          >
            <Download size={16} aria-hidden="true" />
            Download findings source
          </a>
        )}
        {stageSources.length > 0 && (
          <details className="advanced">
            <summary>Stage source files ({stageSources.length})</summary>
            <SourceList runId={run.id} artifacts={stageSources} />
          </details>
        )}
      </article>
    );
  };
  return (
    <section className="stack" aria-labelledby="details-heading">
      <div>
        <h2 id="details-heading">Experiment details</h2>
        <p className="muted">
          Saved records are grouped by actual log directory and stage/substage.
          Their presence does not mean a stage completed. Node counts are not a
          completion percentage; metrics retain their saved labels and are not
          compared across stages.
        </p>
      </div>
      <div className="card">
        <dl className="results-record">
          <dt>Output directory</dt>
          <dd>{run.directory}</dd>
          <dt>Started</dt>
          <dd>{savedTime(run.started_at)}</dd>
          <dt>Finished</dt>
          <dd>{savedTime(run.finished_at)}</dd>
        </dl>
        {run.job_id && (
          <Link
            className="button secondary"
            to={`/experiments/${encodeURIComponent(run.job_id)}`}
          >
            Open experiment monitor
          </Link>
        )}
        {run.idea_error && (
          <p className="notice">Saved proposal: {run.idea_error}</p>
        )}
        {run.idea && (
          <details className="advanced">
            <summary>Saved proposal snapshot</summary>
            <JsonText value={run.idea} />
          </details>
        )}
      </div>
      {run.log_directories.map((directory) => {
        const stages = run.stages.filter(
          (stage) => stage.log_directory === directory.path,
        );
        const ids = new Set(directory.artifact_ids);
        return (
          <section className="card stack" key={directory.path}>
            <h3 className="results-path">{directory.path}</h3>
            {stages.length ? (
              stages.map(stageCard)
            ) : (
              <p className="muted">
                No saved stage progress was found in this log directory.
              </p>
            )}
            <details className="advanced">
              <summary>Log-directory source files</summary>
              <SourceList
                runId={run.id}
                artifacts={sources.filter((artifact) => ids.has(artifact.id))}
              />
            </details>
          </section>
        );
      })}
      {orphanStages.length > 0 && (
        <section className="card stack">
          <h3>Other saved stages</h3>
          {orphanStages.map(stageCard)}
        </section>
      )}
      {!run.log_directories.length && !run.stages.length && (
        <div className="card empty-state">
          <h3>Stage records not available</h3>
          <p>No readable saved stage records or log directories were found.</p>
        </div>
      )}
      {run.summaries.length > 0 && (
        <section className="card stack">
          <h3>Saved summaries and findings</h3>
          {run.summaries.map((summary) => (
            <details className="advanced" key={summary.artifact_id}>
              <summary>
                {summary.log_directory ? `${summary.log_directory} / ` : ""}
                {summary.name}
              </summary>
              {summary.error ? (
                <p className="notice">
                  {summary.error} This file will be checked again on refresh.
                </p>
              ) : summary.data === null ? (
                <p>No saved summary content is available.</p>
              ) : (
                <JsonText value={summary.data} />
              )}
              <a
                className="results-download"
                href={artifactUrl(run.id, summary.artifact_id, true)}
                download
              >
                <Download size={16} aria-hidden="true" />
                Download summary source
              </a>
            </details>
          ))}
        </section>
      )}
      <section className="card">
        <h3>Source downloads</h3>
        <p className="muted">
          Journals and tree data are downloaded only when requested. Tree HTML
          is download-only and never rendered here. Pickle and NumPy files are
          not loaded by Studio.
        </p>
        {sources.length ? (
          <details className="advanced">
            <summary>Browse all source files ({sources.length})</summary>
            <SourceList runId={run.id} artifacts={sources} />
          </details>
        ) : (
          <p>No source downloads are currently available.</p>
        )}
      </section>
    </section>
  );
}

const panels = [
  { id: "paper", label: "Open paper", icon: FileText },
  { id: "figures", label: "Figures", icon: Images },
  { id: "reviews", label: "Reviews", icon: MessageSquare },
  { id: "details", label: "Experiment details", icon: FolderOpen },
];
function ResultDetail({ runId }: { runId: string }) {
  const [search, setSearch] = useSearchParams();
  const requestedPanel = search.get("panel");
  const panel = panels.some((item) => item.id === requestedPanel)
    ? requestedPanel
    : "paper";
  const {
    data: run,
    error,
    loading,
    refresh,
  } = useApi<RunDetail>(`/api/runs/${encodeURIComponent(runId)}`, 2000);
  return (
    <>
      <Link className="results-back" to="/results">
        <ArrowLeft size={16} aria-hidden="true" />
        All results
      </Link>
      <ErrorNotice error={error} />
      {error != null && (
        <div className="notice">
          <p role="status">
            Connection lost or outputs unavailable. Reconnecting while this page
            is visible; the last loaded outputs remain below.
          </p>
          <button className="button secondary" onClick={refresh}>
            Retry now
          </button>
        </div>
      )}
      {loading && !run && <p role="status">Loading saved outputs…</p>}
      {run && (
        <>
          <PageHeading eyebrow="Saved outputs" title={run.title}>
            <p className="results-hypothesis">
              {run.hypothesis || "No saved hypothesis is available."}
            </p>
          </PageHeading>
          <div className="toolbar">
            <Status
              state={run.historical ? "unavailable" : run.state}
              label={
                run.historical
                  ? "Status unavailable · Saved outputs"
                  : run.status_label
              }
            />
            <button className="button secondary" onClick={refresh}>
              <RefreshCw size={16} aria-hidden="true" />
              Refresh outputs
            </button>
          </div>
          {run.historical && (
            <p className="notice">
              Historical run · Read-only. Saved artifacts do not establish
              whether the original pipeline completed.
            </p>
          )}
          {run.missing_outputs.length > 0 && (
            <p className="muted">
              Not available: {run.missing_outputs.join(", ")}.
            </p>
          )}
          <nav
            className="output-strip results-panels"
            aria-label="Result outputs"
          >
            {panels.map((item) => (
              <button
                key={item.id}
                className={`button ${panel === item.id ? "primary" : "secondary"}`}
                aria-pressed={panel === item.id}
                onClick={() => {
                  const next = new URLSearchParams(search);
                  next.set("panel", item.id);
                  setSearch(next);
                }}
              >
                <item.icon size={18} aria-hidden="true" />
                {item.label}
              </button>
            ))}
          </nav>
          <div className="results-panel">
            {panel === "paper" && <PaperPanel run={run} />}
            {panel === "figures" && <FiguresPanel run={run} />}
            {panel === "reviews" && <ReviewsPanel run={run} />}
            {panel === "details" && <DetailsPanel run={run} />}
          </div>
        </>
      )}
    </>
  );
}

export default function Results() {
  const { runId } = useParams<{ runId: string }>();
  return runId ? <ResultDetail key={runId} runId={runId} /> : <ResultsIndex />;
}
