/**
 * Catches anything a routed page throws, including failing to load at all.
 *
 * Every page in this app is `React.lazy`, so navigating to one fetches a hashed chunk —
 * `/assets/Backup-CEsM9GNL.js` and friends. Vite gives those files a new hash whenever
 * their contents change, and the container serves only the current build. So the moment
 * the image is updated underneath an open tab, that tab is holding an `index.html` whose
 * chunk names no longer exist: clicking "Backup" requests a file that 404s, the dynamic
 * import rejects, and the rejection surfaces during render.
 *
 * Without a boundary here React's response to that is to unmount the entire tree — which
 * is the blank screen, and why a manual refresh fixes it every time. The refresh is the
 * actual repair; the user was just being made to perform it by hand, with no clue that
 * was what the page wanted.
 *
 * So a stale chunk reloads the page itself, once. Once matters: if the chunk is still
 * missing after a reload the problem is not staleness, and a boundary that reloads on
 * every failure would spin. Anything that is not a stale chunk is a real bug in a page and
 * is shown rather than papered over with a refresh that will not help.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { ErrorState } from '@/components/ui'
import { claimChunkReload, clearChunkReload, isStaleChunk } from '@/lib/chunkRecovery'

interface Props {
  children: ReactNode
}

interface State {
  error: unknown
}

export default class RouteBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    if (isStaleChunk(error) && claimChunkReload()) {
      window.location.reload()
      return
    }
    console.error('a page failed to render', error, info.componentStack)
  }

  render(): ReactNode {
    if (this.state.error === null) return this.props.children
    if (isStaleChunk(this.state.error)) {
      // Reached only when the reload has already been spent, so asking again is the one
      // thing left — and it is at least an explanation instead of a white page.
      return (
        <ErrorState
          error={new Error('This page was updated while the tab was open, and could not reload.')}
          retry={() => {
            clearChunkReload()
            window.location.reload()
          }}
        />
      )
    }
    return <ErrorState error={this.state.error} retry={() => this.setState({ error: null })} />
  }
}
