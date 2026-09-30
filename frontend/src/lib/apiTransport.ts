export function apiHeaders(input?: HeadersInit): Headers {
  const headers = new Headers(input)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  return headers
}

/** Describe validation fields without reflecting submitted passwords, tokens, or inputs. */
export function apiErrorMessage(body: unknown, fallback: string): string {
  if (body === null || typeof body !== 'object') return fallback
  const value = body as { detail?: unknown; message?: unknown }
  if (typeof value.detail === 'string') return value.detail
  if (typeof value.message === 'string') return value.message
  if (Array.isArray(value.detail)) {
    const messages = value.detail.flatMap((item: unknown) => {
      if (!item || typeof item !== 'object') return []
      const issue = item as { loc?: unknown; msg?: unknown }
      if (typeof issue.msg !== 'string') return []
      const path = Array.isArray(issue.loc) ? issue.loc.filter((part) => typeof part === 'string' || typeof part === 'number').filter((part) => !['body', 'query', 'path'].includes(String(part))).join('.') : ''
      return [path ? `${path}: ${issue.msg}` : issue.msg]
    })
    if (messages.length) return messages.join('; ')
  }
  return fallback
}
