import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { RotateCcw, Trash2 } from "lucide-react";
import { mutate, type Job } from "./api";
import { ErrorNotice } from "./components";
import { useStudio } from "./studio";

export default function FailedRunActions({
  job,
  onDeleted,
}: {
  job: Job;
  onDeleted?: () => void;
}) {
  const { refreshBootstrap } = useStudio();
  const navigate = useNavigate();
  const busy = useRef(false);
  const [pending, setPending] = useState<"restart" | "delete" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const eligible = job.kind === "experiment" && job.state === "failed";

  async function act(action: "restart" | "delete") {
    if (!eligible || busy.current) return;
    const name = `${job.title || "Research experiment"} (${job.id})`;
    const confirmation = action === "restart"
      ? `Restart experiment "${name}"? This starts a fresh execution using the original saved study, model settings, and run settings—not your current edits. Old outputs are retained; no checkpoints are resumed. Model calls may incur costs. Generated Python runs with your account permissions and is not sandboxed. Continue?`
      : `Permanently delete failed run "${name}"? Its run outputs, logs, job record, events, and diagnostics will be removed. The saved idea and conversation stay, and other runs are not deleted. This cannot be undone. Continue?`;
    if (!window.confirm(confirmation)) return;
    busy.current = true;
    setPending(action);
    setError(null);
    const path = `/api/jobs/${encodeURIComponent(job.id)}`;
    const storageKey = `scientist-studio-restart-${job.id}`;
    try {
      if (action === "restart") {
        let requestId: string;
        try {
          requestId = sessionStorage.getItem(storageKey) || crypto.randomUUID();
          sessionStorage.setItem(storageKey, requestId);
        } catch {
          throw new Error(
            "Restart was not sent because browser session storage is unavailable. Enable session storage so retries remain safe after navigation or reload.",
          );
        }
        const result = await mutate<{ job_id: string; run_id: string }>(
          `${path}/restart`,
          { request_id: requestId, execution_acknowledged: true },
        );
        try {
          sessionStorage.removeItem(storageKey);
        } catch {
          /* Keeping the successful request ID makes later retries idempotent. */
        }
        refreshBootstrap();
        navigate(`/experiments/${encodeURIComponent(result.job_id)}`);
      } else {
        await mutate<{ deleted: boolean }>(path, {}, "DELETE");
        refreshBootstrap();
        if (onDeleted) onDeleted();
        else navigate("/experiments", { replace: true });
      }
    } catch (failure) {
      setError(failure);
      refreshBootstrap();
    } finally {
      busy.current = false;
      setPending(null);
    }
  }

  if (!eligible) return null;
  return (
    <div>
      <div className="actions">
        <button
          className="button secondary"
          type="button"
          disabled={pending !== null}
          onClick={() => void act("restart")}
        >
          <RotateCcw size={16} aria-hidden="true" />
          {pending === "restart" ? "Restarting experiment…" : "Restart experiment"}
        </button>
        <button
          className="button danger"
          type="button"
          disabled={pending !== null}
          onClick={() => void act("delete")}
        >
          <Trash2 size={16} aria-hidden="true" />
          {pending === "delete" ? "Deleting failed run…" : "Delete failed run"}
        </button>
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}
