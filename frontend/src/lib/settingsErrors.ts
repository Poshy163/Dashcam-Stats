import type { SettingCategory } from './types'

export function settingControlId(key: string): string {
  return `setting-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

/** Keep unresolved field errors when another field is edited or reset. */
export function clearSettingError(errors: Record<string, string>, key: string): Record<string, string> {
  return Object.fromEntries(Object.entries(errors).filter(([name]) => name !== key && name !== '_'))
}

export function settingsSaveErrors(
  message: string,
  submitted: Record<string, unknown>,
  current: Record<string, unknown>,
): Record<string, string> {
  const key = Object.keys(submitted).sort((a, b) => b.length - a.length).find((name) => {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    return new RegExp(`(^|[^a-zA-Z0-9_.])${escaped}($|[^a-zA-Z0-9_.])`).test(message)
  })
  if (!key) return { _: message }
  // A slow rejected request describes the submitted value, not an edit made since then.
  if (!(key in current) || !Object.is(current[key], submitted[key])) {
    return { _: 'The previous settings save failed. Review your current changes and try again.' }
  }
  return { [key]: message }
}

export function settingsErrorSummary(errors: Record<string, string>, categories: SettingCategory[]) {
  return Object.entries(errors).map(([key, message]) => {
    const category = categories.find((item) => item.settings.some((setting) => setting.key === key))
    const setting = category?.settings.find((item) => item.key === key)
    return { key, message, category, setting }
  })
}
