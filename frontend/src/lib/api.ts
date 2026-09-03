/**
 * Typed API client.
 *
 * The backend serialises snake_case; the UI works in camelCase. Rather than maintaining
 * two parallel sets of field names, responses are converted once here on the way in and
 * requests converted on the way out. `types.ts` therefore describes the camelCase shape
 * the components actually see.
 */
import type {
  AuthSession,
  AuthState,
  CarPlayTimingOut,
  Heatmap,
  HeatmapFilters,
  Job,
  Journey,
  JourneyDetail,
  LogEntry,
  OsdDebug,
  Paginated,
  Plate,
  PlateObservation,
  QueueStats,
  Recording,
  ReprocessResult,
  RetentionPlan,
  RouteFilters,
  Routes,
  SafetyReport,
  SearchResults,
  SettingCategory,
  Status,
  TelemetryPoint,
  TelemetryQuality,
  TrackedObject,
  UnitLogEntry,
  UnitLogTag,
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

/**
 * Called whenever the API answers 401, so the app can put the login page back up.
 *
 * A hook here rather than in the query client because not every call is a query — a
 * mutation firing after a thirty-day session finally expired has to reach it too, and this
 * is the one place every request already passes through.
 */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler
}

async function request<T>(
  path: string,
  options: RequestInit & { query?: Query; rawKeys?: boolean } = {},
): Promise<T> {
  const { query, rawKeys, ...init } = options
  const response = await fetch(`/api${path}${buildQuery(query)}`, {
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
    // Explicit rather than relying on the default. The session cookie is what
    // authenticates every one of these, and a silent change of default here would log
    // everyone out with no obvious cause.
    credentials: 'same-origin',
    ...init,
  })

  if (response.status === 401) onUnauthorized?.()

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
  /**
   * Where a running transfer currently is. Separate from `state`, which says how a run
   * *ended* and is also what the Home Assistant sensor publishes.
   */
  phase: 'idle' | 'connecting' | 'scanning' | 'preparing' | 'transferring' | 'verifying'
  unitOnline: boolean
  filesTotal: number
  filesDone: number
  bytesTotal: number
  bytesDone: number
  /** Average across the whole run, including the seconds before any bytes moved. */
  throughputMbs: number
  /** Rate over the last few seconds — the one to show while somebody is watching. */
  speedMbsRecent: number
  /** Null unless bytes are actually moving; an estimate is not offered otherwise. */
  etaSeconds: number | null
  currentFile: string | null
  backlogFiles: number
  backlogBytes: number
  /** False until this process has actually listed the card; zero is not evidence yet. */
  backlogKnown: boolean
  /** Recordings left alone because the camera is still writing them. */
  activeSkipped: number
  /** The unit's WiFi frequency in MHz as last read by the band gate; null when unknown. */
  wifiFrequencyMhz: number | null
  /** True while a transfer is being held waiting for a 5 GHz link. */
  wifiBandHold: boolean
  /** Why it is being held, including whether 5 GHz is in range. Null unless held. */
  wifiBandHoldReason: string | null
  /** The unit's running time in seconds as last read by the arrival gate; null if unknown. */
  unitUptimeS: number | null
  /** True while the first pull is being held until the unit has been running long enough. */
  arrivalHold: boolean
  /** Why the pull is being held for arrival. Null unless held. */
  arrivalHoldReason: string | null
  /** True while the pull is being held because the ignition is on and CarPlay would be cut. */
  ignitionHold: boolean
  /** Why it is being held for the ignition. Null unless held. */
  ignitionHoldReason: string | null
  /** The recording watcher's last verdict in one line; null until it has collected once. */
  recorderHealth: string | null
  /** Whether that verdict was clean. Null when no verdict yet. */
  recorderHealthOk: boolean | null
  /** When the verdict was collected. Null when no verdict yet. */
  recorderHealthAt: string | null
  startedAt: string | null
  lastSuccessTs: string | null
  lastError: string | null
}

export interface IngestRadioDeviceState {
  /** Exact state observed before the transition; transport means the hotspot carries ADB. */
  baseline: 'on' | 'off' | 'transport' | 'unknown'
  disableAttempted: boolean
  disableVerified: boolean
  restoreAttempted: boolean
  restoreVerified: boolean
}

