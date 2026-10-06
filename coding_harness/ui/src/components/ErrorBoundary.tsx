// Per-panel isolation (pattern forked from nemoclaw-smb ErrorBoundary.tsx).

import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <p className="text-sm italic text-muted py-4">
          {this.props.label ?? "panel"} failed to render:{" "}
          <span className="font-mono text-xs">{this.state.error.message}</span>
        </p>
      );
    }
    return this.props.children;
  }
}
