import { useEffect, useRef, useState } from "react";
import { mutate, useApi, type CodexLogin, type CodexStatus } from "../api";
import { ErrorNotice } from "../components";

export default function CodexProviderCard({ onAuthChange }: { onAuthChange: () => void }) {
  const [polling, setPolling] = useState(false);
  const status = useApi<CodexStatus>("/api/providers/codex", polling ? 1000 : 0);
  const [authorizationUrl, setAuthorizationUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false);
  const previous = useRef<CodexStatus | undefined>(undefined);

  useEffect(() => {
    const current = status.data;
    if (!current) return;
    setPolling(current.pending);
    if (!current.pending) setAuthorizationUrl(null);
    if (previous.current && (previous.current.connected !== current.connected ||
      (previous.current.pending && !current.pending))) onAuthChange();
    previous.current = current;
  }, [status.data, onAuthChange]);

  async function act(action: "login" | "cancel" | "logout") {
    if (inFlight.current) return;
    if (action === "logout" && !window.confirm("Disconnect Codex from AI-Scientist? Future Codex requests will require signing in again.")) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      if (action === "login") {
        const result = await mutate<CodexLogin>("/api/providers/codex/login");
        setAuthorizationUrl(result.authorization_url);
        setPolling(true);
      } else {
        await mutate<CodexStatus>(`/api/providers/codex/${action}`);
        setAuthorizationUrl(null);
        onAuthChange();
      }
      status.refresh();
    } catch (failure) {
      setError(failure);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="card stack" id="codex-provider" aria-labelledby="codex-provider-heading">
      <div className="card-header">
        <h2 id="codex-provider-heading">Codex — ChatGPT sign-in</h2>
        <span className="badge" role="status">
          {status.data?.pending ? "Waiting for sign-in" : status.data?.connected ? "Connected" : "Not connected"}
        </span>
      </div>
      <p className="muted">
        Use your ChatGPT account's Codex models and usage limits, separately from
        OpenAI API-key billing. Credentials stay in this computer's OS credential
        vault, outside YAML and browser storage.
      </p>
      <p className="muted">
        Sign in, choose the Codex endpoint for a role, then detect models and save
        the assignment. Codex manages output-token limits and sampling; selecting
        it clears unsupported token, temperature, and API-key overrides in your draft.
      </p>
      <ErrorNotice error={error || status.error} />
      {status.data?.error && <p className="notice" role="alert">{status.data.error}</p>}
      {authorizationUrl && (
        <p role="status">
          <a className="button primary" href={authorizationUrl} target="_blank" rel="noopener noreferrer">Continue to ChatGPT</a>
          {" "}Complete sign-in in the new tab, then return here.
        </p>
      )}
      <div className="toolbar">
        {!status.data?.pending && (
          <button className="button secondary" disabled={busy || status.loading} onClick={() => void act("login")}>
            {status.data?.connected ? "Sign in with another account" : "Sign in with ChatGPT"}
          </button>
        )}
        {status.data?.pending && (
          <button className="button secondary" disabled={busy} onClick={() => void act("cancel")}>Cancel sign-in</button>
        )}
        {status.data?.connected && (
          <button className="button secondary" disabled={busy} onClick={() => void act("logout")}>Disconnect Codex</button>
        )}
        <button className="button secondary" disabled={busy || status.loading} onClick={status.refresh}>Refresh sign-in status</button>
      </div>
    </section>
  );
}
