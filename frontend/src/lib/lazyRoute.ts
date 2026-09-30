import { createElement, lazy, type ComponentType } from 'react'
import RouteContentReady from '@/components/RouteContentReady'

/** The success marker must not exist until the route chunk actually resolves. */
export function lazyRoute(load: () => Promise<{ default: ComponentType }>) {
  return lazy(async () => {
    const { default: Page } = await load()
    function LoadedRoute() {
      return createElement(RouteContentReady, null, createElement(Page))
    }
    return { default: LoadedRoute }
  })
}
