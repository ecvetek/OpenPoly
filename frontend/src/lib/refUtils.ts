/**
 * Helpers for secret `*_ref` fields.
 *
 * - Detect whether a value is already a `<scheme>:<...>` ref string.
 * - Sanitize free-form text into a single secret-store name segment.
 *
 * Name rules mirror the backend regex in ``openpoly/news/secret_store.py``
 * (`[A-Za-z0-9_-]` characters).
 */

// Schemes the backend resolver knows about.
const REF_SCHEME_RE = /^(env|local|vault|keychain):/

export function isRefFormatted(value: unknown): boolean {
  return typeof value === 'string' && REF_SCHEME_RE.test(value)
}

// Mirrors openpoly/news/secret_store.py's _NAME_RE exactly: segments of
// [A-Za-z0-9_-]+ separated by single `/` — unlike sanitizeNameSegment
// below, `/` is allowed here since a full stored-key name may have more
// than one segment (e.g. "demo/news_source/tradingnews").
const SECRET_NAME_RE = /^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/

export function isValidSecretName(name: string): boolean {
  return SECRET_NAME_RE.test(name)
}

/**
 * Normalize free-form text into a secret-store name segment: runs of
 * disallowed characters collapse to a single `-`; leading and trailing
 * dashes are stripped.
 */
export function sanitizeNameSegment(s: string): string {
  return s
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
