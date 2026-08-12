/**
 * Typed API client.
 *
 * The backend serialises snake_case; the UI works in camelCase. Rather than maintaining
 * two parallel sets of field names, responses are converted once here on the way in and
 * requests converted on the way out. `types.ts` therefore describes the camelCase shape
 * the components actually see.
 */
import type {
  Heatmap,
  OsdDebug,
  HeatmapFilters,
  RouteFilters,
  Routes,
  Job,
  Journey,
  JourneyDetail,
  LogEntry,
  Paginated,
  Plate,
  PlateObservation,
  QueueStats,
  Recording,
  ReprocessResult,
  RetentionPlan,
  SafetyReport,
  SearchResults,
  SettingCategory,
  Status,
  TelemetryPoint,
  TelemetryQuality,
  TrackedObject,
  Vehicle,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

const snakeToCamel = (s: string) => s.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase())
const camelToSnake = (s: string) => s.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`)

function convertKeys<T>(value: unknown, transform: (key: string) => string): T {
  if (Array.isArray(value)) {
    return value.map((v) => convertKeys(v, transform)) as T
  }
  if (value !== null && typeof value === 'object') {
    // Date instances and the like would be flattened into plain objects by the generic
    // branch below, so only plain objects are walked.
    if (Object.getPrototypeOf(value) !== Object.prototype) return value as T
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value)) {
      out[transform(k)] = convertKeys(v, transform)
    }
    return out as T
  }
  return value as T
}

/** Settings keys are dotted identifiers owned by the backend and must survive verbatim. */
function convertPreservingKeys<T>(value: unknown): T {
  return value as T
}

type Query = Record<string, string | number | boolean | null | undefined>

function buildQuery(params?: Query): string {
  if (!params) return ''
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(camelToSnake(key), String(value))
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

async function request<T>(
  path: string,
  options: RequestInit & { query?: Query; rawKeys?: boolean } = {},
): Promise<T> {
  const { query, rawKeys, ...init } = options
  const response = await fetch(`/api${path}${buildQuery(query)}`, {
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    ...init,
  })

  if (!response.ok) {
    let detail: unknown
    let message = `${response.status} ${response.statusText}`
    try {
      detail = await response.json()
      const d = detail as { detail?: unknown; message?: string }
      if (typeof d?.detail === 'string') message = d.detail
      else if (typeof d?.message === 'string') message = d.message
    } catch {
      /* a non-JSON error body is still an error; keep the status line */
    }
    throw new ApiError(message, response.status, detail)
  }

  if (response.status === 204) return undefined as T
  const json = await response.json()
  return rawKeys ? convertPreservingKeys<T>(json) : convertKeys<T>(json, snakeToCamel)
}

function post<T>(path: string, body?: unknown, opts: { rawKeys?: boolean } = {}): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(convertKeys(body, camelToSnake)),
    ...opts,
  })
}

/** What POST /api/scan reports back, so the Settings page can summarise the run. */
export interface ScanResult {
  scanId: number | null
  seen: number
  new: number
  changed: number
  unsettled: number
  missing: number
  queued: number
  errors: number
  damagedHidden: number
  damagedDeleted: number
  damagedDeleteBlocked: number
  damagedRestored: number
  errorMessage: string | null
}

/** Live progress of a pull off the head unit. Also the Home Assistant REST sensor shape. */
export interface IngestStatus {
  state:
    | 'disabled'
    | 'idle'
    | 'running'
    | 'ok'
    | 'partial'
    | 'error'
    | 'offline'
    | 'unauthorized'
    | 'cancelled'
  unitOnline: boolean
  filesTotal: number
  filesDone: number
  bytesTotal: number
  bytesDone: number
  throughputMbs: number
  currentFile: string | null
  backlogFiles: number
  backlogBytes: number
  lastSuccessTs: string | null
  lastError: string | null
}

export interface IngestRun {
  id: number
  startedAt: string | null
  finishedAt: string | null
  trigger: string
  state: string
  filesTransferred: number
  bytesTransferred: number
  throughputMbsAvg: number
  error: string | null
}

export interface RecordingFilters extends Query {
  page?: number
  pageSize?: number
  cameraId?: number
  journeyId?: number
  state?: string
  hasGps?: boolean
  hasDetections?: boolean
  dateFrom?: string
  dateTo?: string
  search?: string
  sort?: string
}

export const api = {
  status: () => request<Status>('/status'),
  health: () => fetch('/health').then((r) => r.json()),

  recordings: {
    list: (filters?: RecordingFilters) =>
      request<Paginated<Recording>>('/recordings', { query: filters }),
    get: (id: number) => request<Recording>(`/recordings/${id}`),
    telemetry: (id: number) => request<TelemetryPoint[]>(`/recordings/${id}/telemetry`),
    /** What the overlay reader saw at this instant, and what it made of it. */
    osdDebug: (id: number, t: number) =>
      request<OsdDebug>(`/recordings/${id}/osd-debug`, { query: { t } }),
    /** The rendered composite: frame, cropped strip, and the thresholded mask. */
    osdDebugImage: (id: number, t: number) =>
      `/api/recordings/${id}/osd-debug.png?t=${encodeURIComponent(t)}`,
    detections: (id: number) => request<TrackedObject[]>(`/recordings/${id}/detections`),
    plates: (id: number) => request<PlateObservation[]>(`/recordings/${id}/plates`),
    reprocess: (id: number, stages: string[]) =>
      post<{ jobId: number }>(`/recordings/${id}/reprocess`, { stages }),
    updateEvent: (id: number, patch: { protected?: boolean; eventType?: string | null; eventNotes?: string | null }) =>
      request<Recording>(`/recordings/${id}/event`, {
        method: 'PATCH',
        body: JSON.stringify(convertKeys(patch, camelToSnake)),
      }),
    exportClipUrl: (id: number, start = 0, end?: number) =>
      `/api/recordings/${id}/export.mp4${buildQuery({ start, end })}`,
    exportMetadataUrl: (id: number) => `/api/recordings/${id}/export.json`,
    /** Streamed with HTTP range support so the player can seek. */
    streamUrl: (id: number) => `/stream/${id}`,
  },

  journeys: {
    list: (query?: Query) => request<Paginated<Journey>>('/journeys', { query }),
    get: (id: number) => request<JourneyDetail>(`/journeys/${id}`),
    merge: (journeyIds: number[]) => post<Journey>('/journeys/merge', { journeyIds }),
    split: (id: number, atRecordingId: number) =>
      post<Journey[]>(`/journeys/${id}/split`, { atRecordingId }),
    reprocess: (id: number, stages: string[]) =>
      post<{ queued: number }>(`/journeys/${id}/reprocess`, { stages }),
  },

  plates: {
    /** `q` does partial matching: "ABC" matches "ABC123". */
    list: (query?: Query & { q?: string }) => request<Paginated<Plate>>('/plates', { query }),
    get: (id: number) => request<Plate>(`/plates/${id}`),
    observations: (id: number, query?: Query) =>
      request<Paginated<PlateObservation>>(`/plates/${id}/observations`, { query }),
    update: (id: number, patch: { flagged?: boolean; notes?: string; dismissed?: boolean }) =>
      request<Plate>(`/plates/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(convertKeys(patch, camelToSnake)),
      }),
    correct: (id: number, text: string) => post<Plate>(`/plates/${id}/correct`, { text }),
    merge: (id: number, targetPlateId: number) =>
      post<Plate>(`/plates/${id}/merge`, { targetPlateId }),
  },

  vehicles: {
    list: (query?: Query) => request<Paginated<Vehicle>>('/vehicles', { query }),
    get: (id: number) => request<Vehicle>(`/vehicles/${id}`),
  },

  map: {
    /** Grid-aggregated fixes, ready for a heat layer. See the `Heatmap` type. */
    heatmap: (filters?: HeatmapFilters) => request<Heatmap>('/map/heatmap', { query: filters }),
    /** The paths themselves, split at signal gaps. See the `Routes` type. */
    routes: (filters?: RouteFilters) => request<Routes>('/map/routes', { query: filters }),
  },

  jobs: {
    list: (query?: Query) => request<Paginated<Job>>('/jobs', { query }),
    stats: () => request<QueueStats>('/jobs/stats'),
    pause: () => post<QueueStats>('/jobs/pause'),
    resume: () => post<QueueStats>('/jobs/resume'),
    retryFailed: () => post<{ retried: number }>('/jobs/retry-failed'),
    retry: (id: number) => post<Job>(`/jobs/${id}/retry`),
    cancel: (id: number) => request<void>(`/jobs/${id}`, { method: 'DELETE' }),
  },

  ingest: {
    status: () => request<IngestStatus>('/ingest/status'),
    run: () => post<{ started: boolean; state: string }>('/ingest/run'),
    cancel: () => post<{ cancelled: boolean }>('/ingest/cancel'),
    history: (query?: Query) => request<Paginated<IngestRun>>('/ingest/history', { query }),
  },

  settings: {
    // Setting keys are dotted backend identifiers ("storage.max_footage_gb"); converting
    // them to camelCase would break the round-trip, so these calls keep keys verbatim.
    get: () => request<SettingCategory[]>('/settings', { rawKeys: true }),
    update: (values: Record<string, unknown>) =>
      request<SettingCategory[]>('/settings', {
        method: 'PUT',
        body: JSON.stringify({ values }),
        rawKeys: true,
      }),
    reset: (keys: string[]) => post<SettingCategory[]>('/settings/reset', { keys }, { rawKeys: true }),
  },

  scan: {
    now: () => post<ScanResult>('/scan'),
    processNew: () => post<{ queued: number }>('/process'),
    /**
     * Rebuild the queue from the footage, or requeue a targeted subset of it.
     *
     * With neither flag set this is a full reset: the queue is emptied, in-flight runs are
     * stopped, and it is rebuilt thumbnails-first then oldest-to-newest. The two flags are
     * targeted repairs and leave the rest of the queue alone.
     */
    reprocessAll: (stages: string[], onlyFailed = false, onlyOutdated = false) =>
      post<ReprocessResult>('/reprocess', { stages, onlyFailed, onlyOutdated }),
  },

  telemetryQuality: () => request<TelemetryQuality>('/telemetry/quality'),

  retention: {
    plan: () => post<RetentionPlan>('/retention/plan'),
    run: () => post<RetentionPlan>('/retention/run'),
    safety: () => request<SafetyReport>('/retention/safety'),
    history: (query?: Query) => request<Paginated<unknown>>('/retention/history', { query }),
  },

  logs: {
    list: (query?: Query) => request<Paginated<LogEntry>>('/logs', { query }),
  },

  search: (q: string) => request<SearchResults>('/search', { query: { q } }),

  system: {
    hardware: () => request<Status['hardware']>('/system/hardware'),
    info: () => request<Record<string, unknown>>('/system/info'),
    database: () => request<Record<string, unknown>>('/system/database'),
    backupUrl: () => '/api/system/database/backup',
    restore: (file: File) =>
      fetch('/api/system/database/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      }).then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new ApiError(data.detail ?? 'Restore failed', response.status, data)
        return convertKeys<{ validated: boolean; restartRequired: boolean; message: string }>(data, snakeToCamel)
      }),
  },
}

/** Resolve a stored media path (thumbnail, crop) to a URL the browser can fetch. */
export function mediaUrl(path: string | null | undefined): string | undefined {
  if (!path) return undefined
  return `/media/${path.replace(/^\/+/, '')}`
}
