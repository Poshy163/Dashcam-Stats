/**
 * API response shapes, mirroring backend/app/api/schemas.py.
 *
 * A note that matters throughout: this dashcam writes no telemetry metadata, so GPS and
 * speed are recovered by OCR of a burned-in overlay. Every value derived from it carries a
 * confidence, and `hasFix` is a real distinction — the camera prints `E:00.0000 N:00.0000`
 * when it has no satellite lock, which is not a coordinate. Anything that renders a
 * position must check `hasFix` rather than testing for non-null numbers.
 */

/**
 * `settling` and `invalid` are the two ends of "not being processed, and that is fine".
 * A settling file is still being written and will be picked up on a later scan; an invalid
 * one is empty or has no video stream and never will be. `failed` sits between them: it
 * was tried, it did not work, and it will be tried again.
 */
export type RecordingState =
  | 'discovered'
  | 'settling'
  | 'metadata_extracted'
  | 'queued'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'invalid'
  | 'ignored'
  | 'deleted'

export type StageState = 'pending' | 'running' | 'done' | 'failed' | 'skipped'
export type JobState = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export type CameraRole = 'front' | 'rear' | 'cabin' | 'other'

export interface Camera {
  id: number
  key: string
  name: string
  role: CameraRole
}

export interface Recording {
  id: number
  filename: string
  relPath: string
  camera: Camera | null
  journeyId: number | null

  startedAt: string | null
  endedAt: string | null
  durationS: number | null
  /** True when the time came from the decoded overlay rather than the filename. */
  timeFromOsd: boolean

  sizeBytes: number
  videoCodec: string | null
  width: number | null
  height: number | null
  fps: number | null
  hasAudio: boolean

  state: RecordingState
  metadataState: StageState
  telemetryState: StageState
  detectionState: StageState
  plateState: StageState
  metadataRevision: string | null
  telemetryRevision: string | null
  detectionRevision: string | null
  plateRevision: string | null
  errorMessage: string | null

  hasGps: boolean
  gpsPointCount: number
  distanceM: number | null
  avgSpeedKmh: number | null
  maxSpeedKmh: number | null
  gpsGapCount: number
  gpsLongestGapS: number
  gpsRecoveredCount: number
  gpsNoFixCount: number
  gpsOcrGapCount: number
  gpsRejectedCount: number
  telemetryProblemCount: number

  vehicleCount: number
  plateCount: number
  thumbnailPath: string | null
  /** The row is kept after the file goes, so history survives retention cleanup. */
  fileMissing: boolean
  /** Hidden from normal library views by the damaged-footage policy. */
  ignored: boolean
  protected: boolean
  eventType: string | null
  eventNotes: string | null

  /** Problems in the source file itself — a wrapped PTS, frames that will not decode. */
  warnings: string[]
  /** True when the camera wrote a damaged file. Not an application error. */
  sourceDamaged: boolean
}

export interface TelemetryPoint {
  tOffsetS: number
  capturedAt: string | null
  lat: number | null
  lon: number | null
  /** False means the camera had no satellite lock; lat/lon are null and must not be plotted. */
  hasFix: boolean
  speedKmh: number | null
  /** Derived from consecutive fixes — a bearing, not a compass reading. */
  headingDeg: number | null
  ocrConfidence: number | null
  rawText: string | null
  quality: TelemetryQuality
}

export interface TelemetryQuality {
  source?: string
  ocrStatus?: string
  timeStatus?: string
  timeSource?: string
  overlayCapturedAt?: string | null
  expectedAt?: string | null
  timeDeltaS?: number | null
  gpsStatus?: string
  gpsSource?: string
  interpolated?: boolean
  candidateCount?: number
  problems?: string[]
}

export interface Journey {
  id: number
  title: string | null
  startedAt: string
  endedAt: string
  durationS: number
  distanceM: number | null
  avgSpeedKmh: number | null
  maxSpeedKmh: number | null
  hasGps: boolean
  recordingCount: number
  vehicleCount: number
  uniquePlateCount: number
  startLat: number | null
  startLon: number | null
  endLat: number | null
  endLon: number | null
  manual: boolean
}

/** One fix on a route: where it was, and which clip and moment it came from. */
export type RoutePoint = [lat: number, lon: number, recordingId: number, offsetS: number]

export interface JourneyDetail extends Journey {
  recordings: Recording[]
  /**
   * Drawable segments. Separate lines rather than one, because the breaks between them
   * are real — lost signal or a gap between clips — and joining across them draws a road
   * that was never taken.
   */
  route: RoutePoint[][]
}

export interface TrackedObject {
  id: number
  recordingId: number
  classLabel: string
  confidenceMax: number
  firstSeenOffsetS: number
  lastSeenOffsetS: number
  durationS: number
  frameCount: number
  lat: number | null
  lon: number | null
  cropPath: string | null
}

