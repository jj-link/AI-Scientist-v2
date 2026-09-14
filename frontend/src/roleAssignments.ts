import type { ModelConfigEditorRole, ReasoningEffort, RoleAssignment, RoleAssignments } from "./api";

const emptyAssignment: RoleAssignment = {
  endpoint: null,
  model: null,
  max_tokens: null,
  temperature: null,
  reasoning_effort: null,
  timeout: null,
  api_key_env: null,
};

export function validReasoningEffort(value: unknown): value is ReasoningEffort | null {
  return value === null || value === "none" || value === "low" ||
    value === "medium" || value === "high";
}

export function serializeRoleAssignments(
  roles: RoleAssignments,
): RoleAssignments {
  return Object.fromEntries(Object.entries(roles).map(([name, role]) => [name, {
    endpoint: role.endpoint,
    model: role.model,
    max_tokens: role.max_tokens,
    temperature: role.temperature,
    reasoning_effort: role.reasoning_effort ?? null,
    timeout: role.timeout,
    api_key_env: role.api_key_env,
  }]));
}

export function validRoleAssignments(
  roles: Record<string, ModelConfigEditorRole>,
  endpoints: Record<string, { provider?: string }>,
): boolean {
  const positive = (value: unknown) => value === null ||
    (typeof value === "number" && Number.isFinite(value) && value > 0);
  return Object.values(roles).every((role) => {
    if (!role.endpoint || !Object.hasOwn(endpoints, role.endpoint) || !role.model?.trim()) return false;
    if (!positive(role.max_tokens) ||
      (role.max_tokens !== null && !Number.isInteger(role.max_tokens)) ||
      !positive(role.timeout)) return false;
    if (role.temperature !== null && (typeof role.temperature !== "number" ||
      !Number.isFinite(role.temperature) || role.temperature < 0 || role.temperature > 2)) return false;
    if (!validReasoningEffort(role.reasoning_effort)) return false;
    if (role.api_key_env !== null && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(role.api_key_env)) return false;
    return endpoints[role.endpoint].provider !== "openai-codex" ||
      (role.max_tokens === null && role.temperature === null &&
        role.reasoning_effort === null && role.api_key_env === null);
  });
}

export function applyRoleAssignments(
  base: Record<string, ModelConfigEditorRole>,
  assignments: RoleAssignments,
): Record<string, ModelConfigEditorRole> {
  return Object.fromEntries(
    [...new Set([...Object.keys(base), ...Object.keys(assignments)])].map((name) => [name, {
      ...(Object.hasOwn(assignments, name) ? assignments[name] : emptyAssignment),
      reasoning_effort: Object.hasOwn(assignments, name) ? assignments[name].reasoning_effort ?? null : null,
      requires: Object.hasOwn(base, name) ? base[name].requires : [],
    }]),
  );
}
