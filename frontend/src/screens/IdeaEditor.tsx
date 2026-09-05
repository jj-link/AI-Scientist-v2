import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useBlocker, useParams } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Download,
  Plus,
  Save,
  Trash2,
} from "lucide-react";
import {
  ApiError,
  mutate,
  useApi,
  type Idea,
  type IdeaRecord,
  type Json,
} from "../api";
import { ErrorNotice, JsonText, PageHeading } from "../components";
import "./ideas.css";

function validate(idea: Idea): Record<string, string> {
  const errors: Record<string, string> = {};
  if (
    typeof idea.Name !== "string" ||
    !/^[a-z][a-z0-9_]{0,63}$/.test(idea.Name)
  )
    errors.Name =
      "Use 1–64 lowercase letters, digits or underscores, starting with a letter.";
  for (const key of ["Title", "Short Hypothesis", "Abstract"])
    if (typeof idea[key] !== "string" || !(idea[key] as string).trim())
      errors[key] = "Enter nonempty text.";
  if (
    !Array.isArray(idea.Experiments) ||
    !idea.Experiments.length ||
    idea.Experiments.some(
      (item) =>
        !(typeof item === "string" && item.trim()) &&
        !(
          item &&
          typeof item === "object" &&
          !Array.isArray(item) &&
          Object.keys(item).length
        ),
    )
  )
    errors.Experiments =
      "Add at least one nonempty experiment (text or a structured object).";
  const risks = idea["Risk Factors and Limitations"];
  if (
    !Array.isArray(risks) ||
    risks.some((item) => typeof item !== "string" || !item.trim())
  )
    errors["Risk Factors and Limitations"] =
      "Use a list of nonempty risk descriptions; an empty list is allowed.";
  return errors;
}
function BufferedJson({
  id,
  label,
  value,
  objectOnly = false,
  disabled = false,
  onChange,
  onProblem,
}: {
  id: string;
  label: string;
  value: Json | undefined;
  objectOnly?: boolean;
  disabled?: boolean;
  onChange: (value: Json) => void;
  onProblem: (error: string | null) => void;
}) {
  const [text, setText] = useState(() =>
    JSON.stringify(value ?? null, null, 2),
  );
  const [error, setError] = useState("");
  const callback = useRef(onProblem);
  callback.current = onProblem;
  const lastValue = useRef(JSON.stringify(value));
  const invalid = useRef(false);
  useEffect(() => {
    const next = JSON.stringify(value);
    if (invalid.current) return;
    if (lastValue.current !== next) {
      lastValue.current = next;
      setText(JSON.stringify(value ?? null, null, 2));
      setError("");
      callback.current(null);
    }
  }, [value]);
  function change(next: string) {
    setText(next);
    try {
      const parsed: Json = JSON.parse(next);
      if (
        objectOnly &&
        (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      )
        throw new Error("Enter a JSON object, preserving every needed key.");
      invalid.current = false;
      lastValue.current = JSON.stringify(parsed);
      setError("");
      onProblem(null);
      onChange(parsed);
    } catch (failure) {
      invalid.current = true;
      const message =
        failure instanceof Error &&
        failure.message.startsWith("Enter a JSON object")
          ? failure.message
          : "Enter valid JSON before saving. Your unparsed text is kept here.";
      setError(message);
      onProblem(message);
    }
  }
  return (
    <label className="field" htmlFor={id}>
      {label}
      <textarea
        className="json-editor"
        id={id}
        rows={10}
        value={text}
        disabled={disabled}
        onChange={(event) => change(event.target.value)}
        spellCheck={false}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? `${id}-error` : undefined}
      />
      {error && (
        <span className="field-error" id={`${id}-error`}>
          {error}
        </span>
      )}
    </label>
  );
}