export interface IngestRadioTransitionStatus {
  /** Durable state-machine phase. Kept open so an older UI can render a newer phase. */
  phase: string
  active: boolean
  recoveryRequired: boolean
  createdAt: string
  updatedAt: string
  completedAt: string | null
  bluetooth: IngestRadioDeviceState
  hotspot: IngestRadioDeviceState
  obdLogger: {
    quiesceCapable: boolean
    quiesceAttempted: boolean
    quiesceVerified: boolean
    resumeAttempted: boolean
    resumeVerified: boolean
  }
}

/** Durable safety evidence for the current, or most recent, radio transition. */
export interface IngestRadioStatus {
  quietingEnabled: boolean
  transition: IngestRadioTransitionStatus | null
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

export interface OBDLoggerStatus {
  schemaVersion?: number
  loggerVersion?: string
  appVersionName?: string
  appVersionCode?: number
  pollPlanVersion?: number
  buildGitSha?: string
  capabilities?: string[]
  state?: string
  ownershipEnabled?: boolean
  adapterState?: string
  adapterReachable?: boolean
  adapterConnected?: boolean
  ecuConnected?: boolean
  engineRunning?: boolean
  vehicleState?: string
  batteryVoltage?: number | null
  batteryVoltageSource?: 'dashcam_elm_atrv' | string | null
  batteryVoltageSampleAtUtc?: string | null
  batteryVoltageFresh?: boolean
  batteryVoltageRawResponse?: string | null
  batteryVoltageQuality?: 'valid' | 'stale' | 'invalid' | 'failed' | 'unavailable' | string
  bleOwner?:
    | 'unowned'
    | 'dashcam_voltage_only'
    | 'dashcam_full_obd'
    | 'home_assistant_voltage_only'
    | 'phone_reserved'
    | 'transitioning'
    | 'conflict_detected'
    | string
  headUnitState?: 'awake' | 'sleep_requested' | 'asleep' | 'unknown' | string
  voltageOnlyMode?: boolean
  wifiConnected?: boolean
  accStateKnown?: boolean
  accOn?: boolean
  ingestionSleepHoldKnown?: boolean
  ingestionSleepHold?: boolean
  sleepWindowPolicy?: string
  sleepWindowTargetS?: number | null
  sleepWindowObservedS?: number | null
  sleepWindowVerified?: boolean
  sleepWindowError?: string | null
  currentDriveId?: string | null
  lastDriveId?: string | null
  lastDriveFinishedAtUtc?: string | null
  pendingBundleCount?: number
  sampleCount?: number
  lastError?: string | null
  lastErrorAtUtc?: string | null
  updatedAtUtc?: string | null
}

export interface OBDBundle {
  id: number
  driveId: string
  schemaVersion: number
  bundleSha256: string
  filename: string
  sizeBytes: number
  vehicleId: string
  driveStartedAt: string
  driveFinishedAt: string
  sampleCount: number
  diagnosticCount: number
  metadataTrusted: boolean
  state: string
  attempts: number
  nextAttemptAt: string | null
  lastError: string | null
  failureKind: string | null
  lastHttpStatus: number | null
  verifiedAt: string | null
  importedAt: string | null
  duplicate: boolean
  warnings: string[]
}

export interface OBDDriveSummary {
  driveId: string
  vehicleId: string
  startedAt: string
  finishedAt: string
  originalTimezone: string | null
  startReason: string | null
  stopReason: string | null
  obdProtocol: string | null
  completionStatus: string
  producerCompletionStatus: string
  lifecycleStatus: 'complete' | 'interrupted' | 'recovered' | string
  cleanEnd: boolean
  interruptionReason: string | null
  firstSampleAt: string | null
  lastSampleAt: string | null
  lastSuccessfulResponseAt: string | null
  finalizationObservedAt: string | null
  connectionLossCount: number
  gapCount: number
  longestGapS: number | null
  dataCompletenessPercentage: number | null
  processingStatus: string
  lastProcessingError: string | null
  summarySource: 'producer' | 'derived' | string
  summaryGeneratedAt: string | null
  durationS: number | null
  distanceKm: number | null
  averageSpeedKmh: number | null
  maximumSpeedKmh: number | null
  averageRpm: number | null
  maximumRpm: number | null
  idleDurationS: number | null
  estimatedFuelUsedL: number | null
  averageFuelConsumptionL100km: number | null
  maximumCoolantTemperatureC: number | null
  maximumEngineLoadPct: number | null
  missingDataDurationS: number | null
  expectedSampleCount: number
  receivedSamplePercentage: number | null
  sampleCount: number
  errorCount: number
  dtcsObserved: string[]
  bundleId: number
  bundleFilename: string
  bundleSha256: string
  bundleAvailable: boolean
  bundleDownloadUrl: string | null
  exportStatus: string
  backupStatus: string
  copiedAt: string | null
  verifiedAt: string | null
  importedAt: string | null
  importState: string
  bundleError: string | null
  validationWarnings: string[]
  gapAnalysis?: OBDGapAnalysis | null
}

export interface OBDCadenceQuality {
  observationCount: number
  firstObservedAt: string | null
  lastObservedAt: string | null
  expectedCadenceS: number
  gapThresholdS: number
  medianCadenceS: number | null
  p95CadenceS: number | null
  p99CadenceS: number | null
  maximumCadenceS: number | null
  cadenceIsSampled: boolean
  gapCount: number
  totalGapDurationS: number
  longestGapS: number | null
  outOfOrderCount: number
  gaps: { startAt: string; endAt: string; durationS: number; excessS: number }[]
  gapsTruncated: boolean
}

export interface OBDSignalQuality extends OBDCadenceQuality {
  name: string
  label: string
  pid: string | null
  tier: 'fast' | 'medium' | 'slow' | string
  provenance: 'measured' | 'derived' | string
  discrete: boolean
  supported: boolean
  expectedObservationCount: number
  receivedObservationCount: number
  missingObservationCount: number
  missingRunCount: number
  longestMissingRun: number
  coveragePercentage: number | null
}

export interface OBDGapAnalysis {
  schemaVersion: number
  pollPlanVersion: number
  nominalCycleS: number
  gapTolerance: number
  supportedPidSource: string
  supportedPids: string[]
  transport: OBDCadenceQuality & {
    expectedObservationCount: number
    receivedObservationCount: number
    sequenceGapCount: number
    coveragePercentage: number | null
  }
  signals: OBDSignalQuality[]
  aggregateSignalCompletenessPercentage: number | null
}

export interface OBDDrivesTotals {
  driveCount: number
  totalDistanceKm: number
  totalDurationS: number
  totalIdleDurationS: number
  totalFuelUsedL: number
  averageFuelConsumptionL100km: number | null
  maximumSpeedKmh: number | null
  maximumRpm: number | null
  maximumCoolantTemperatureC: number | null
  totalSampleCount: number
  firstDriveAt: string | null
  lastDriveAt: string | null
}

export interface OBDSeriesSample {
  sampleId: string
  t: string
  sequence: number
  ecuDataStatus: 'live' | 'last_known' | string
  quality: { transport?: string; parser?: string; missingPids?: number[] } | null
  provenance: Record<string, 'measured' | 'derived' | string>
  engineRpm: number | null
  vehicleSpeedKmh: number | null
  coolantTemperatureC: number | null
  intakeAirTemperatureC: number | null
  engineLoadPct: number | null
  throttlePositionPct: number | null
  timingAdvanceDeg: number | null
  massAirFlowGS: number | null
  shortTermFuelTrimPct: number | null
  longTermFuelTrimPct: number | null
  oxygenSensor1VoltageV: number | null
  oxygenSensor2VoltageV: number | null
  adapterVoltageV: number | null
  estimatedFuelRateLH: number | null
  estimatedFuelConsumptionL100km: number | null
}

export interface OBDSignalMetadata {
  name: string
  label: string
  pid: string | null
  tier: string
  expectedCadenceS: number
  provenance: 'measured' | 'derived' | string
  discrete: boolean
}

export interface OBDDriveSeries {
  drive: OBDDriveSummary
  journey: { id: number; title: string | null; overlapS: number } | null
  units: Record<string, string>
  signalMetadata: OBDSignalMetadata[]
  samples: OBDSeriesSample[]
  diagnostics: { observedAt: string | null; kind: string; payload: Record<string, unknown> }[]
}

export interface OBDStatus {
  logger: OBDLoggerStatus | null
  loggerCheckedAt: string | null
  eventStream: {
    available: boolean
    checkedAt: string | null
    lastReceivedAt: string | null
    accepted: number
    duplicates: number
    sequenceGap: number
    lastError: string | null
  }
  waitingOnUnit: number
  currentBundleCopy: string | null
  lastCopyAt: string | null
  lastCopyError: string | null
  copyThroughputMbs: number
  homeAssistantAuthentication: 'configured' | 'invalid' | 'not_configured'
  homeAssistantConfigurationError: string | null
  counts: Record<string, number>
  waitingForHomeAssistant: number
  currentImport: string | null
  lastCompletedDrive: OBDBundle | null
  importedDriveCount: number
  duplicateCount: number
  failedCount: number
  lastSuccessfulHomeAssistantSync: string | null
  lastImportError: string | null
  importsLastHour: number
  workerRunning: boolean
}

export interface OBDLoggerEvent {
  sequence: number
  occurredAt: string
  receivedAt: string
  kind:
    | 'app.boot'
    | 'app.service'
    | 'network.wifi'
    | 'power.sleep_window'
    | 'obd.ble_connection'
    | 'obd.elm_session'
    | 'obd.ecu_session'
    | 'obd.poll_health'
    | 'drive.lifecycle'
    | 'ingest.handoff'
    | 'radio.observation'
    | 'bundle.export'
    | 'receipt.verification'
    | string
  level: 'info' | 'warning' | 'error'
  outcome: string
  reasonCode: string | null
  driveId: string | null
  metrics: Record<string, number>
  appVersionName: string
  appVersionCode: number
  buildGitSha: string
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

