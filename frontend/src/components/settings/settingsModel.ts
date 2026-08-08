/**
 * Editing model for the settings catalogue.
 *
 * The catalogue is data: nothing here enumerates or rewrites a setting key, so a setting
 * the backend adds tomorrow renders with no frontend change. Keys are dotted backend
 * identifiers ("storage.max_footage_gb") and travel verbatim in both directions.
 *
 * Values are edited as text (booleans excepted) because a half-typed number is a normal
 * state for an input to be in, and coerced back to a typed value only on submit.
 */
import { ApiError } from '@/lib/api'
import { formatBytes } from '@/lib/format'
import type { SettingCategory, SettingDef, SettingType } from '@/lib/types'

export type DraftValue = string | boolean
export type DraftMap = Record<string, DraftValue>

const NUMERIC_TYPES: ReadonlySet<SettingType> = new Set<SettingType>(['int', 'float', 'bytes'])

export function isNumericType(type: SettingType): boolean {
  return NUMERIC_TYPES.has(type)
}

export function flattenSettings(categories: SettingCategory[]): Map<string, SettingDef> {
  const byKey = new Map<string, SettingDef>()
  for (const category of categories) {
    for (const setting of category.settings) byKey.set(setting.key, setting)
  }
  return byKey
}

export function categoryIndex(categories: SettingCategory[]): Map<string, string> {
  const index = new Map<string, string>()
  for (const category of categories) {
    for (const setting of category.settings) index.set(setting.key, category.key)
  }
  return index
}

/** The stored value in editable form. */
export function toDraft(setting: SettingDef): DraftValue {
  if (setting.type === 'bool') return setting.value === true
  const value = setting.value
  if (value === null || value === undefined) return ''
  return typeof value === 'string' ? value : String(value)
}

/** The value a gate check should see: the pending edit if there is one, else what is stored. */
export function effectiveBool(setting: SettingDef, drafts: DraftMap): boolean {
  const draft = drafts[setting.key]
  if (typeof draft === 'boolean') return draft
  if (typeof draft === 'string') return draft === 'true'
  return setting.value === true
}

export interface BlockingGate {
  key: string
  label: string
}

/**
 * The nearest gate up the `requires` chain that is switched off, or null when the whole
 * chain is satisfied. Walking the chain matters: "Maximum positional jump" requires GPS
 * continuity, which itself requires journeys — turning journeys off has to grey out both.
 */
export function resolveGate(
  setting: SettingDef,
  byKey: Map<string, SettingDef>,
  drafts: DraftMap,
): BlockingGate | null {
  const seen = new Set<string>([setting.key])
  let current: SettingDef = setting
  while (current.requires) {
    // A cycle would be a backend bug; refusing to gate is safer than locking the field.
    if (seen.has(current.requires)) return null
    seen.add(current.requires)
    const gate = byKey.get(current.requires)
    if (!gate) return null
    if (!effectiveBool(gate, drafts)) return { key: gate.key, label: gate.label }
    current = gate
  }
  return null
}

export function draftEquals(setting: SettingDef, draft: DraftValue): boolean {
  const current = toDraft(setting)
  if (typeof draft === 'boolean' || typeof current === 'boolean') return draft === current
  if (isNumericType(setting.type)) {
    const a = Number(draft)
    const b = Number(current)
    // "4" and "4.0" are the same setting; only fall back to text comparison if either
    // side is not a number the backend would accept.
    if (draft.trim() !== '' && current.trim() !== '' && Number.isFinite(a) && Number.isFinite(b)) {
      return a === b
    }
  }
  return draft === current
}

/**
 * Typed where the text parses cleanly, raw where it does not — an unparseable value is
 * sent as written so the backend's own validator produces the message the user sees.
 */
export function draftToPayload(setting: SettingDef, draft: DraftValue): unknown {
  if (typeof draft === 'boolean') return draft
  if (isNumericType(setting.type)) {
    const trimmed = draft.trim()
    const parsed = Number(trimmed)
    return trimmed !== '' && Number.isFinite(parsed) ? parsed : trimmed
  }
  return draft
}

/** Cheap client-side checks so an obviously invalid value does not need a round trip. */
export function localValidationError(setting: SettingDef, draft: DraftValue): string | null {
  if (typeof draft === 'boolean') return null
  if (isNumericType(setting.type)) {
    if (draft.trim() === '') return 'A value is required'
    const parsed = Number(draft)
    if (!Number.isFinite(parsed)) return 'Must be a number'
    if (setting.type !== 'float' && !Number.isInteger(parsed)) return 'Must be a whole number'
    if (setting.minimum !== null && parsed < setting.minimum) {
      return `Must be at least ${setting.minimum}${setting.unit ? ` ${setting.unit}` : ''}`
    }
    if (setting.maximum !== null && parsed > setting.maximum) {
      return `Must be at most ${setting.maximum}${setting.unit ? ` ${setting.unit}` : ''}`
    }
    return null
  }
  if (setting.type === 'path' && draft.trim() === '') return 'A path is required'
  return null
}

/** Human-readable rendering of a stored or default value, used for the "default" marker. */
export function displayValue(setting: SettingDef, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'On' : 'Off'
  if (setting.type === 'select') {
    const match = (setting.choices ?? []).find((choice) => choice.value === String(value))
    return match ? match.label : String(value)
  }
  if (setting.type === 'bytes' && typeof value === 'number') return formatBytes(value)
  const text = String(value)
  return setting.unit ? `${text} ${setting.unit}` : text
}

export interface SaveErrors {
  /** Setting key → message, for rendering beside the offending field. */
  fields: Record<string, string>
  /** Set only when nothing could be attributed to a specific field. */
  general: string | null
}

const MESSAGE_FIELDS = ['reason', 'message', 'msg', 'error'] as const
const KEY_FIELDS = ['key', 'field', 'setting'] as const

function messageOf(node: Record<string, unknown>): string | null {
  for (const field of MESSAGE_FIELDS) {
    const value = node[field]
    if (typeof value === 'string' && value.trim() !== '') return value
  }
  return null
}

function keyOf(node: Record<string, unknown>, known: ReadonlySet<string>): string | null {
  for (const field of KEY_FIELDS) {
    const value = node[field]
    if (typeof value === 'string' && known.has(value)) return value
  }
  // FastAPI reports the location as a path, e.g. ["body", "values", "storage.max_footage_gb"].
  const loc = node.loc
  if (Array.isArray(loc)) {
    for (const part of loc) {
      if (typeof part === 'string' && known.has(part)) return part
    }
  }
  return null
}

/**
 * Pull per-field messages out of an error body. The shape is not pinned down by the
 * client, so several plausible ones are accepted rather than guessing at one and showing
 * the user nothing useful when the guess is wrong.
 */
export function extractFieldErrors(error: unknown, known: ReadonlySet<string>): SaveErrors {
  const fields: Record<string, string> = {}

  const visit = (node: unknown, depth: number) => {
    if (depth > 6 || node === null || typeof node !== 'object') return
    if (Array.isArray(node)) {
      for (const item of node) visit(item, depth + 1)
      return
    }
    const record = node as Record<string, unknown>
    const key = keyOf(record, known)
    const message = messageOf(record)
    if (key && message) {
      fields[key] = message
      return
    }
    for (const [name, value] of Object.entries(record)) {
      if (known.has(name) && typeof value === 'string') fields[name] = value
      else visit(value, depth + 1)
    }
  }

  if (error instanceof ApiError) visit(error.detail, 0)

  if (Object.keys(fields).length > 0) return { fields, general: null }
  const general = error instanceof Error ? error.message : 'The change could not be saved'
  return { fields, general }
}
