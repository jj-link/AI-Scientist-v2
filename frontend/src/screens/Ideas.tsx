import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowRight, Download, MessageSquare, Plus, Send, Square } from "lucide-react";
import {
  ApiError,
  mutate,
  request,
  useApi,
  type Idea,
  type IdeaConversation,
  type IdeaConversationCreate,
  type IdeaConversationMessage,
  type IdeaRecord,
} from "../api";
import { ErrorNotice, JsonText, PageHeading } from "../components";
import { useStudio } from "../studio";
import "./ideas.css";

type PendingMessage = {
  path: string;
  body: IdeaConversationCreate | IdeaConversationMessage;
};
type ConversationDraft = { message: string; pending?: PendingMessage };

function readDraft(key: string): ConversationDraft {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null") as ConversationDraft | null;
    if (value && typeof value.message === "string") return value;
  } catch {
    // A local draft is optional; submitted conversations live on the server.
  }
  return { message: "" };
}

function writeDraft(key: string, draft: ConversationDraft) {
  try {
    if (!draft.message && !draft.pending) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(draft));
  } catch {
    // Keep the in-memory draft when browser storage is unavailable.
  }
}

function ideaTitle(idea: Idea) {
  return typeof idea.Title === "string" ? idea.Title : "Untitled idea";
}

/** Show every field, including unknown structured fields, without interpreting HTML. */
function CompleteIdea({ idea }: { idea: Idea }) {
  return (
    <dl className="complete-idea">
      {Object.entries(idea).map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd><JsonText value={value} /></dd>
        </div>
      ))}
    </dl>
  );
}

function SavedIdeaSummary({ record }: { record: IdeaRecord }) {
  return (
    <section className="card stack" aria-labelledby="saved-idea-heading">
      <div className="metadata" role="status">Saved · Revision {record.revision}</div>
      <h2 id="saved-idea-heading">{ideaTitle(record.idea)}</h2>
      {Object.keys(record.errors).length > 0 && (
        <div className="notice">
          <p>This saved idea does not yet contain a complete experiment plan.</p>
          <ul>{Object.entries(record.errors).map(([field, message]) => <li key={field}>{field}: {message}</li>)}</ul>
        </div>
      )}
      <div className="actions">
        {Object.keys(record.errors).length === 0 && (
          <Link className="button primary" to={`/ideas/${encodeURIComponent(record.id)}/setup`}>
            Prepare experiment <ArrowRight size={16} aria-hidden="true" />
          </Link>
        )}
        {!record.errors.Name && (
          <a className="button secondary" href={`/api/ideas/${encodeURIComponent(record.id)}/export`} download>
            <Download size={16} aria-hidden="true" /> Export saved idea
          </a>
        )}
      </div>
      <p className="metadata">Preparation uses this saved revision. Further discussion stays unsaved until approved. Nothing runs until you explicitly start the experiment.</p>
    </section>
  );
}

