/** Keeps one broken panel from taking the whole control surface down.
 *
 * React unmounts the entire tree when a render throws, which on this page would mean
 * a single malformed field in a session payload blanking the screen - including the
 * halt button - while an agent is trading unattended. Each section is therefore
 * wrapped on its own, and the halt control sits outside every boundary so it is the
 * last thing that can disappear.
 *
 * A class component because React has no hook form of componentDidCatch. This is the
 * only class in the app.
 */

import { Component, type ErrorInfo, type ReactNode } from "react"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

interface SectionBoundaryProps {
  /** Named in the fallback so the operator can say which panel failed. */
  name: string
  children: ReactNode
}

interface SectionBoundaryState {
  message: string
}

export class SectionBoundary extends Component<SectionBoundaryProps, SectionBoundaryState> {
  state: SectionBoundaryState = { message: "" }

  static getDerivedStateFromError(error: unknown): SectionBoundaryState {
    return { message: error instanceof Error ? error.message : String(error) }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console rather than a toast: the operator's next move is to read it, and a
    // dismissible notice about a rendering fault is worse than a permanent one.
    console.error(`[${this.props.name}] render failed`, error, info.componentStack)
  }

  render(): ReactNode {
    if (!this.state.message) return this.props.children
    return (
      <Card className="border-danger-border bg-danger-soft">
        <CardHeader>
          <CardTitle className="text-danger">The {this.props.name} panel failed to render</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm text-danger">
          <p className="font-mono text-xs">{this.state.message}</p>
          <p>
            The rest of this page, including the halt control, is unaffected. Reload to try again
            once the payload is fixed.
          </p>
        </CardContent>
      </Card>
    )
  }
}
