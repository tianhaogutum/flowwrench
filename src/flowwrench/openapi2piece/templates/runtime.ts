import { httpClient, HttpMethod, HttpRequest } from '@activepieces/pieces-common';
import axios from 'axios';
import { Agent } from 'node:https';
import { Agent as HttpAgent } from 'node:http';

export type Schema = {
  type: string; nullable: boolean; enum?: unknown[]; oneOf?: Schema[]; constraints?: Schema;
  properties?: Record<string, Schema>; required?: string[]; readOnlyProperties?: string[]; additionalProperties?: boolean | Schema; items?: Schema;
  minimum?: number; maximum?: number; exclusiveMinimum?: number; exclusiveMaximum?: number;
  minLength?: number; maxLength?: number; minItems?: number; maxItems?: number; uniqueItems?: boolean;
};
export type Parameter = { id: string; name: string; in: string; required: boolean; schema: Schema };
export type Security = { id: string; name: string; kind: string; in?: string; key?: string; usernameField?: string; passwordField?: string };
export type Operation = {
  path: string; method: string; baseUrl: string | null; props: Parameter[];
  body: { mediaType: string } | null; security: Security[]; securityChoice?: boolean;
  securityRequirements?: { value: string; names: string[] }[];
};
function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && [Object.prototype, null].includes(Object.getPrototypeOf(value));
}
class InvalidInput extends Error {}
function invalid(label: string): never { throw new InvalidInput(`Invalid or missing input: ${label}`); }
function propertyPath(path: string, key: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? `${path}.${key}` : `${path}[${JSON.stringify(key)}]`;
}
function spend(budget: { remaining: number }, steps = 1): void {
  budget.remaining -= steps;
  if (budget.remaining < 0) throw new Error('Input validation limit exceeded');
}
export function validate(value: unknown, schema: Schema, label: string, depth = 0, budget = { remaining: 100000 }, path = label): void {
  spend(budget);
  if (depth > 40) invalid(label);
  if (schema.oneOf) {
    if (schema.constraints) validate(value, schema.constraints, label, depth + 1, budget, path);
    let matches = 0;
    for (const branch of schema.oneOf) {
      try { validate(value, branch, label, depth + 1, budget, path); matches++; }
      catch (error) { if (!(error instanceof InvalidInput)) throw error; }
      if (matches > 1) invalid(label);
    }
    if (matches !== 1) invalid(label);
    return;
  }
  if (value === null) {
    if (!schema.nullable || (schema.enum && !schema.enum.includes(null))) invalid(label);
    return;
  }
  if (schema.enum && !schema.enum.includes(value)) invalid(label);
  switch (schema.type) {
    case 'string': {
      if (typeof value !== 'string') invalid(label);
      const length = [...value].length;
      if ((schema.minLength !== undefined && length < schema.minLength) || (schema.maxLength !== undefined && length > schema.maxLength)) invalid(label);
      break;
    }
    case 'integer': case 'number':
      if (typeof value !== 'number' || !Number.isFinite(value) || (schema.type === 'integer' && !Number.isInteger(value))) invalid(label);
      if ((schema.minimum !== undefined && value < schema.minimum) || (schema.maximum !== undefined && value > schema.maximum)
        || (schema.exclusiveMinimum !== undefined && value <= schema.exclusiveMinimum) || (schema.exclusiveMaximum !== undefined && value >= schema.exclusiveMaximum)) invalid(label);
      break;
    case 'boolean': if (typeof value !== 'boolean') invalid(label); break;
    case 'array':
      if (!Array.isArray(value) || !schema.items) invalid(label);
      if ((schema.minItems !== undefined && value.length < schema.minItems) || (schema.maxItems !== undefined && value.length > schema.maxItems)) invalid(label);
      for (let i = 0; i < value.length; i++) validate(value[i], schema.items, label, depth + 1, budget, `${path}[${i}]`);
      if (schema.uniqueItems) {
        const seen = new Set<string>();
        for (const item of value) {
          const key = jsonKey(item, label, depth + 1, budget);
          if (seen.has(key)) invalid(label);
          seen.add(key);
        }
      }
      break;
    case 'object':
      if (!record(value)) invalid(label);
      // Removed request properties remain forbidden even for open objects, and
      // even when explicitly supplied as null or undefined. Never strip silently.
      for (const key of schema.readOnlyProperties ?? []) {
        if (Object.hasOwn(value, key)) throw new InvalidInput(`Read-only request property: ${propertyPath(path, key)}`);
      }
      for (const key of schema.required ?? []) if (!Object.hasOwn(value, key) || value[key] === undefined) invalid(label);
      for (const [key, v] of Object.entries(value)) {
        const child = Object.hasOwn(schema.properties ?? {}, key) ? schema.properties![key] : undefined;
        if (child) validate(v, child, label, depth + 1, budget, propertyPath(path, key));
        else if (schema.additionalProperties === false) invalid(label);
        else if (record(schema.additionalProperties)) validate(v, schema.additionalProperties as Schema, label, depth + 1, budget, propertyPath(path, key));
        else assertJson(v, label, depth + 1, budget);
      }
      break;
    default: invalid(label);
  }
}
function assertJson(value: unknown, label: string, depth: number, budget: { remaining: number }): void {
  spend(budget);
  if (depth > 40) invalid(label);
  if (value === null || typeof value === 'string' || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) return;
  if (Array.isArray(value)) { for (const v of value) assertJson(v, label, depth + 1, budget); return; }
  if (record(value)) { for (const v of Object.values(value)) assertJson(v, label, depth + 1, budget); return; }
  invalid(label);
}
// Canonical JSON is only an equality key: never replace or mutate input.
// Sorted object keys ignore insertion order; array order and scalar types stay
// significant. JSON numbers use their finite value (including -0 equal to 0).
function jsonKey(value: unknown, label: string, depth: number, budget: { remaining: number }): string {
  spend(budget);
  if (depth > 40) invalid(label);
  if (value === null || typeof value === 'boolean' || (typeof value === 'number' && Number.isFinite(value))) return JSON.stringify(value);
  if (typeof value === 'string') {
    spend(budget, value.length);
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    const items: string[] = [];
    for (const item of value) items.push(jsonKey(item, label, depth + 1, budget));
    return `[${items.join(',')}]`;
  }
  if (record(value)) {
    const keys = Object.keys(value);
    spend(budget, keys.length);
    for (const key of keys) spend(budget, key.length);
    keys.sort((a, b) => {
      spend(budget, 1 + Math.max(a.length, b.length));
      return a < b ? -1 : a > b ? 1 : 0;
    });
    return `{${keys.map(key => `${JSON.stringify(key)}:${jsonKey(value[key], label, depth + 1, budget)}`).join(',')}}`;
  }
  invalid(label);
}
function secret(value: unknown): string {
  if (typeof value !== 'string' || !value.length) throw new Error('Missing credentials for this operation');
  return value;
}
function setHeader(headers: Record<string, string>, key: string, value: string): void {
  if (/[\r\n\0]/.test(value)) throw new Error('Invalid header value');
  headers[key] = value;
}
const tlsAgent = new Agent({ rejectUnauthorized: true });
const plainAgent = new HttpAgent();
const transport = axios.create({
  httpsAgent: tlsAgent,
  proxy: false, maxRedirects: 0, validateStatus: () => true,
  maxBodyLength: 8 * 1024 * 1024, maxContentLength: 8 * 1024 * 1024,
});
// The upstream client supplies proxy agents from the process environment. Override them.
transport.interceptors.request.use(config => {
  config.httpAgent = plainAgent;
  config.httpsAgent = tlsAgent;
  config.proxy = false;
  config.maxRedirects = 0;
  config.validateStatus = () => true;
  return config;
});
// Avoid the upstream Axios-error logger, which includes request and response bodies.
transport.interceptors.response.use(response => response, () => { throw new Error('HTTP transport failed'); });

