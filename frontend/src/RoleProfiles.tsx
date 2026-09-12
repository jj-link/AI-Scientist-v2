import { useEffect, useId, useRef, useState } from "react";
import { RefreshCw, Save } from "lucide-react";
import { ApiError, mutate, useApi, type ModelConfigEditorRole, type RoleAssignments, type RoleProfile } from "./api";
import { ErrorNotice } from "./components";
import { serializeRoleAssignments } from "./roleAssignments";

export default function RoleProfiles({ roles, onLoad, showSave, disabled = false, saveDisabled = false }: {
  roles: Record<string, ModelConfigEditorRole>;
  onLoad: (roles: RoleAssignments) => void;
  showSave: boolean;
  disabled?: boolean;
  saveDisabled?: boolean;
}) {
  const listing = useApi<{ profiles: RoleProfile[] }>("/api/models/profiles");
  const [profiles, setProfiles] = useState<RoleProfile[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [refreshNeeded, setRefreshNeeded] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const savingRequest = useRef(false);
  const id = useId();
  useEffect(() => {
    if (!listing.data) return;
    setProfiles(listing.data.profiles);
    setRefreshNeeded(false);
  }, [listing.data]);
  const selected = profiles.find((profile) => profile.id === selectedId);
  const positive = (value: unknown) => value === null ||
    (typeof value === "number" && Number.isFinite(value) && value > 0);
  const invalidRoles = Object.values(roles).some((role) =>
    (role.endpoint === null ? role.model !== null : !role.endpoint.trim() || !role.model?.trim()) ||
    !positive(role.max_tokens) || (role.max_tokens !== null && !Number.isInteger(role.max_tokens)) ||
    !positive(role.timeout) ||
    (role.temperature !== null && (typeof role.temperature !== "number" ||
      !Number.isFinite(role.temperature) || role.temperature < 0 || role.temperature > 2)) ||
    (role.api_key_env !== null && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(role.api_key_env)));
  const issues = error instanceof ApiError && Array.isArray(error.detail.errors)
    ? error.detail.errors.filter((issue): issue is { field: string; message: string } =>
      typeof issue?.field === "string" && typeof issue?.message === "string")
    : [];

  async function saveProfile() {
    if (!showSave || disabled || saveDisabled || invalidRoles || savingRequest.current ||
      refreshNeeded || listing.loading || !listing.data || listing.error) return;
    const enteredName = window.prompt("Save role profile\nEnter a profile name (1–100 characters).", name);
    if (enteredName === null) return;
    setName(enteredName);
    const trimmedName = enteredName.trim();
    if (!trimmedName || trimmedName.length > 100 || /[\u0000-\u001f\u007f-\u009f]/.test(trimmedName)) {
      setError(new Error("Enter a profile name with 1–100 characters and no control characters."));
      setNotice("");
      return;
    }
    const existing = profiles.find((profile) => profile.name.toLowerCase() === trimmedName.toLowerCase());
    if (existing && !window.confirm(`Overwrite role profile "${existing.name}" with the current role assignments? This does not change model defaults.`)) return;
    savingRequest.current = true;
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      const assignments = serializeRoleAssignments(roles);
      const result = existing
        ? await mutate<RoleProfile>(`/api/models/profiles/${encodeURIComponent(existing.id)}`, {
          expected_revision: existing.revision, roles: assignments,
        }, "PUT")
        : await mutate<RoleProfile>("/api/models/profiles", { name: trimmedName, roles: assignments });
      setProfiles((current) => [...current.filter((profile) => profile.id !== result.id), result]);
      setSelectedId(result.id);
      setName(result.name);
      setNotice(`Saved role profile "${result.name}". Default assignments and server settings are unchanged.`);
      listing.refresh();
    } catch (failure) {
      setError(failure);
      if (failure instanceof ApiError && (failure.status === 409 || failure.status === 404)) {
        setRefreshNeeded(true);
        setNotice("Profile metadata changed. Refreshing the profile list; your role edits and name are preserved. Review and save again to confirm any overwrite.");
        listing.refresh();
      }
    } finally {
      savingRequest.current = false;
      setSaving(false);
    }
  }

  return (
    <div className="card stack role-profiles" aria-label="Named role-assignment profiles">
      <p className="muted">Profiles are shared between Models and Experiment Setup. Load replaces only the editable role draft. Saving a profile stores the current draft, not server settings or default assignments.</p>
      <div className="field-row">
        <div className="field">
          <label htmlFor={`${id}-profile`}>Saved role profile</label>
          <select id={`${id}-profile`} value={selectedId} disabled={disabled || saving || listing.loading || refreshNeeded}
            onChange={(event) => {
              const next = profiles.find((profile) => profile.id === event.target.value);
              setSelectedId(event.target.value);
              if (next) setName(next.name);
              setNotice("");
              setError(null);
            }}>
            <option value="">{profiles.length ? "Choose a profile" : "No saved profiles"}</option>
            {selectedId && !selected && <option value={selectedId}>Selected profile unavailable</option>}
            {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
          </select>
        </div>
        <div className="toolbar">
          <button type="button" className="button secondary" disabled={disabled || saving || !selected || listing.loading || refreshNeeded || Boolean(listing.error)}
            onClick={() => {
              if (!selected) return;
              try {
                onLoad(selected.roles);
                setName(selected.name);
                setError(null);
                setNotice(`Loaded "${selected.name}" into the role draft. Default assignments are unchanged.`);
              } catch (failure) {
                setError(failure);
                setNotice("");
              }
            }}>Load profile</button>
          <button type="button" className="button secondary" disabled={disabled || saving || listing.loading}
            onClick={() => listing.refresh()}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh profiles
          </button>
          {showSave && (
            <button type="button" className="button secondary" disabled={disabled || saveDisabled || saving || invalidRoles || listing.loading || refreshNeeded || !listing.data || Boolean(listing.error)}
              onClick={() => void saveProfile()}>
              <Save size={16} aria-hidden="true" /> {saving ? "Saving profile…" : "Save profile"}
            </button>
          )}
        </div>
      </div>
      {showSave && invalidRoles && <p className="field-error">Correct invalid overrides and pair each endpoint with a model before saving a profile. A role may be fully unassigned.</p>}
      {listing.loading && <p className="muted" role="status">Loading role profiles…</p>}
      <ErrorNotice error={listing.error} />
      <ErrorNotice error={error} />
      {issues.length > 0 && <ul className="field-error" role="alert">{issues.map((issue, index) => <li key={index}>{issue.field}: {issue.message}</li>)}</ul>}
      {notice && <p role="status" className="muted">{notice}</p>}
    </div>
  );
}
