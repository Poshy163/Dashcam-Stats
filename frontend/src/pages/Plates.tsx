import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, Pagination, PlateText } from '@/components/ui'
import { api, mediaUrl } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

export default function Plates() {
  const [params, setParams] = useSearchParams()

  const page = Number(params.get('page') ?? 1)
  const q = params.get('q') ?? ''
  // The box follows the URL as well as leading it — the same reconciliation Recordings and
  // Logs already do. Seeded once and never resynced, it kept showing "ABC" after Back had
  // taken the filter out of the URL and the list below had gone back to every plate: the
  // form claiming one thing and the results another, permanently, until something else was
  // typed. An inbound /plates?q=… link opened while the page was already mounted did the
  // same in reverse.
  const [term, setTerm] = useState(q)
  const [lastUrlQ, setLastUrlQ] = useState(q)
  if (q !== lastUrlQ) {
    setLastUrlQ(q)
    setTerm(q)
  }
  const sort = params.get('sort') ?? 'last_seen_desc'
  const flagged = params.get('flagged') === 'true' ? true : undefined

  // Debounced so typing a plate does not fire a request per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      const next = new URLSearchParams(params)
      if (term) next.set('q', term)
      else next.delete('q')
      next.delete('page')
      if (next.toString() !== params.toString()) setParams(next, { replace: true })
    }, 250)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [term])

  const query = useQuery({
    queryKey: ['plates', q, page, sort, flagged],
    queryFn: () => api.plates.list({ q: q || undefined, page, pageSize: 24, sort, flagged }),
  })

  /**
   * Changing a *filter* resets to page 1, which is right — the old page number means
   * nothing against a different result set. Changing the page must not go through here:
   * it would set `page` and then delete it on the next line, leaving the URL untouched
   * and the Next button apparently inert. Use `setPage` for that.
   */
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('page')
    setParams(next)
  }

  const setPage = (value: number) => {
    const next = new URLSearchParams(params)
    next.set('page', String(value))
    setParams(next)
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Licence plates"
        subtitle={query.data ? `${query.data.total} plates on record` : undefined}
      />

      <div className="card flex flex-wrap items-end gap-3 p-3">
        <label className="min-w-[16rem] flex-1">
          <span className="label mb-1 block text-xs">Plate</span>
          <input
            className="input font-mono uppercase tracking-wide"
            placeholder="Full or partial — ABC finds ABC123"
            value={term}
            onChange={(e) => setTerm(e.target.value.toUpperCase())}
            autoFocus
          />
        </label>
        <label>
          <span className="label mb-1 block text-xs">Sort</span>
          <select className="input w-auto" value={sort} onChange={(e) => update('sort', e.target.value)}>
            <option value="last_seen_desc">Most recent</option>
            <option value="observations_desc">Most sightings</option>
            <option value="confidence_desc">Highest confidence</option>
            <option value="alpha">Alphabetical</option>
          </select>
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-sm">
          <input
            type="checkbox"
            checked={flagged === true}
            onChange={(e) => update('flagged', e.target.checked ? 'true' : '')}
          />
          Flagged only
        </label>
      </div>

      {query.isLoading && <Spinner className="py-20" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {query.data?.items.length === 0 && (
        <EmptyState
          title={q ? `No plates matching "${q}"` : 'No plates yet'}
          description={
            q
              ? 'Try a shorter fragment — partial matches are supported.'
              : 'Plates appear here once footage has been analysed with plate detection enabled.'
          }
        />
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {query.data?.items.map((plate) => {
          const crop = mediaUrl(plate.representativeCropPath)
          const vehicle = mediaUrl(plate.representativeVehiclePath)
          return (
            <Link
              key={plate.id}
              to={`/plates/${plate.id}`}
              className="card overflow-hidden p-2 transition-colors hover:border-accent/50"
            >
              {vehicle ? (
                <img src={vehicle} alt="" loading="lazy" className="aspect-video w-full rounded object-cover" />
              ) : (
                <div className="flex aspect-video items-center justify-center rounded bg-surface-sunken text-2xs text-content-faint">
                  no vehicle image
                </div>
              )}
              {crop && (
                <img src={crop} alt="" loading="lazy" className="mt-1.5 h-9 rounded border border-border object-contain" />
              )}
              <div className="mt-2 space-y-1">
                <div className="flex items-center gap-1.5">
                  <PlateText
                    text={plate.displayText}
                    confidence={plate.bestConfidence}
                    matchedPattern={plate.patternName}
                  />
                  {plate.flagged && <span className="badge bg-state-warn/15 text-state-warn">flagged</span>}
                </div>
                <div className="tabular text-2xs text-content-faint">
                  {plate.observationCount} sightings · {plate.journeyCount} journeys
                </div>
                <div className="tabular text-2xs text-content-faint">
                  last seen {formatDateTime(plate.lastSeenAt)}
                </div>
              </div>
            </Link>
          )
        })}
      </div>

      {query.data && (
        <Pagination
          page={query.data.page}
          pages={query.data.pages}
          total={query.data.total}
          onChange={setPage}
        />
      )}
    </div>
  )
}