  auth: {
    /** Reachable signed out — it is how the app finds out that it is signed out. */
    state: () => request<AuthState>('/auth/state'),
    login: (username: string, password: string, remember: boolean) =>
      post<{ authenticated: boolean; username: string; expiresAt: string }>('/auth/login', {
        username,
        password,
        remember,
      }),
    logout: () => post<void>('/auth/logout'),
    /** Creates the account, or changes it. Signs every other browser out either way. */
    setCredential: (username: string, password: string, currentPassword?: string) =>
      request<void>('/auth/credential', {
        method: 'PUT',
        body: JSON.stringify(
          convertKeys({ username, password, currentPassword }, camelToSnake),
        ),
      }),
    /** Deletes the account and switches sign-in off with it. */
    clearCredential: (currentPassword?: string) =>
      post<void>('/auth/credential/clear', { currentPassword }),
    sessions: () => request<AuthSession[]>('/auth/sessions'),
    revokeSession: (id: number) => request<void>(`/auth/sessions/${id}`, { method: 'DELETE' }),
    revokeOtherSessions: () => post<{ revoked: number }>('/auth/sessions/revoke-all'),
  },

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
    radioStatus: () => request<IngestRadioStatus>('/ingest/radio-status'),
    run: () => post<{ started: boolean; state: string }>('/ingest/run'),
    cancel: () => post<{ cancelled: boolean }>('/ingest/cancel'),
    // The URL comes back with the API key masked — it is rendered straight into the page.
    showTest: () => post<{ shown: boolean; url: string }>('/ingest/show-test'),
    history: (query?: Query) => request<Paginated<IngestRun>>('/ingest/history', { query }),
  },

