import type { ReactNode } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  CircleHelp,
  Clock3,
  OctagonX,
  Square,
  TriangleAlert,
} from "lucide-react";
import { Link } from "react-router-dom";
import { ApiError, errorMessage, type JobState, type Json } from "./api";
interface RoleHelp {
  title: string;
  description: string;
  usageNote?: string;
}

export const ROLE_HELP: Record<string, RoleHelp> = {
  ideation: {
    title: "Generate research ideas",
    description: "Turns your research question and constraints into a proposal, then refines and finalizes it across generation rounds.",
  },
  experiment_code: {
    title: "Write and debug experiment code",
    description: "Designs, writes, improves, and debugs Python experiments, including tuning, ablations, and supporting analysis code.",
  },
  experiment_feedback: {
    title: "Evaluate runs and plan next steps",
    description: "Reviews execution outputs and bugs, extracts metrics, and decides stage completion and next substage goals.",
  },
  tree_scoring: {
    title: "Select the best experiment",
    description: "Compares recorded results and selects the best implementation to retain or carry into later experiment stages.",
    usageNote: "Used when best-result selection needs an LLM comparison, not for every tree-search decision.",
  },
  findings_synthesis: {
    title: "Summarize research findings",
    description: "Combines experiment results into summaries of individual branches and the overall research findings for reporting and paper writing.",
  },
  visual_feedback: {
    title: "Interpret experiment figures",
    description: "Examines generated plots and figures to assess experiment results and provide visual feedback during the search.",
  },
  writeup: {
    title: "Write and revise the paper",
    description: "Drafts and revises the LaTeX manuscript using the research idea, experiment results, citations, and figure feedback.",
  },
  review: {
    title: "Review the paper and its figures",
    description: "Assesses the finished paper's scientific quality and checks figures against their captions and discussion.",
  },
  plot_generation: {
    title: "Create combined result plots",
    description: "Writes and refines Python code that turns saved experiment results into final paper figures.",
  },
  citation: {
    title: "Find and prepare citations",
    description: "Chooses literature searches and relevant papers for the paper's bibliography.",
  },
  writeup_small: {
    title: "Describe and check paper figures",
    description: "Describes figures, checks captions, duplicates, and figure selection, and gathers citations when needed.",
    usageNote: "Despite its name, this role handles figure-related work in the default workflow. Manuscript revisions use writeup.",
  },
};

export const CUSTOM_ROLE_HELP: RoleHelp = {
  title: "Custom role",
  description: "No built-in description is available. This role's purpose is defined by the code or configuration that selects it.",
};

const labels: Record<JobState, string> = {
  starting: "Starting",
  running: "Running",
  stopping: "Stopping",
  stopped: "Stopped",
  completed: "Completed",
  partial: "Partial",
  failed: "Failed",
  interrupted: "Interrupted",
  unavailable: "Status unavailable · Saved outputs",
};
export function Status({ state, label }: { state: JobState; label?: string }) {
  const Icon =
    state === "completed"
      ? CheckCircle2
      : state === "failed"
        ? OctagonX
        : state === "partial" || state === "interrupted"
          ? TriangleAlert
          : state === "unavailable"
            ? CircleHelp
            : state === "stopped"
              ? Square
              : state === "starting" || state === "stopping"
                ? Clock3
                : Circle;
  return (
    <span className={`badge status-${state}`}>
      <Icon size={16} aria-hidden="true" />
      {label || labels[state]}
    </span>
  );
}
export function ErrorNotice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="error-notice" role="alert">
      <AlertCircle size={20} aria-hidden="true" />
      <div>
        <p>{errorMessage(error)}</p>
        {error instanceof ApiError && error.detail.blockers && (
          <ul>
            {error.detail.blockers.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
        {error instanceof ApiError && error.detail.job_id && (
          <Link to={`/experiments/${error.detail.job_id}`}>
            Open active job
          </Link>
        )}
      </div>
    </div>
  );
}
export function PageHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>
      {children}
    </header>
  );
}
export function JsonText({ value }: { value: Json }) {
  if (value === null) return <span className="muted">Not provided</span>;
  if (Array.isArray(value))
    return (
      <ul className="prose">
        {value.map((item, index) => (
          <li key={index}>
            <JsonText value={item} />
          </li>
        ))}
      </ul>
    );
  if (typeof value === "object")
    return (
      <dl className="key-value">
        {Object.entries(value).map(([key, item]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <JsonText value={item} />
            </dd>
          </div>
        ))}
      </dl>
    );
  return <span className="prose">{String(value)}</span>;
}
