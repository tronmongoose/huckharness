// Fleet: the machine's background work, read-only. Routines are the heartbeat
// scheduler's jobs; Work is the beads issue list the fleet pulls from.

import { RoutinesView } from "./RoutinesView";
import { WorkView } from "./WorkView";

export function FleetView() {
  return (
    <div className="flex-1 min-w-0 overflow-y-auto space-y-8 pr-2">
      <div>
        <p className="text-sm text-muted italic mb-3">
          Routines: jobs the heartbeat scheduler runs on a cadence, with their failure state.
        </p>
        <RoutinesView />
      </div>
      <div>
        <p className="text-sm text-muted italic mb-3">
          Work: the beads issues the fleet picks up, by priority.
        </p>
        <WorkView />
      </div>
    </div>
  );
}