export default function IdeaEditor() {
  const { ideaId } = useParams();
  const record = useApi<IdeaRecord>(
    ideaId ? `/api/ideas/${encodeURIComponent(ideaId)}` : null,
  );
  return (
    <div className="stack">
      <Link to="/ideas" className="row">
        <ArrowLeft size={16} aria-hidden="true" /> All proposals
      </Link>
      <ErrorNotice error={record.error} />
      {record.data && record.data.id === ideaId ? (
        <Editor key={record.data.id} initial={record.data} />
      ) : record.loading ? (
        <p role="status">Loading proposal…</p>
      ) : null}
    </div>
  );
}
function Editor({ initial }: { initial: IdeaRecord }) {
  const [saved, setSaved] = useState(initial);
  const [draft, setDraft] = useState<Idea>(initial.idea);
  const [syntax, setSyntax] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const busy = useRef(false);
  const [error, setError] = useState<unknown>(null);
  const [message, setMessage] = useState("");
  const [reloadError, setReloadError] = useState<unknown>(null);
  const [editorVersion, setEditorVersion] = useState(0);
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(saved.idea) ||
    Object.keys(syntax).length > 0;
  const errors = validate(draft);
  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function problem(key: string, value: string | null) {
    setSyntax((current) => {
      if (value === null && !(key in current)) return current;
      if (value !== null && current[key] === value) return current;
      const next = { ...current };
      if (value) next[key] = value;
      else delete next[key];
      return next;
    });
  }
  function update(key: string, value: Json) {
    setDraft((current) => ({ ...current, [key]: value }));
    setMessage("");
  }
  function fieldError(key: string): ReactNode {
    return errors[key] ? (
      <span className="field-error" id={`error-${key.replaceAll(" ", "-")}`}>
        {errors[key]}
      </span>
    ) : null;
  }
  function textField(key: string, label: string, rows = 3) {
    const value = draft[key];
    const id = `proposal-${key.replaceAll(" ", "-")}`;
    if (value !== undefined && typeof value !== "string")
      return (
        <div className="stack" key={key}>
          <p className="notice">
            {label} contains a structured value. It is preserved below; use a
            JSON string for ordinary text.
          </p>
          <BufferedJson
            id={id}
            label={`${label} — saved structured value`}
            value={value}
            onChange={(next) => update(key, next)}
            onProblem={(next) => problem(key, next)}
          />
          {fieldError(key)}
        </div>
      );
    return (
      <label className="field" htmlFor={id} key={key}>
        {label}
        {rows === 1 ? (
          <input
            id={id}
            value={value ?? ""}
            onChange={(event) => update(key, event.target.value)}
            aria-invalid={Boolean(errors[key])}
            aria-describedby={
              errors[key] ? `error-${key.replaceAll(" ", "-")}` : undefined
            }
          />
        ) : (
          <textarea
            id={id}
            rows={rows}
            value={value ?? ""}
            onChange={(event) => update(key, event.target.value)}
            aria-invalid={Boolean(errors[key])}
            aria-describedby={
              errors[key] ? `error-${key.replaceAll(" ", "-")}` : undefined
            }
          />
        )}
        {fieldError(key)}
      </label>
    );
  }
  function listField(key: string, label: string, structured: boolean) {
    const value = draft[key];
    function replace(items: Json[]) {
      setSyntax((current) =>
        Object.fromEntries(
          Object.entries(current).filter(
            ([name]) => !name.startsWith(`${key}:`),
          ),
        ),
      );
      update(key, items);
    }
    if (!Array.isArray(value))
      return (
        <section className="stack">
          <h2>{label}</h2>
          <p className="notice">
            This field is not a list. Its saved value is preserved. Edit the
            complete JSON value below to make a list.
          </p>
          <BufferedJson
            id={`list-${structured ? "experiments" : "risks"}`}
            label={label}
            value={value}
            onChange={(next) => update(key, next)}
            onProblem={(next) => problem(key, next)}
          />
          {fieldError(key)}
          {value === undefined && (
            <button
              className="button secondary"
              type="button"
              onClick={() => update(key, [])}
            >
              Create an empty list
            </button>
          )}
        </section>
      );
    return (
      <section
        className="stack"
        aria-labelledby={structured ? "experiments-heading" : "risks-heading"}
      >
        <h2 id={structured ? "experiments-heading" : "risks-heading"}>
          {label}
        </h2>
        {fieldError(key)}
        {value.length === 0 && (
          <p className="muted">
            {structured
              ? "No experiments added."
              : "No risks added. An empty list is allowed."}
          </p>
        )}
        {value.map((item, index) => (
          <div className="card stack editor-list-item" key={index}>
            {typeof item === "string" ? (
              <label
                className="field"
                htmlFor={`${structured ? "experiment" : "risk"}-${index}`}
              >
                {structured ? "Experiment" : "Risk"} {index + 1}
                <textarea
                  id={`${structured ? "experiment" : "risk"}-${index}`}
                  rows={3}
                  value={item}
                  onChange={(event) =>
                    update(
                      key,
                      value.map((current, i) =>
                        i === index ? event.target.value : current,
                      ),
                    )
                  }
                />
              </label>
            ) : (
              <details className="advanced">
                <summary>
                  {structured
                    ? "Structured experiment"
                    : "Structured risk value"}{" "}
                  {index + 1} — edit all keys
                </summary>
                <JsonText value={item} />
                <BufferedJson
                  id={`${structured ? "experiment" : "risk"}-json-${index}`}
                  label={`Complete ${structured ? "experiment" : "risk"} JSON ${index + 1}`}
                  value={item}
                  objectOnly={
                    structured &&
                    Boolean(item) &&
                    typeof item === "object" &&
                    !Array.isArray(item)
                  }
                  onChange={(next) =>
                    update(
                      key,
                      value.map((current, i) => (i === index ? next : current)),
                    )
                  }
                  onProblem={(next) => problem(`${key}:${index}`, next)}
                />
              </details>
            )}
            <div className="actions">
              <button
                className="button secondary"
                type="button"
                disabled={index === 0 || Object.keys(syntax).length > 0}
                onClick={() => {
                  const next = [...value];
                  [next[index - 1], next[index]] = [
                    next[index],
                    next[index - 1],
                  ];
                  replace(next);
                }}
                aria-label={`Move ${structured ? "experiment" : "risk"} ${index + 1} up`}
              >
                Move up
              </button>
              <button
                className="button secondary"
                type="button"
                disabled={
                  index === value.length - 1 || Object.keys(syntax).length > 0
                }
                onClick={() => {
                  const next = [...value];
                  [next[index + 1], next[index]] = [
                    next[index],
                    next[index + 1],
                  ];
                  replace(next);
                }}
                aria-label={`Move ${structured ? "experiment" : "risk"} ${index + 1} down`}
              >
                Move down
              </button>
              <button
                className="button secondary"
                type="button"
                disabled={Object.keys(syntax).length > 0}
                onClick={() => replace(value.filter((_, i) => i !== index))}
                aria-label={`Remove ${structured ? "experiment" : "risk"} ${index + 1}`}
              >
                <Trash2 size={16} aria-hidden="true" />
                Remove
              </button>
            </div>
          </div>
        ))}
        <div className="actions">
          <button
            type="button"
            className="button secondary"
            onClick={() => update(key, [...value, ""])}
          >
            <Plus size={16} aria-hidden="true" />
            Add {structured ? "experiment" : "risk"}
          </button>
          {structured && (
            <button
              type="button"
              className="button secondary"
              onClick={() => update(key, [...value, {}])}
            >
              Add structured experiment
            </button>
          )}
        </div>
      </section>
    );
  }
  async function save() {
    if (busy.current || !dirty || Object.keys(syntax).length) return;
    busy.current = true;
    setSaving(true);
    setError(null);
    try {
      const result = await mutate<IdeaRecord>(
        `/api/ideas/${encodeURIComponent(saved.id)}`,
        { expected_revision: saved.revision, idea: draft },
        "PATCH",
      );
      setSaved(result);
      setDraft(result.idea);
      setSyntax({});
      setMessage(`Saved revision ${result.revision}.`);
    } catch (failure) {
      setError(failure);
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }
  async function reloadSaved() {
    if (
      !window.confirm(
        "Discard this local draft and load the latest saved proposal?",
      )
    )
      return;
    setReloadError(null);
    try {
      const response = await fetch(
        `/api/ideas/${encodeURIComponent(saved.id)}`,
      );
      if (!response.ok)
        throw new Error(
          "Could not load the latest saved proposal. Your draft is still here.",
        );
      const latest = (await response.json()) as IdeaRecord;
      setSaved(latest);
      setDraft(latest.idea);
      setSyntax({});
      setEditorVersion((version) => version + 1);
      setError(null);
      setMessage(`Loaded saved revision ${latest.revision}.`);
    } catch (failure) {
      setReloadError(failure);
    }
  }
  const canPrepare =
    !dirty &&
    !saving &&
    !Object.keys(saved.errors).length &&
    !Object.keys(errors).length;
  return (
    <>
      <PageHeading
        eyebrow={`Proposal · Revision ${saved.revision}`}
        title="Edit proposal"
      >
        <p className="muted">
          Save your changes before preparing an experiment. Incomplete drafts
          can be saved.
        </p>
      </PageHeading>
      <div className="card stack">
        <fieldset
          key={editorVersion}
          disabled={saving}
          className="editor-fields stack"
        >
          {textField("Title", "Title", 1)}
          {textField("Short Hypothesis", "Hypothesis")}
          {textField("Abstract", "Abstract", 6)}
          {textField("Related Work", "Related work", 5)}
          <p className="metadata">
            Related work may be empty. Saved text does not establish that a
            literature search was performed or that the idea is novel.
          </p>
          {listField("Experiments", "Experiments", true)}
          {listField(
            "Risk Factors and Limitations",
            "Risks and limitations",
            false,
          )}
          <details className="advanced">
            <summary>
              Advanced{errors.Name ? " — internal Name needs attention" : ""}
            </summary>
            <div className="stack">
              {textField("Name", "Internal Name", 1)}
              <p className="metadata">
                The internal Name is part of the scientific proposal, not the
                output directory. Unknown generated fields are preserved in the
                raw draft and export.
              </p>
              <BufferedJson
                id="raw-proposal"
                label="Raw scientific draft JSON"
                value={draft}
                objectOnly
                disabled={Object.keys(syntax).some((key) => key !== "raw")}
                onChange={(value) => {
                  setDraft(value as Idea);
                  setMessage("");
                }}
                onProblem={(value) => problem("raw", value)}
              />
            </div>
          </details>
          <details className="advanced">
            <summary>Original generated JSON — read only</summary>
            <JsonText value={saved.original} />
          </details>
        </fieldset>
        <ErrorNotice error={error} />
        <ErrorNotice error={reloadError} />
        {error instanceof ApiError && error.status === 409 && (
          <div className="notice">
            <p>
              Your saved revision is stale. Your local draft has not been
              discarded. Copy the raw draft if needed before loading the latest
              saved proposal.
            </p>
            <button
              type="button"
              className="button secondary"
              onClick={reloadSaved}
            >
              Discard local draft and load latest
            </button>
          </div>
        )}
        {Object.keys(syntax).length > 0 && (
          <p className="field-error" role="alert">
            Resolve the JSON syntax errors before saving. Unparsed edits have
            not been saved.
          </p>
        )}
        {Object.keys(errors).length > 0 && (
          <div className="notice">
            <strong>Experiment preparation needs these fields:</strong>
            <ul>
              {Object.entries(errors).map(([key, value]) => (
                <li key={key}>
                  {key}: {value}
                </li>
              ))}
            </ul>
            <p>You can still save this as an incomplete draft.</p>
          </div>
        )}
        <p role="status" className="metadata">
          {dirty ? "Unsaved changes" : message || "All changes saved"}
        </p>
        <div className="actions">
          <button
            type="button"
            className="button primary"
            disabled={!dirty || saving || Object.keys(syntax).length > 0}
            onClick={save}
          >
            <Save size={18} aria-hidden="true" />
            {saving ? "Saving…" : "Save changes"}
          </button>
          {canPrepare ? (
            <Link
              className="button secondary"
              to={`/ideas/${encodeURIComponent(saved.id)}/setup`}
            >
              Prepare experiment <ArrowRight size={18} aria-hidden="true" />
            </Link>
          ) : (
            <button type="button" className="button secondary" disabled>
              Prepare experiment
            </button>
          )}
          {saved.errors.Name ? (
            <button type="button" className="button secondary" disabled title="Save a valid internal Name before exporting.">
              <Download size={18} aria-hidden="true" />Export saved proposal
            </button>
          ) : (
            <a className="button secondary" href={`/api/ideas/${encodeURIComponent(saved.id)}/export`} download>
              <Download size={18} aria-hidden="true" />Export saved proposal
            </a>
          )}
        </div>
        {dirty && (
          <p className="metadata">
            Export downloads the saved revision only, as a launcher-compatible
            single-proposal JSON array.
          </p>
        )}
      </div>
      {blocker.state === "blocked" && (
        <div className="modal-backdrop">
          <section
            className="modal stack"
            role="dialog"
            aria-modal="true"
            aria-labelledby="unsaved-heading"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                blocker.reset();
              }
              if (event.key === "Tab") {
                const buttons =
                  event.currentTarget.querySelectorAll<HTMLButtonElement>(
                    "button",
                  );
                const first = buttons[0],
                  last = buttons[buttons.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                  event.preventDefault();
                  last?.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                  event.preventDefault();
                  first?.focus();
                }
              }
            }}
          >
            <h2 id="unsaved-heading">Leave without saving?</h2>
            <p>Your unsaved proposal edits will be lost.</p>
            <div className="actions">
              <button
                type="button"
                autoFocus
                className="button primary"
                onClick={() => blocker.reset()}
              >
                Keep editing
              </button>
              <button
                type="button"
                className="button danger"
                onClick={() => blocker.proceed()}
              >
                Discard changes and leave
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