export interface Plate {
  id: number
  /** Normalised for matching and display. */
  displayText: string
  normalisedText: string
  /** Null when OCR output matched no known Australian format — shown as uncertain. */
  patternName: string | null
  region: string | null
  firstSeenAt: string | null
  lastSeenAt: string | null
  observationCount: number
  journeyCount: number
  bestConfidence: number
  flagged: boolean
  dismissed: boolean
  notes: string | null
  representativeCropPath: string | null
  representativeVehiclePath: string | null
}

export interface PlateObservation {
  id: number
  plateId: number
  recordingId: number
  recordingFilename: string
  journeyId: number | null
  cameraName: string | null
  tOffsetS: number
  capturedAt: string | null
  /** Unmodified OCR output, always kept beside the normalised value. */
  rawText: string
  normalisedText: string
  ocrConfidence: number
  detectionConfidence: number
  voteCount: number
  lat: number | null
  lon: number | null
  plateCropPath: string | null
  vehicleCropPath: string | null
}

export interface Vehicle {
  id: number
  classLabel: string | null
  primaryPlate: Plate | null
  /** Null unless a classifier actually produced them — never inferred. */
  make: string | null
  model: string | null
  colour: string | null
  classifier: string | null
  firstSeenAt: string | null
  lastSeenAt: string | null
  observationCount: number
  representativeCropPath: string | null
  /** Where the sighting came from, so the crop leads back to the footage. */
  recordingId: number | null
  firstSeenOffsetS: number | null
  lat: number | null
  lon: number | null
}

export interface Job {
  id: number
  recordingId: number | null
  recordingFilename: string | null
  kind: string
  stages: string[] | null
  state: JobState
  progress: number
  stageCurrent: string | null
  speedRealtime: number | null
  decoder: string | null
  inferenceDevice: string | null
  resourceState: string | null
  attempts: number
  maxAttempts: number
  queuedAt: string
  startedAt: string | null
  finishedAt: string | null
  errorMessage: string | null
}

export interface QueueStats {
  queued: number
  running: number
  failed: number
  completedToday: number
  /** Waiting or running thumbnail jobs. Already counted in `queued`/`running`. */
  thumbnailsPending: number
  /** Which pass the run is working through. */
  phase: 'thumbnails' | 'analysis' | 'idle'
  paused: boolean
  throughputPerHour: number | null
  estimatedMinutesRemaining: number | null
}

/** What "reprocess all footage" did. A full run resets the queue; the targeted ones add to it. */
export interface ReprocessResult {
  queued: number
  stages: string[]
  reset?: boolean
  recordings?: number
  thumbnailsQueued?: number
  analysisQueued?: number
  cleared?: Partial<Record<JobState, number>>
  aborted?: number
  paused?: boolean
}

export interface TelemetryQualityRecording {
  recordingId: number
  filename: string
  startedAt: string | null
  points: number
  fixes: number
  gaps: number
  longestGapS: number
  recovered: number
  realGpsLoss: number
  ocrUnreadable: number
  rejected: number
  problems: number
  status: 'healthy' | 'degraded' | 'no_fix' | 'pending'
}

export interface TelemetryQuality {
  recordings: number
  healthy: number
  degraded: number
  noFix: number
  pending: number
  totalGaps: number
  pairedRecoveries: number
  issues: TelemetryQualityRecording[]
}

export interface HardwareInfo {
  gpu: {
    name: string | null
    vendor: string | null
    driver: string | null
    renderNodes: string[]
  }
  decode: {
    vaapiAvailable: boolean
    vaapiCodecs: string[]
    hardwareDecode: boolean
  }
  inference: {
    openvinoAvailable: boolean
    openvinoDevices: string[]
    preferredDevice: string
    backendName?: string
  }
  cpu: { model: string | null; cores: number }
  ffmpeg: { version: string | null }
  /** Human-readable diagnostics, e.g. "/dev/dri mapped but not accessible to uid 1000". */
  notes: string[]
}

/**
 * Whether an optional analysis feature can actually run, and whether it has.
 *
 * Exists so the UI can tell two very different zeroes apart. "0 vehicles seen" across 674
 * recordings reads as a finding — the roads were empty — when the truth was that the
 * detection models could not be downloaded and the stage never ran. A count only means
 * something once you know the thing producing it was working.
 */
export interface FeatureStatus {
  key: string
  label: string
  enabled: boolean
  ready: boolean
  blockedReason: string | null
  results: number
}

export interface Status {
  totals: {
    recordings: number
    journeys: number
    telemetryPoints: number
    trackedObjects: number
    plates: number
    footageBytes: number
    footageFiles: number
    durationS: number
  }
  features?: FeatureStatus[]
  processing: {
    completed: number
    pending: number
    processing: number
    failed: number
    /** Empty, or carrying no video stream. Never retried; see RecordingState. */
    invalid: number
    /** Still being written, so not yet work that is waiting. */
    settling: number
    recordingsToday: number
    throughputPerHour: number | null
  }
  storage: {
    limitBytes: number
    usedBytes: number
    deletionEnabled: boolean
    footageWritable: boolean
  }
  latestJourney: Journey | null
  hardware: HardwareInfo
  version: string
  /** The camera's timezone, used to render every timestamp in the UI. */
  timezone?: string
}

