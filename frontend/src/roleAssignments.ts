import type { ModelConfigEditorRole, RoleAssignment, RoleAssignments } from "./api";

const emptyAssignment: RoleAssignment = {
  endpoint: null,
  model: null,
  max_tokens: null,
  temperature: null,
  timeout: null,
  api_key_env: null,
};

export function serializeRoleAssignments(
  roles: Record<string, ModelConfigEditorRole>,
): RoleAssignments {
  return Object.fromEntries(Object.entries(roles).map(([name, role]) => [name, {
    endpoint: role.endpoint,
    model: role.model,
    max_tokens: role.max_tokens,
    temperature: role.temperature,
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
    if (role.api_key_env !== null && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(role.api_key_env)) return false;
    return endpoints[role.endpoint].provider !== "openai-codex" ||
      (role.max_tokens === null && role.temperature === null && role.api_key_env === null);
  });
}

export function applyRoleAssignments(
  base: Record<string, ModelConfigEditorRole>,
  assignments: RoleAssignments,
): Record<string, ModelConfigEditorRole> {
  return Object.fromEntries(
    [...new Set([...Object.keys(base), ...Object.keys(assignments)])].map((name) => [name, {
      ...(Object.hasOwn(assignments, name) ? assignments[name] : emptyAssignment),
      requires: Object.hasOwn(base, name) ? base[name].requires : [],
    }]),
  );
}
