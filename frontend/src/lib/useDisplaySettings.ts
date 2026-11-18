import { useQuery } from '@tanstack/react-query'

import { api } from '@/lib/api'

export const DISPLAY_SETTINGS_KEY = ['display-settings'] as const

export function useDisplaySettings(enabled = true) {
  return useQuery({
    queryKey: DISPLAY_SETTINGS_KEY,
    queryFn: api.settings.display,
    staleTime: 300_000,
    enabled,
  })
}
