import type { OBDDriveSummary, OBDRecordingGap } from '@/lib/api'
import { formatDateTimeMillis } from '@/lib/format'

function valid(gap: OBDRecordingGap | null | undefined): gap is OBDRecordingGap {
  return !!gap && Number.isFinite(gap.observationIntervalS) && gap.observationIntervalS > 0 &&
    gap.observationIntervalS <= 120 &&
    ['likely_reconnect', 'unconfirmed_interruption'].includes(gap.classification)
}

/** Reconnect intervals are outside the individual segment's cadence counters. */
export function ObdRecordingGaps({ drive }: { drive: OBDDriveSummary }) {
  const rows = [
    { side: 'Before this segment', gap: drive.recordingGapBefore, direction: 'previous' },
    { side: 'After this segment', gap: drive.recordingGapAfter, direction: 'next' },
  ] as const
  const visible = rows.filter((row) => valid(row.gap))
  if (!visible.length) return null
  return <section className="card space-y-3 border-state-warn/40 p-4" aria-label="Gaps between OBD recordings">
    <h2 className="font-medium text-state-warn">Gaps between recordings</h2>
    {visible.map(({ side, gap, direction }) => valid(gap) && <div key={side}>
      <p className="text-sm"><strong>{gap.observationIntervalS.toFixed(1)} s between observations</strong>
        {' · '}{side}{' · '}{gap.classification === 'likely_reconnect' ? 'Likely reconnect' : 'Continuity unconfirmed'}</p>
      <p className="mt-1 text-xs text-content-muted">
        {formatDateTimeMillis(gap.previousObservationAt)} – {formatDateTimeMillis(gap.nextObservationAt)}.
        {' '}{gap.timestampBasis === 'response_completion' ? 'Recorded reply times.' : 'Recorded row times.'}
        {' '}<a className="text-accent hover:underline" href={`/obd/${encodeURIComponent(direction === 'previous' ? gap.previousDriveId : gap.nextDriveId)}`}>View {direction} recording</a>
      </p>
    </div>)}
    <p className="text-xs text-content-muted">These intervals are separate from gaps within a recording. Distance and fuel are not filled across them.</p>
  </section>
}
