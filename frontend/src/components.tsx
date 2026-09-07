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
import { useStudio } from "./studio";

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
export function ConfigSelect({
  id = "role-config",
  label = "Role configuration",
  disabled,
  onBeforeChange,
}: {
  id?: string;
  label?: string;
  disabled?: boolean;
  onBeforeChange?: (nextId: string) => boolean;
}) {
  const { bootstrap, roleConfigId, setRoleConfigId } = useStudio();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={roleConfigId}
        disabled={disabled}
        onChange={(event) => {
          const nextId = event.target.value;
          if (onBeforeChange && !onBeforeChange(nextId)) {
            event.target.value = roleConfigId;
            return;
          }
          setRoleConfigId(nextId);
        }}
      >
        {bootstrap.role_configs.map((config) => (
          <option key={config.id} value={config.id}>
            {config.label}
          </option>
        ))}
      </select>
    </div>
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
