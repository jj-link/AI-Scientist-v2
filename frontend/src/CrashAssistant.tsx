import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  BotMessageSquare,
  CircleHelp,
  ExternalLink,
  X,
} from "lucide-react";
import {
  errorMessage,
  mutate,
  request,
  useApi,
  type Diagnostic,
  type DiagnosticAdvice,
  type DiagnosticDraft,
  type DiagnosticEnvelope,
  type Job,
} from "./api";

const adviceLabels: Record<DiagnosticAdvice["classification"], string> = {
  user_action: "Suggested fix for your setup",
  bug: "Possible application bug",
  uncertain: "Uncertain advice",
};

function AdviceBody({ advice }: { advice: DiagnosticAdvice }) {
  return (
    <div className="stack crash-advice">
      <p className="metadata">
        {adviceLabels[advice.classification]} · Model advice, not a verified
        fix
        {advice.classification === "uncertain"
          ? " · The log excerpt did not show enough to decide"
          : ""}
      </p>
      <p className="crash-prose">{advice.summary}</p>
      {advice.evidence.length > 0 && (
        <>
          <h3>Evidence from technical details</h3>
          <ul className="crash-prose">
            {advice.evidence.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      )}
      {advice.steps.length > 0 && (
        <>
          <h3>
            {advice.classification === "bug"
              ? "Reproduction steps"
              : "Corrective steps"}
          </h3>
          <ol className="crash-prose">
            {advice.steps.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}

function ReviewDialog({
  diagnostic,
  refresh,
  onClose,
}: {
  diagnostic: Diagnostic;
  refresh: () => void;
  onClose: () => void;
}) {
  const saved = diagnostic.draft;
  const [phase, setPhase] = useState<"edit" | "preview">("edit");
  const [title, setTitle] = useState(
    saved?.title ?? diagnostic.result?.issue?.title ?? "",
  );
  const [body, setBody] = useState(
    saved?.body ?? diagnostic.result?.issue?.body ?? "",
  );
  const [preview, setPreview] = useState<DiagnosticDraft | null>(null);
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const firstField = useRef<HTMLTextAreaElement>(null);
  const issueState = diagnostic.issue.state;
  const published = issueState === "published" && diagnostic.issue.url;
  const unknown = issueState === "unknown";

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusFirst = () => {
      if (firstField.current?.isConnected) {
        firstField.current.focus();
        return;
      }
      dialog.current
        ?.querySelector<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled])',
        )
        ?.focus();
    };
    focusFirst();
    const keepFocus = (event: FocusEvent) => {
      if (
        event.target instanceof Node &&
        !dialog.current?.contains(event.target)
      )
        focusFirst();
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
      if (event.key === "Tab") {
        const elements = dialog.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex="0"]',
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
  }, [onClose]);

  async function saveDraft() {
    setPending(true);
    setActionError(null);
    setConflict(null);
    try {
      const result = await mutate<DiagnosticEnvelope>(
        `/api/jobs/${diagnostic.job_id}/diagnostic/draft`,
        { expected_revision: diagnostic.draft?.revision ?? 0, title, body },
      );
      if (result.diagnostic?.draft) {
        setPreview(result.diagnostic.draft);
        setPhase("preview");
      }
      refresh();
    } catch (error) {
      setActionError(error);
    } finally {
      setPending(false);
    }
  }

  async function publish() {
    if (!preview) return;
    setPending(true);
    setActionError(null);
    setConflict(null);
    try {
      await mutate<DiagnosticEnvelope>(
        `/api/jobs/${diagnostic.job_id}/diagnostic/issue`,
        { revision: preview.revision, confirmed: true },
      );
      refresh();
    } catch (error) {
      const status =
        error instanceof Object && "status" in error
          ? (error as { status: number }).status
          : 0;
      if (status === 409) {
        // Show the current server draft and require review again.
        try {
          const current = await request<DiagnosticEnvelope>(
            `/api/jobs/${diagnostic.job_id}/diagnostic`,
          );
          if (current.diagnostic?.draft) {
            setTitle(current.diagnostic.draft.title);
            setBody(current.diagnostic.draft.body);
          }
        } catch {
          /* The next refresh shows the server draft. */
        }
        setConflict(
          errorMessage(error) ||
            "The draft changed on the server. Review the current revision again.",
        );
        setPreview(null);
        setPhase("edit");
        refresh();
      } else {
        setActionError(error);
      }
    } finally {
      setPending(false);
    }
  }

  return createPortal(
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="crash-review-title"
        className="card crash-review"
      >
        <div className="card-header">
          <h2 id="crash-review-title">Review GitHub issue</h2>
          <p className="muted">
            This report is published publicly to a fixed repository only after
            your explicit approval of its exact content.
          </p>
        </div>
        <button
          className="crash-close"
          onClick={onClose}
          aria-label="Close review dialog"
        >
          <X size={18} aria-hidden="true" />
        </button>
        {published ? (
          <div className="stack">
            <p>
              Published:{" "}
              <a
                href={diagnostic.issue.url!}
                target="_blank"
                rel="noopener noreferrer"
              >
                {diagnostic.issue.url}
              </a>
            </p>
            <div className="actions">
              <button className="button secondary" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : unknown ? (
          <div className="stack">
            <div className="error-notice" role="alert">
              <CircleHelp size={20} aria-hidden="true" />
              <div>
                <p>
                  GitHub may have created this issue. Check the repository
                  before posting another report; publication is blocked here
                  until then.
                </p>
              </div>
            </div>
            <a
              className="button secondary"
              href={`https://github.com/${diagnostic.draft?.repository ?? ""}/issues`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={16} aria-hidden="true" />
              Open the repository issue list
            </a>
            <div className="actions">
              <button className="button secondary" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        ) : phase === "edit" ? (
          <div className="stack">
            <p className="metadata">
              Public repository: <code>{diagnostic.draft?.repository}</code> ·
              The exact text below is what would be published.
            </p>
            {conflict && (
              <div className="error-notice" role="alert">
                <CircleHelp size={20} aria-hidden="true" />
                <div>
                  <p>{conflict}</p>
                </div>
              </div>
            )}
            <label className="field">
              Title
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={256}
              />
            </label>
            <label className="field">
              Report body
              <textarea
                ref={firstField}
                value={body}
                onChange={(event) => setBody(event.target.value)}
                maxLength={12000}
                rows={10}
              />
            </label>
            {actionError != null && (
              <p role="alert">{errorMessage(actionError)}</p>
            )}
            <div className="actions">
              <button
                className="button primary"
                onClick={() => void saveDraft()}
                disabled={pending || !title.trim() || !body.trim()}
              >
                {pending ? "Preparing review…" : "Review publication"}
              </button>
              <button className="button secondary" onClick={onClose}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          preview && (
            <div className="stack">
              <p className="metadata">
                Exact reviewed content for{" "}
                <code>{preview.repository}</code>. Editing requires a new
                review.
              </p>
              <h3>{preview.title}</h3>
              <pre className="crash-prose crash-review-body">
                {preview.body}
              </pre>
              {actionError != null && (
                <p role="alert">{errorMessage(actionError)}</p>
              )}
              <div className="actions">
                <button
                  className="button primary"
                  onClick={() => void publish()}
                  disabled={pending || issueState === "publishing"}
                >
                  {pending || issueState === "publishing"
                    ? "Publishing…"
                    : "Publish issue"}
                </button>
                <button
                  className="button secondary"
                  onClick={() => {
                    setPreview(null);
                    setPhase("edit");
                  }}
                  disabled={pending || issueState === "publishing"}
                >
                  Back to edit
                </button>
              </div>
            </div>
          )
        )}
      </div>
    </div>,
    document.body,
  );
}

export default function CrashAssistant({
  job,
  onOpenLog,
}: {
  job: Job;
  onOpenLog: () => void;
}) {
  const [reviewOpen, setReviewOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [reopen, setReopen] = useState(false);
  const { data, error, refresh } = useApi<DiagnosticEnvelope>(
    `/api/jobs/${job.id}/diagnostic`,
    2000,
  );
  const diagnostic = data?.diagnostic ?? undefined;
  useEffect(() => {
    setHidden(false);
    setReopen(false);
    setReviewOpen(false);
  }, [job.id]);
  if (!diagnostic || diagnostic.state === "skipped") return null;
  const analysisActive =
    diagnostic.state === "watching" ||
    diagnostic.state === "pending" ||
    diagnostic.state === "analyzing";
  const settled = diagnostic.state === "ready" || diagnostic.state === "unavailable";
  if (error != null && !diagnostic) {
    return (
      <p className="metadata" role="status">
        Connection lost—reconnecting. Saved assistant results will reappear.
      </p>
    );
  }
  if (analysisActive) {
    return (
      <div className="crash-bubble" role="status">
        <BotMessageSquare size={18} aria-hidden="true" />
        <span>
          {diagnostic.state === "analyzing"
            ? "Crash assistant is analyzing the recorded failure…"
            : "Crash assistant will analyze this failure once it is recorded."}
        </span>
      </div>
    );
  }
  if ((diagnostic.dismissed || hidden) && !reopen) {
    return (
      <button
        className="text-button"
        onClick={() => {
          setReopen(true);
          setHidden(false);
        }}
      >
        Crash assistant
      </button>
    );
  }
  if (diagnostic.state === "ready" && diagnostic.result) {
    const advice = diagnostic.result;
    const hasDraft = Boolean(diagnostic.draft || advice.issue);
    return (
      <>
        <div className="crash-bubble open" role="status">
          <div className="crash-bubble-head">
            <strong>
              <BotMessageSquare size={18} aria-hidden="true" /> Crash assistant
            </strong>
            <button
              className="crash-close"
              aria-label="Dismiss assistant advice"
              onClick={() => {
                setHidden(true);
                void mutate(`/api/jobs/${job.id}/diagnostic/dismiss`, {})
                  .then(refresh)
                  .catch(() => undefined);
              }}
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          <AdviceBody advice={advice} />
          <div className="actions">
            {hasDraft && (
              <button
                className="button secondary"
                onClick={() => setReviewOpen(true)}
              >
                Review GitHub issue
              </button>
            )}
            <button className="text-button" onClick={onOpenLog}>
              Open technical details
            </button>
          </div>
        </div>
        {reviewOpen && (
          <ReviewDialog
            diagnostic={diagnostic}
            refresh={refresh}
            onClose={() => setReviewOpen(false)}
          />
        )}
      </>
    );
  }
  if (settled) {
    return (
      <div className="crash-bubble" role="status">
        <CircleHelp size={16} aria-hidden="true" />
        <span>
          {diagnostic.error?.message ??
            "Crash analysis did not produce advice."}
        </span>
        <button className="text-button" onClick={onOpenLog}>
          Open technical details
        </button>
      </div>
    );
  }
  return null;
}