  obd: {
    status: () => request<OBDStatus>('/obd/status'),
    events: (query?: Query) => request<Paginated<OBDLoggerEvent>>('/obd/events', { query }),
    bundles: (query?: Query) => request<Paginated<OBDBundle>>('/obd/bundles', { query }),
    drives: (query?: Query) => request<Paginated<OBDDriveSummary>>('/obd/drives', { query }),
    drivesSummary: () => request<OBDDrivesTotals>('/obd/drives/summary'),
    driveSeries: (driveId: string) =>
      request<OBDDriveSeries>(`/obd/drives/${encodeURIComponent(driveId)}/series`),
    driveForJourney: (journeyId: number) =>
      request<{ drive: OBDDriveSummary | null; overlapS: number | null }>(
        `/obd/drives/for-journey/${journeyId}`,
      ),
    reprocessDrive: (driveId: string) =>
      post<{ result: Record<string, unknown>; drive: OBDDriveSummary }>(
        `/obd/drives/${encodeURIComponent(driveId)}/reprocess`,
      ),
    validate: (id: number) =>
      post<{ valid: boolean; bundle: OBDBundle }>(`/obd/bundles/${id}/validate`),
    retry: (id: number) =>
      post<{ queued: boolean; alreadyImported?: boolean; bundle: OBDBundle }>(
        `/obd/bundles/${id}/retry`,
      ),
    rebuild: () =>
      post<{
        recoveredImports: number
        registered: number
        duplicates: number
        quarantined: number
      }>('/obd/queue/rebuild'),
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

  unitLogs: {
    list: (query?: Query) => request<Paginated<UnitLogEntry>>('/unit-logs', { query }),
    tags: () => request<UnitLogTag[]>('/unit-logs/tags'),
    carplayTiming: (query?: Query) =>
      request<CarPlayTimingOut>('/unit-logs/carplay-timing', { query }),
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
