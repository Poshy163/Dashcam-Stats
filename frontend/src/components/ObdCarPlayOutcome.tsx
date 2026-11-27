import { useQuery } from '@tanstack/react-query'

import { ErrorState } from '@/components/ui'
import { api, type CarPlayMilestone, type OBDCarPlayOutcome } from '@/lib/api'
import { formatDateTime } from '@/lib/format'

const outcomes: Record<OBDCarPlayOutcome['status'], string> = {
  carplay_video: 'Saved phone handshake and ZLink video confirmed',
  carplay_session: 'Saved phone handoff and ZLink video session observed. Frame output unconfirmed.',
  zlink_video: 'ZLink decoded video. Saved phone handoff unconfirmed.',
  decoder_observed: 'ZLink opened a video decoder. Video output unconfirmed.',
  phone_handshake: 'Saved phone reached ZLink. Video unconfirmed.',
  saved_phone_joined: 'Saved phone joined the hotspot. Video unconfirmed.',
  unknown: 'CarPlay outcome unknown',
}

const actions = {
  early_retry: 'Reconnect requested',
  waiting_connect: 'Connect requested',
  waiting_cycle: 'Disconnect and reconnect requested',
}

function timing(mark: CarPlayMilestone | null) {
  return mark ? `+${mark.afterIgnitionS.toLocaleString(undefined, { maximumFractionDigits: 1 })} s` : 'Unobserved'
}

export function ObdCarPlayOutcome({ driveId }: { driveId: string }) {
  const query = useQuery({
    queryKey: ['obd-carplay-outcome', driveId],
    queryFn: () => api.obd.carplayOutcome(driveId),
  })
  if (query.isError) return <ErrorState title="Couldn’t load CarPlay outcome" error={query.error} retry={() => query.refetch()} />
  const data = query.data
  return <section className="card p-4 sm:p-5">
    <h2 className="section-title">Wireless CarPlay</h2>
    {!data ? <p className="mt-2 text-sm">Loading CarPlay evidence…</p> : <>
      <p className="mt-2 font-medium">{outcomes[data.status]}</p>
      {!data.ignitionAt ? <p className="mt-1 text-xs text-content-muted">
        The logger could not link this recording to one ignition start.
      </p> : <>
        <p className="mt-1 text-xs text-content-muted">Time since ignition at {formatDateTime(data.ignitionAt)}.</p>
        <dl className="mt-3 grid max-w-xl grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-sm">
          <dt>Hotspot active</dt><dd>{timing(data.hotspot)}</dd>
          <dt>Saved phone received an address</dt><dd>{timing(data.savedPhoneLease)}</dd>
          <dt>Saved phone handshake</dt><dd>{timing(data.phoneHandshake)}</dd>
          <dt>ZLink video decoder start (estimated)</dt><dd>{timing(data.videoSession)}</dd>
          <dt>Phone recovery helper</dt><dd>{data.helper ? `${actions[data.helper.action]} at ${timing(data.helper)}` : 'No recorded request'}</dd>
        </dl>
        {data.videoSession && <p className="mt-2 text-xs text-content-muted">
          The decoder start is calculated from its completed session. The first frame shown on screen was not measured.
        </p>}
      </>}
    </>}
  </section>
}