export interface LogEntry {
  id: number
  ts: string
  level: string
  logger: string
  message: string
  context: Record<string, unknown> | null
  recordingId: number | null
  jobId: number | null
}

export type SettingType = 'bool' | 'int' | 'float' | 'string' | 'select' | 'path' | 'bytes'

export interface SettingDef {
  key: string
  label: string
  type: SettingType
  value: unknown
  default: unknown
  description: string
  minimum: number | null
  maximum: number | null
  choices: { value: string; label: string }[]
  unit: string | null
  dangerous: boolean
  /** Key of a boolean setting that gates this one; used to grey out dependents. */
  requires: string | null
  // snake_case on purpose. Unlike every other response, settings are fetched with
  // `rawKeys` so the dotted setting identifiers survive the round trip, which means these
  // two arrive exactly as the backend spells them. Declaring them camelCase made both
  // permanently `undefined`, and `!setting.isDefault` therefore always true — so a "reset
  // to default" link appeared beside all forty controls on a fresh install, next to
  // settings nobody had touched.
  read_only: boolean
  is_default: boolean
}

export interface SettingCategory {
  key: string
  label: string
  description: string
  settings: SettingDef[]
}

export interface RetentionCandidate {
  recordingId: number
  filename: string
  startedAt: string | null
  sizeBytes: number
  reason: string
}

export interface RetentionPlan {
  bytesBefore: number
  bytesLimit: number
  bytesToFree: number
  candidates: RetentionCandidate[]
  wouldDeleteCount: number
  wouldFreeBytes: number
  blocked: boolean
  blockedReason: string | null
  /** False in the recommended read-only deployment: the plan is a report, not an action. */
  deletionEnabled: boolean
  safety: SafetyReport
}

export interface SafetyCheck {
  name: string
  passed: boolean
  reason: string | null
}

export interface SafetyReport {
  ok: boolean
  checks: SafetyCheck[]
  blockedReason: string | null
}

export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  pageSize: number
  pages: number
}

export interface SearchResults {
  plates: Plate[]
  recordings: Recording[]
  journeys: Journey[]
}

/**
 * A heat map of where the vehicle has actually been.
 *
 * `points` are `[lat, lon, weight]` triples on a grid the server rounded to `precision`
 * decimal places. Aggregation happens in SQL because one second of footage is one fix, so
 * a year of driving is millions of coordinates; grouping into cells bounds the payload by
 * the area covered rather than the time spent covering it.
 */
export interface Heatmap {
  points: [number, number, number][]
  /** Grid resolution in decimal places: 2 is about 1.1 km, 3 about 110 m, 4 about 11 m. */
  precision: number
  cells: number
  /** Weight of the busiest cell; the colour ramp normalises against this. */
  maxWeight: number
  /** Total fixes represented, which at the 1 Hz sample rate is also seconds of driving. */
  totalPoints: number
  averageSpeedKmh: number | null
  /** True when the cell cap was reached, so only the densest cells are shown. */
  truncated: boolean
}

export interface HeatmapFilters {
  /** Index signature so this satisfies the api client's `Query` shape. */
  [key: string]: string | number | boolean | null | undefined
  precision?: number
  start?: string
  end?: string
  journeyId?: number
  camera?: string
  minSpeedKmh?: number
}

/**
 * Every drive as drawable lines, to trace the roads under the heat map.
 *
 * A drive is several lines, not one. The camera loses its lock in tunnels, rejected
 * readings leave holes, and a journey stitches together clips minutes apart — joining
 * across any of those would draw a road that does not exist.
 */
export interface Routes {
  /** Polylines, each a list of [lat, lon] pairs. */
  lines: number[][][]
  journeys: number
  segments: number
  points: number
  /** Simplification tolerance actually applied, in metres. */
  simplifyM: number
  /** True when the point cap was reached, so the network shown is incomplete. */
  truncated: boolean
}

export interface RouteFilters {
  [key: string]: string | number | boolean | null | undefined
  start?: string
  end?: string
  journeyId?: number
  simplifyM?: number
}

/**
 * What the overlay reader extracted from one frame.
 *
 * `decodedText` and `parsed` are separate on purpose: text that decoded wrongly looks
 * nothing like text that decoded fine and then failed validation, and telling those two
 * apart is most of diagnosing a telemetry problem.
 */
export interface OsdDebug {
  recordingId: number
  filename: string
  offsetS: number
  region: { x: number; y: number; width: number; height: number }
  templatesLoaded: boolean
  decodedText: string
  confidence: number
  glyphs: number
  rereadAvailable: boolean
  rereadError: string | null
  parsed: {
    capturedAt: string | null
    lat: number | null
    lon: number | null
    hasFix: boolean
    speedKmh: number | null
    problems: string[]
    timeStatus: string
    gpsStatus: string
  }
  timeline: {
    frameNumber: number | null
    videoPtsS: number
    relativeTimeS: number
    expectedAt: string | null
  }
  storedSample: (TelemetryPoint & { dtS: number }) | null
  imageUrl: string
}
