import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import Spinner from '@/components/Spinner'
import { EmptyState, ErrorState, PageHeader, PlateText, StateBadge } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDateTime, formatDistance, formatDuration } from '@/lib/format'

export default function Search() {
  const [params, setParams] = useSearchParams()
  const q = params.get('q') ?? ''
  const [draft, setDraft] = useState(q)

  // The page owns a box of its own, not just the header's.
  //
  // The header search is hidden below 640px and Search has no nav entry, so on a phone
  // this page could be reached (now, via the header icon) and then offered no way to type
  // anything into it. It is also what makes an inbound link with no `q` useful rather than
  // a dead end.
  useEffect(() => setDraft(q), [q])

  const query = useQuery({
    queryKey: ['search', q],
    queryFn: () => api.search(q),
    enabled: q.length > 0,
  })

  const box = (
    <form
      className="card p-3"
      onSubmit={(event) => {
        event.preventDefault()
        const next = draft.trim()
        setParams(next ? { q: next } : {}, { replace: true })
      }}
    >
      <label className="sr-only" htmlFor="search-page-input">
        Search plates, recordings and journeys
      </label>
      <input
        id="search-page-input"
        className="input"
        placeholder="Search recordings, plates, journeys…"
        value={draft}
        autoFocus={!q}
        onChange={(event) => setDraft(event.target.value)}
      />
    </form>
  )

  if (!q) {
    return (
      <div className="space-y-4">
        <PageHeader title="Search" />
        {box}
        <EmptyState
          title="Search your footage"
          description="Find a licence plate (full or partial, so 'ABC' matches 'ABC123'), a date, a filename, or a journey."
        />
      </div>
    )
  }

  const empty =
    query.data &&
    query.data.plates.length === 0 &&
    query.data.recordings.length === 0 &&
    query.data.journeys.length === 0

  return (
    <div className="space-y-5">
      <PageHeader title="Search" subtitle={<>Results for <strong className="text-content">{q}</strong></>} />
      {box}

      {query.isLoading && <Spinner className="py-16" />}
      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}
      {empty && <EmptyState title="Nothing found" description="Try a partial plate, a date like 2026-08-04, or part of a filename." />}

      {query.data && query.data.plates.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-semibold">Plates</h2>
          <div className="card divide-y divide-border">
            {query.data.plates.map((plate) => (
              <Link key={plate.id} to={`/plates/${plate.id}`} className="flex flex-wrap items-center gap-3 p-2.5 hover:bg-surface-sunken">
                <PlateText
                  text={plate.displayText}
                  confidence={plate.bestConfidence}
                  matchedPattern={plate.patternName}
                />
                <span className="tabular text-xs text-content-muted">
                  {plate.observationCount} sightings
                </span>
                <span className="tabular ml-auto text-xs text-content-faint">
                  last {formatDateTime(plate.lastSeenAt)}
                </span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {query.data && query.data.recordings.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-semibold">Recordings</h2>
          <div className="card divide-y divide-border">
            {query.data.recordings.map((r) => (
              <Link key={r.id} to={`/recordings/${r.id}`} className="flex flex-wrap items-center gap-3 p-2.5 hover:bg-surface-sunken">
                <span className="font-medium">{r.filename}</span>
                <span className="tabular text-xs text-content-muted">{formatDateTime(r.startedAt)}</span>
                <span className="tabular text-xs text-content-muted">{formatDuration(r.durationS)}</span>
                <span className="ml-auto"><StateBadge state={r.state} /></span>
              </Link>
            ))}
          </div>
        </section>
      )}

      {query.data && query.data.journeys.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-semibold">Journeys</h2>
          <div className="card divide-y divide-border">
            {query.data.journeys.map((j) => (
              <Link key={j.id} to={`/journeys/${j.id}`} className="flex flex-wrap items-center gap-3 p-2.5 hover:bg-surface-sunken">
                <span className="font-medium">{formatDateTime(j.startedAt)}</span>
                <span className="tabular text-xs text-content-muted">{formatDuration(j.durationS)}</span>
                <span className="tabular text-xs text-content-muted">{formatDistance(j.distanceM)}</span>
                <span className="tabular ml-auto text-xs text-content-faint">{j.recordingCount} recordings</span>
              </Link>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