function ConversationWorkspace({ conversationId, seedIdea, onChange, onOpen }: {
  conversationId: string;
  seedIdea?: IdeaRecord;
  onChange: () => void;
  onOpen: (id: string) => void;
}) {
  const { roleConfigId } = useStudio();
  const storageKey = `scientist-studio-idea-chat-${conversationId || (seedIdea ? `idea-${seedIdea.id}` : "new")}`;
  const [draft, setDraft] = useState(() => readDraft(storageKey));
  const draftRef = useRef(draft);
  const [conversation, setConversation] = useState<IdeaConversation | null>(null);
  const [loading, setLoading] = useState(Boolean(conversationId));
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [reload, setReload] = useState(0);
  const busy = useRef(false);
  const mounted = useRef(true);
  const epoch = useRef(0);
  const lastUpdate = useRef("");
  const discussion = useRef<HTMLDivElement>(null);
  const running = conversation?.state === "running";

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const log = discussion.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [conversation?.messages.length]);

  function updateDraft(next: ConversationDraft) {
    draftRef.current = next;
    setDraft(next);
    writeDraft(storageKey, next);
  }

  useEffect(() => {
    if (!conversationId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const load = async () => {
      const started = epoch.current;
      try {
        const next = await request<IdeaConversation>(`/api/idea-conversations/${encodeURIComponent(conversationId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (started === epoch.current && !busy.current) {
          setConversation((previous) => !previous || next.revision >= previous.revision ? next : previous);
          setLoadError(null);
        }
        if (next.state === "running") timer = window.setTimeout(load, 1000);
      } catch (failure) {
        if (!controller.signal.aborted) {
          setLoadError(failure);
          timer = window.setTimeout(load, 1000);
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [conversationId, reload, running]);

  useEffect(() => {
    if (conversation && lastUpdate.current !== `${conversation.revision}:${conversation.updated_at}`) {
      lastUpdate.current = `${conversation.revision}:${conversation.updated_at}`;
      onChange();
    }
  }, [conversation, onChange]);

  async function send(event: FormEvent) {
    event.preventDefault();
    if (busy.current || (!draftRef.current.pending && (running || !draftRef.current.message.trim()))) return;
    if (conversationId && !conversation && !draftRef.current.pending) return;
    const pending = draftRef.current.pending || {
      path: conversationId ? `/api/idea-conversations/${encodeURIComponent(conversationId)}/messages` : "/api/idea-conversations",
      body: conversation ? {
        request_id: crypto.randomUUID(),
        expected_revision: conversation.revision,
        message: draftRef.current.message,
      } : {
        request_id: crypto.randomUUID(),
        role_config_id: roleConfigId,
        message: draftRef.current.message,
        ...(seedIdea ? { idea_id: seedIdea.id } : {}),
      },
    };
    updateDraft({ message: draftRef.current.message, pending });
    busy.current = true;
    epoch.current += 1;
    setSubmitting(true);
    setError(null);
    try {
      const next = await mutate<IdeaConversation>(pending.path, pending.body);
      writeDraft(storageKey, { message: "" });
      if (!mounted.current) return;
      updateDraft({ message: "" });
      setConversation(next);
      setLoadError(null);
      onChange();
      if (!conversationId) onOpen(next.id);
    } catch (failure) {
      if (!mounted.current) return;
      setError(failure);
      // A definite rejection can be edited. Unknown outcomes retain the exact request.
      if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && failure.status !== 408) {
        updateDraft({ message: draftRef.current.message });
      }
    } finally {
      busy.current = false;
      if (mounted.current) {
        setSubmitting(false);
        if (conversationId) setReload((value) => value + 1);
      }
    }
  }

  async function stop() {
    if (busy.current || !conversationId || !running) return;
    busy.current = true;
    epoch.current += 1;
    setStopping(true);
    setError(null);
    try {
      const next = await mutate<IdeaConversation>(`/api/idea-conversations/${encodeURIComponent(conversationId)}/stop`);
      if (!mounted.current) return;
      setConversation(next);
      onChange();
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      busy.current = false;
      if (mounted.current) {
        setStopping(false);
        setReload((value) => value + 1);
      }
    }
  }

  const conflict = error instanceof ApiError && error.status === 409;
  return (
    <section className="card stack conversation-workspace" aria-labelledby="conversation-heading">
      <h2 id="conversation-heading">{conversation?.title || (seedIdea ? `Refine: ${ideaTitle(seedIdea.idea)}` : "Let's develop an idea")}</h2>
      {!conversationId && (
        <p className="muted">{seedIdea ? "Tell the agent what to discuss or change in this saved idea." : "Bring an existing idea, ask a question about a paper, or ask for suggestions. Discuss and refine it here."} The agent will present a complete design for you to approve or revise in your own words.</p>
      )}
      {loading && !conversation && <p role="status">Loading conversation…</p>}
      <ErrorNotice error={loadError} />
      {Boolean(loadError) && <button className="button secondary" type="button" onClick={() => setReload((value) => value + 1)}>Reload conversation</button>}
      {seedIdea && !conversationId && (
        <details className="presented-design">
          <summary>Saved design</summary>
          <CompleteIdea idea={seedIdea.idea} />
        </details>
      )}
      <div ref={discussion} className="conversation-messages" role="log" aria-label="Idea discussion" aria-live="polite" aria-relevant="additions text">
        {conversation?.messages.map((message, index) => (
          <article className={`conversation-message message-${message.role}`} key={index}>
            <h3>{message.role === "user" ? "You" : "Agent"}</h3>
            <div className="conversation-text">{message.content}</div>
            {message.idea && (
              <details className="presented-design">
                <summary>Presented design</summary>
                <CompleteIdea idea={message.idea} />
              </details>
            )}
          </article>
        ))}
      </div>
      {conversation?.pending_idea && (
        <section className="pending-design stack" aria-labelledby="pending-design-heading">
          <div className="metadata">Complete candidate · Conversation revision {conversation.revision} · Not yet saved</div>
          <h3 id="pending-design-heading">Final idea and experiment design</h3>
          <CompleteIdea idea={conversation.pending_idea} />
          <p>Review every field above. Reply in the conversation to approve this exact design, or describe what should change. Only an explicitly approved design is added to the backlog; requesting changes does not approve it.</p>
        </section>
      )}
      {running && (
        <div className="row conversation-progress">
          <p role="status">{conversation.progress || "Agent is working on your message."}</p>
          <button className="button secondary" type="button" disabled={stopping || submitting} onClick={() => void stop()}>
            <Square size={16} aria-hidden="true" /> {stopping ? "Stopping…" : "Stop"}
          </button>
        </div>
      )}
      {conversation?.error && <ErrorNotice error={new Error(conversation.error.message)} />}
      <form className="stack conversation-composer" onSubmit={send}>
        <label className="field" htmlFor="idea-message">
          Message
          <textarea id="idea-message" rows={4} maxLength={50000} required value={draft.message}
            readOnly={submitting || Boolean(draft.pending)}
            onChange={(event) => updateDraft({ message: event.target.value })}
            aria-describedby="idea-message-help"
            placeholder="Share an idea, ask for suggestions, or discuss the design…" />
        </label>
        <p className="metadata" id="idea-message-help">Enter adds a new line. Use Send message to reply. Approval happens here in the conversation, not through a separate save step.</p>
        <ErrorNotice error={error} />
        {conflict && <p className="notice">The conversation or saved artifact changed. Your message is still here. Review the latest conversation and any agent instructions before sending it again.</p>}
        {draft.pending && !submitting && <p className="notice">The last request has not been confirmed. Retry sends the identical message and request ID so it cannot create a duplicate turn.</p>}
        <div className="actions">
          <button className="button primary" type="submit"
            disabled={submitting || stopping || !draft.message.trim() || (!draft.pending && (running || (Boolean(conversationId) && (!conversation || Boolean(loadError))) || (!conversationId && !roleConfigId)))}>
            <Send size={16} aria-hidden="true" /> {submitting ? "Sending…" : draft.pending ? "Retry message" : "Send message"}
          </button>
        </div>
        {!conversationId && !roleConfigId && <p className="notice">A role configuration is required. Check Models to restore the Studio configuration; no assignment is changed here.</p>}
      </form>
    </section>
  );
}

export default function Ideas() {
  const { ideaId } = useParams();
  const [search] = useSearchParams();
  const navigate = useNavigate();
  const conversations = useApi<{ conversations: IdeaConversation[] }>("/api/idea-conversations");
  const ideas = useApi<{ ideas: IdeaRecord[] }>("/api/ideas");
  const associated = ideaId ? conversations.data?.conversations
    .filter((conversation) => conversation.idea_id === ideaId)
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0] : undefined;
  const conversationId = search.get("conversation") || associated?.id || "";
  const savedId = ideaId || conversations.data?.conversations.find((conversation) => conversation.id === conversationId)?.idea_id;
  const approved = useApi<IdeaRecord>(savedId ? `/api/ideas/${encodeURIComponent(savedId)}` : null);
  const refresh = useCallback(() => {
    conversations.refresh();
    ideas.refresh();
    approved.refresh();
  }, [conversations.refresh, ideas.refresh, approved.refresh]);
  const record = approved.data?.id === savedId ? approved.data : undefined;
  const openConversation = (id: string) => {
    navigate(`${ideaId ? `/ideas/${encodeURIComponent(ideaId)}` : "/ideas"}?conversation=${encodeURIComponent(id)}`, { replace: true });
  };
  return (
    <div className="stack">
      <PageHeading eyebrow="Ideas" title="Develop an idea together">
        <p>From a question to an approved research design, in one conversation.</p>
      </PageHeading>
      <div className="actions">
        <Link className="button secondary" to="/ideas"><Plus size={16} aria-hidden="true" /> New conversation</Link>
      </div>
      <div className="ideas-workspace">
        <nav className="card stack conversation-navigation" aria-labelledby="conversations-heading">
          <h2 id="conversations-heading">Conversations</h2>
          <p className="metadata">Discussions and unapproved designs stay here, separate from the backlog.</p>
          <ErrorNotice error={conversations.error} />
          {Boolean(conversations.error) && <button className="button secondary" type="button" onClick={conversations.refresh}>Reload conversations</button>}
          {conversations.loading && !conversations.data && <p role="status">Loading conversations…</p>}
          {conversations.data?.conversations.length === 0 && <p className="muted">Your first conversation starts when you send a message.</p>}
          <ul className="conversation-list">
            {conversations.data?.conversations.map((conversation) => (
              <li key={conversation.id}>
                <Link to={`/ideas?conversation=${encodeURIComponent(conversation.id)}`} aria-current={conversation.id === conversationId ? "page" : undefined}>
                  <span>{conversation.title || "Untitled conversation"}</span>
                  <span className="metadata">{conversation.state === "running" ? "Running" : conversation.state === "failed" ? "Needs attention" : conversation.pending_idea ? "Design awaiting your reply" : conversation.idea_id ? "Linked to saved idea" : "Discussion"}</span>
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <div className="stack conversation-column">
          {(!ideaId || (record && conversations.data)) && (
            <ConversationWorkspace key={conversationId || `new-${ideaId || "idea"}`} conversationId={conversationId} seedIdea={record} onChange={refresh} onOpen={openConversation} />
          )}
        </div>
      </div>
      <section className="stack" aria-labelledby="approved-ideas-heading">
        <div className="section-heading">
          <h2 id="approved-ideas-heading">Approved ideas</h2>
          <span className="metadata">{ideas.data ? `${ideas.data.ideas.length} saved` : ""}</span>
        </div>
        <p className="muted">Approved designs and previously saved artifacts. Conversation drafts never appear here.</p>
        <ErrorNotice error={ideas.error} />
        {ideas.loading && !ideas.data && <p role="status">Loading approved ideas…</p>}
        {ideas.data?.ideas.length === 0 && (
          <div className="empty-state"><MessageSquare size={26} aria-hidden="true" /><h3>No approved ideas yet</h3><p>Discuss a design above and approve the complete artifact in conversation to add it here.</p></div>
        )}
        <div className="card-grid proposal-grid">
          {ideas.data?.ideas.map((item, index) => (
            <article className={`card stack proposal-card proposal-color-${index % 3}`} key={item.id}>
              <div className="metadata">Saved · Revision {item.revision}</div>
              <h3>{ideaTitle(item.idea)}</h3>
              {typeof item.idea["Short Hypothesis"] === "string" && <p className="proposal-excerpt">{item.idea["Short Hypothesis"]}</p>}
              <div className="actions">
                {Object.keys(item.errors).length === 0 && (
                  <Link className="button primary" to={`/ideas/${encodeURIComponent(item.id)}/setup`}>
                    Prepare experiment <ArrowRight size={16} aria-hidden="true" />
                  </Link>
                )}
                <Link className="button secondary" to={`/ideas/${encodeURIComponent(item.id)}`}>Open / refine idea <ArrowRight size={16} aria-hidden="true" /></Link>
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
