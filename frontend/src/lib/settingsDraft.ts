/** A slow save must not erase newer edits made while its request was in flight. */
export function removeSavedDraft(current: Record<string, unknown>, submitted: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(current).filter(([key, value]) => !(key in submitted) || !Object.is(value, submitted[key])))
}
