import { forwardRef, useRef, useState, type ComponentPropsWithoutRef } from 'react'
import { api } from '@/lib/api'

type Props = Omit<ComponentPropsWithoutRef<'video'>, 'src'> & { recordingId: number }

/** Key by recording ID so each clip starts with its original stream. */
export const RecordingVideo = forwardRef<HTMLVideoElement, Props>(function RecordingVideo(
  { recordingId, onError, onLoadedMetadata, ...props }, ref,
) {
  const [compatible, setCompatible] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const resume = useRef({ time: 0, playing: false })
  const source = api.recordings.streamUrl(recordingId)
  return <>
    <video
      {...props}
      ref={ref}
      src={compatible ? `${source}?compatibility=true` : source}
      onError={(event) => {
        const video = event.currentTarget
        const code = video.error?.code
        if (!compatible && (code === 3 || code === 4)) {
          // MEDIA_ERR_DECODE / MEDIA_ERR_SRC_NOT_SUPPORTED. Network or user
          // cancellations do not indicate that re-encoding will help.
          resume.current = { time: video.currentTime, playing: !video.paused }
          setPreparing(true)
          setCompatible(true)
        } else {
          setPreparing(false)
          onError?.(event)
        }
      }}
      onLoadedMetadata={(event) => {
        setPreparing(false)
        onLoadedMetadata?.(event)
        if (compatible) {
          const video = event.currentTarget
          if (resume.current.time > 0) video.currentTime = Math.min(resume.current.time, video.duration)
          if (resume.current.playing) void video.play().catch(() => undefined)
        }
      }}
    />
    {preparing && <p role="status" className="p-2 text-sm">Preparing compatible video…</p>}
  </>
})
