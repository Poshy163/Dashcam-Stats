import { useState } from 'react'
import { diagnosticFlag, gpsCaptureAvailable, gpsCaptureLabel, gpsFirstFixSummary, gpsObservationPage, gpsProviderObservation } from '@/lib/carplay'
import { formatDateTime } from '@/lib/format'
import type { CarPlayLocationProvider, CarPlayTimingEvent } from '@/lib/types'

const providers: CarPlayLocationProvider[] = ['Gps', 'Fused', 'Network', 'Passive']
const providerName = (provider: CarPlayLocationProvider) => provider === 'Gps' ? 'GPS' : provider
const yesNo = (value: boolean | null) => value == null ? 'Unknown' : value ? 'Yes' : 'No'
const measurement = (value: number | null, unit: string) => value == null ? 'Unknown' : `${value.toFixed(1)} ${unit}`
const age = (ms: number | null) => measurement(ms == null ? null : ms / 1000, 's')

export default function CarPlayGpsView({ events }: { events: CarPlayTimingEvent[] }) {
  const [requestedPage, setRequestedPage] = useState<number | 'latest'>(0)
  const page = gpsObservationPage(events, requestedPage)
  const firstPage = page.page === 0
  const lastPage = page.page >= page.pageCount - 1
  const paginationButton = 'rounded border border-border px-2 py-1 text-content hover:bg-surface disabled:cursor-default disabled:opacity-40'
  return (
    <div className="card p-4 text-sm">
      <div className="font-medium">GPS observations</div>
      <p className="mt-1 text-xs text-content-muted">
        Head-unit location observations at the recorded time. Fix age is measured at capture, not now.
        These observations cannot establish whether the iPhone used its own GPS or the head unit’s location.
      </p>
      {page.total === 0 ? (
        <p className="mt-3 text-content-muted">No GPS observations in this period. Provider state and fix age are unknown.</p>
      ) : (
        <>
          <p className="mt-2 text-xs text-content-muted">Oldest first, so startup observations remain visible. “Captured” describes the measurement, not GPS fix quality.</p>
          <nav aria-label="GPS observation pages" className="mt-3 flex flex-wrap items-center gap-2 text-xs">
            <button type="button" className={paginationButton} disabled={firstPage && requestedPage !== 'latest'} onClick={() => setRequestedPage(0)}>First</button>
            <button type="button" className={paginationButton} disabled={firstPage} onClick={() => setRequestedPage(page.page - 1)}>Previous</button>
            <span className="text-content-muted" aria-live="polite">{page.start + 1}–{page.end} of {page.total} · page {page.page + 1} of {page.pageCount}</span>
            <button type="button" className={paginationButton} disabled={lastPage} onClick={() => setRequestedPage(page.page + 1)}>Next</button>
            <button type="button" className={paginationButton} disabled={requestedPage === 'latest'} onClick={() => setRequestedPage('latest')}>Latest</button>
          </nav>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead><tr>
                <th className="px-2 py-2">Observed</th>
                <th className="px-2 py-2">Capture</th>
                <th className="px-2 py-2">GPS enabled</th>
                <th className="px-2 py-2">GPS fix age</th>
                <th className="px-2 py-2">Reported accuracy</th>
                <th className="px-2 py-2">Details</th>
              </tr></thead>
              <tbody>{page.captures.map((event, index) => {
                const gps = gpsProviderObservation(event, 'Gps')
                const available = gpsCaptureAvailable(event)
                return (
                  <tr key={`${event.sessionId}:${event.occurredAt}:${index}`} className="border-t border-border align-top">
                    <td className="whitespace-nowrap px-2 py-2">{formatDateTime(event.occurredAt)}</td>
                    <td className="px-2 py-2">{gpsCaptureLabel(event)}</td>
                    <td className="px-2 py-2">{yesNo(gps.enabled)}</td>
                    <td className="whitespace-nowrap px-2 py-2 tabular-nums">{age(gps.ageMs)}</td>
                    <td className="whitespace-nowrap px-2 py-2 tabular-nums">{measurement(gps.accuracyM, 'm')}</td>
                    <td className="px-2 py-2">
                      <details>
                        <summary className="cursor-pointer">More</summary>
                        <div className="min-w-80 space-y-3 py-3">
                          <p>Location enabled: {yesNo(available ? diagnosticFlag(event.locationEnabled) : null)}.
                            {' '}GPS started: {yesNo(available ? diagnosticFlag(event.gpsStarted) : null)}.
                          </p>
                          <div>
                            <div className="font-medium">First-fix statistics (cumulative)</div>
                            <p className="mt-1">{gpsFirstFixSummary(event)}</p>
                            <p className="mt-1 text-content-muted">Android’s reported first-fix count and mean accumulate across acquisitions. They are not the acquisition time for this drive.</p>
                          </div>
                          <table className="w-full text-left">
                            <thead><tr><th>Provider</th><th>Reported</th><th>Enabled</th><th>Fix age</th><th>Accuracy</th></tr></thead>
                            <tbody>{providers.map((provider) => {
                              const observation = gpsProviderObservation(event, provider)
                              return <tr key={provider}>
                                <td className="py-1 pr-2">{providerName(provider)}</td>
                                <td className="pr-2">{yesNo(observation.present)}</td>
                                <td className="pr-2">{yesNo(observation.enabled)}</td>
                                <td className="whitespace-nowrap pr-2">{age(observation.ageMs)}</td>
                                <td className="whitespace-nowrap">{measurement(observation.accuracyM, 'm')}</td>
                              </tr>
                            })}</tbody>
                          </table>
                          <p>GPS satellites: {gps.satellites ?? 'Unknown'}.
                            {' '}ZLink GPS listener registered: {yesNo(gps.zlinkListener)}.
                          </p>
                          <p>ZLink app running: {yesNo(available ? diagnosticFlag(event.gpsZlinkProcessPresent) : null)}.
                            {' '}ZLink native process running: {yesNo(available ? diagnosticFlag(event.gpsNativeProcessPresent) : null)}.
                          </p>
                          <p className="text-content-muted">Provider, listener and process presence do not establish a valid fix, a CarPlay connection or location delivery.</p>
                        </div>
                      </details>
                    </td>
                  </tr>
                )
              })}</tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
