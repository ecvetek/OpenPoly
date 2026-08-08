/**
 * Validates a stored-key name against the backend's naming rule.
 *
 * Mirrors ``openpoly/news/secret_store.py``'s ``_NAME_RE`` exactly: segments
 * of `[A-Za-z0-9_-]+` separated by single `/` (a stored-key name may have
 * more than one segment, e.g. "demo/news_source/tradingnews").
 */
const SECRET_NAME_RE = /^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/

export function isValidSecretName(name: string): boolean {
  return SECRET_NAME_RE.test(name)
}
