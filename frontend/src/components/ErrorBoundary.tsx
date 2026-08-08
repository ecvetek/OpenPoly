import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Route-level crash guard. React error boundaries must be class components
 * — there is no hooks equivalent. Wraps <Outlet /> in AppShell so a render
 * error in one page (e.g. a canvas section inspector tab) shows a fallback
 * instead of white-screening the whole app, and the nav/header stay usable
 * so the user can navigate away without a full reload.
 */
export class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled render error:', error, info.componentStack)
  }

  render() {
    if (this.state.error === null) {
      return this.props.children
    }
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="max-w-md flex flex-col items-center gap-3 text-center">
          <div className="rounded border border-red-700/50 bg-red-900/20 px-4 py-3 text-sm text-red-200">
            Something went wrong rendering this page.
          </div>
          <div className="font-mono text-[11px] text-neutral-500 break-all">
            {this.state.error.message}
          </div>
          <button
            type="button"
            onClick={() => this.setState({ error: null })}
            className="px-3 py-1 rounded text-sm bg-neutral-800 text-neutral-100 hover:bg-neutral-700 transition-colors"
          >
            Try again
          </button>
        </div>
      </div>
    )
  }
}
