/** Keep bookmarked pagination inside the backend's [1, MAX_PAGE] contract. */
export function listPage(value: string | null): number {
  const page = Number(value)
  return value !== null && /^\d+$/.test(value) && Number.isSafeInteger(page) && page >= 1 && page <= 1_000_000_000 ? page : 1
}

/** An unknown URL value means no boolean filter, rather than silently meaning false. */
export function booleanFilter(value: string | null): boolean | undefined {
  return value === 'true' ? true : value === 'false' ? false : undefined
}

/** IDs represented by this client must be exact positive integers. */
export function rowIdFilter(value: string | null): number | undefined {
  const id = Number(value)
  return value !== null && /^\d+$/.test(value) && Number.isSafeInteger(id) && id > 0 ? id : undefined
}