export async function execute(operation: Operation, authKind: string, props: Record<string, unknown>, auth: unknown): Promise<unknown> {
  let base: URL;
  try { base = new URL(String(props.base_url ?? operation.baseUrl ?? '')); } catch { throw new Error('Base URL must be an absolute HTTP(S) URL'); }
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) throw new Error('Unsafe base URL: credentials, query and fragment are forbidden');
  let path = operation.path;
  const query = new URLSearchParams();
  const headers: Record<string, string> = Object.create(null);
  let body: unknown;
  for (const p of operation.props) {
    const value = props[p.id];
    if (value === undefined) { if (p.required) invalid(p.id); continue; }
    validate(value, p.schema, p.id);
    if (p.in === 'path') {
      if (String(value) === '.' || String(value) === '..') invalid(p.id);
      path = path.split(`{${p.name}}`).join(encodeURIComponent(String(value)));
    } else if (p.in === 'query') query.append(p.name, String(value));
    else if (p.in === 'header') setHeader(headers, p.name, String(value));
    else if (p.in === 'body') body = value;
  }
  const connection = record(auth) ? auth : {};
  const custom = record(connection.props) ? connection.props : {};
  let security = operation.security;
  if (operation.securityRequirements) {
    const selected = operation.securityRequirements.find(g => g.value === props.auth_requirement);
    if (!selected) invalid('auth_requirement');
    security = security.filter(s => selected.names.includes(s.name));
  } else if (operation.securityChoice) {
    const selected = security.find(s => s.name === props.auth_scheme);
    if (!selected) invalid('auth_scheme');
    security = [selected];
  }
  for (const s of security) {
    if (s.kind === 'basic') {
      const username = secret(authKind === 'custom' ? custom[s.usernameField!] : connection.username);
      const password = secret(authKind === 'custom' ? custom[s.passwordField!] : connection.password);
      if (username.includes(':')) throw new Error('Basic authentication username cannot contain a colon');
      setHeader(headers, 'Authorization', `Basic ${Buffer.from(`${username}:${password}`).toString('base64')}`);
    } else {
      const token = secret(authKind === 'custom' ? custom[s.id] : s.kind === 'oauth2' ? connection.access_token : connection.secret_text);
      if (s.kind === 'apiKey' && s.in === 'query') query.append(s.key!, token);
      else if (s.kind === 'apiKey') setHeader(headers, s.key!, token);
      else setHeader(headers, 'Authorization', `Bearer ${token}`);
    }
  }
  const url = new URL(base.toString().replace(/\/$/, '') + path);
  if (url.origin !== base.origin) throw new Error('Operation cannot change the base URL origin');
  url.search = query.toString();
  if (body !== undefined) headers['Content-Type'] = operation.body!.mediaType;
  const request: HttpRequest = {
    method: operation.method as HttpMethod, url: url.toString(), headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    timeout: 30000, retries: 0, followRedirects: false,
  };
  // pieces-common 0.12.5 changes this global synchronously before its first await.
  // Restore it immediately; the explicit HTTPS agent also enforces certificate verification.
  const tlsSetting = process.env.NODE_TLS_REJECT_UNAUTHORIZED;
  let pending;
  try { pending = httpClient.sendRequest(request, transport); }
  finally {
    if (tlsSetting === undefined) delete process.env.NODE_TLS_REJECT_UNAUTHORIZED;
    else process.env.NODE_TLS_REJECT_UNAUTHORIZED = tlsSetting;
  }
  const response = await pending;
  if (response.status < 200 || response.status >= 300) throw new Error(`HTTP request failed with status ${response.status}`);
  return response.body;
}
