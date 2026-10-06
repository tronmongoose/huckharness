// Payload types mirroring coding_harness/modes/serve_mode.py. Kept loose on
// purpose — the server is the source of truth; unknown fields pass through.

export interface SessionSummary {
  session_id: string;
  mode: string;
  model: string;
  identity: string | null;
  revoked: boolean;
  has_envelope: boolean;
  closed: boolean;
  explicit_model?: boolean;
  kind?: "code" | "chat";
  title?: string | null;
  turn_active?: boolean;
  autonomy?: string;
  review_writes?: boolean;
}

export interface Healthz {
  cwd: string;
  default_model: string;
  default_autonomy: string;
}

export interface GrantDict {
  tool: string;
  access: string;
  path_glob: string | null;
  granted_by: string;
  expires_at: string | null;
}

export interface EnvelopeDict {
  revoked: boolean;
  created_at: string;
  expires_at: string | null;
  grants: GrantDict[];
}

export interface PendingPermission {
  req_id: string;
  tool: string;
  args_preview: Record<string, string>;
  reason: string;
  created_at: string;
  // "review": a write parked for the operator's look, args_preview.diff set.
  kind?: "permission" | "review";
}

export interface TurnResponse {
  session_id: string;
  text: string;
  turns: number;
  halted_reason: string;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
}

// One JSON-RPC notification off the SSE stream.
export interface StreamEvent {
  method: string;
  params: Record<string, unknown>;
}

// A tool step inside a turn: start → sentinel verdict → result → audit hash.
export interface ToolStep {
  tool: string;
  argsPreview: string;
  args?: Record<string, unknown>;
  result?: string;
  sentinelAllowed?: boolean;
  sentinelReason?: string;
  isError?: boolean;
  resultPreview?: string;
  auditHash?: string;
  subagentId?: string; // an Explore step's live helper, see TurnBlock.subagents
}

// A helper the turn sent out (Explore), with its live tool steps.
export interface Subagent {
  id: string;
  agent: string;
  task: string;
  model: string;
  steps: Array<{ tool: string; summary: string }>;
  done: boolean;
  halted?: string;
  stepCount?: number;
}

// Thread order within a turn: prose and tool calls as they happened.
export type TurnItem =
  | { kind: "text"; text: string }
  | { kind: "step"; index: number }
  | { kind: "steer"; text: string; applied: boolean; dropped?: boolean };

// GET /v1/board: one card per live session (modes/serve_board.py).
export type BoardStatus = "needs_approval" | "running" | "error" | "done" | "idle";

export interface BoardRow {
  session_id: string;
  title: string | null;
  status: BoardStatus;
  pending: number;
  last_excerpt: string;
  last_event_ts: number | null;
  turn: number;
  model: string;
  autonomy: string;
  halted_reason?: string | null;
}

export interface BoardServer {
  server: { cwd: string; port: number; pid: number };
  sessions: BoardRow[];
  origin?: string;
  self?: boolean;
}

// GET /v1/board?all=1: every live GUI server, this one first.
export interface BoardAll {
  servers: BoardServer[];
  errors: Array<{ origin: string; error: string }>;
}

// One user turn as rendered in the thread.
export interface TurnBlock {
  turn: number;
  prompt: string;
  streamingText: string;
  steps: ToolStep[];
  items: TurnItem[];
  model?: string; // the model the router picked for this turn
  routeReason?: string;
  done: boolean;
  haltedReason?: string;
  tokensIn?: number;
  tokensOut?: number;
  errorText?: string;
  checkpoint?: string | null; // shadow sha at turn start
  filesChanged?: string[];
  revertedFiles?: string[];
  subagents?: Record<string, Subagent>;
}

// One file in GET /v1/sessions/{id}/diff.
export interface FileChange {
  path: string;
  status: "added" | "modified" | "deleted" | "unsnapshotted" | string;
  diff: string;
  truncated: boolean;
}

export interface DiffResponse {
  turn: number | null;
  base: string | null;
  files: FileChange[];
}

export interface AuditRow {
  tool?: string;
  allowed?: boolean;
  sentinel_reason?: string;
  sentinel_path?: string;
  agent_identity?: string | null;
  hash?: string;
  ts?: string;
  kind?: string;
  action?: string;
}

// Ops console (round 3): read-only routines + work views.
export interface RoutineJob {
  id: string;
  description: string;
  cadence: string;
  command: string;
  status: "ok" | "failed";
  consecutive_failures: number;
  last_failure?: string | null;
  last_error?: string | null;
}

export interface RoutinesResponse {
  jobs: RoutineJob[];
  total: number;
  failed: number;
  scheduler_alive: boolean;
  error?: string;
}

export interface WorkItem {
  id: string;
  title: string;
  status: string;
  priority: number | null;
  issue_type: string | null;
  updated_at?: string;
}

export interface WorkResponse {
  items: WorkItem[];
  status: string;
  source: "live" | "backup";
  backup_age?: string | null;
}

// Model agility: pin a turn to a model.
export interface ModelInfo {
  id: string;
  backend: "ollama" | "claude-cli";
  text_only: boolean;
  label?: string;
  loaded?: boolean;
  agent?: boolean | null; // forced tool-call probe: true, false, or untested
  probing?: boolean;
}

export interface ModelsResponse {
  models: ModelInfo[];
}

// What an in-flight turn is doing right now, for the status line.
export type PhaseKind = "idle" | "starting" | "baseline" | "waiting" | "generating" | "tool" | "approval";

export interface Phase {
  kind: PhaseKind;
  since: number; // ms epoch the phase began
  detail: string; // model tag, tool summary, or the baseline command
  tokens: number; // streamed deltas since generation began
}

export interface OllamaStatus {
  reachable: boolean;
  resident: Array<{ model: string; size_gb: number; until: string | null }>;
}

export interface SkillInfo {
  name: string;
  description: string;
  source: string;
}

export interface TranscriptInfo {
  id: string;
  session_id: string;
  title: string;
  modified: string;
  first_prompt: string | null;
  started: string | null;
  turns: number;
  bytes: number;
  resumable: boolean;
  reason_if_not: string | null;
}

// A thread rebuilt from a past transcript (the session_resumed event).
export interface ResumedInfo {
  sessionId: string;
  turns: number;
}

// GET /v1/settings. user/project are the raw files; effective is their merge.
export type SettingsFile = Record<string, unknown>;
export interface SettingsResponse {
  user: SettingsFile;
  project: SettingsFile;
  effective: SettingsFile;
  user_path: string;
  project_path: string;
  locked: string[];
  writable: string[];
  errors: string[];
  disabled: boolean;
}

// The last model read's prompt size against the model's window.
export interface ContextUse {
  used: number;
  max: number;
}

export interface SkillDetail extends SkillInfo {
  editable: boolean;
  text: string;
}

export interface BrainHit {
  path: string;
  vault: string;
  sensitivity: string;
  tier: number;
  score: number;
  snippet: string;
}

export interface BrainPage {
  path: string;
  sensitivity: string;
  tier: number;
  content: string;
}

export interface BrainStatus {
  configured: boolean;
  backend: "inprocess" | "mcp" | null;
  ok: boolean;
  // Hours since the last full index run; null when the backend cannot say.
  age_hours: number | null;
  stale: boolean;
  warnings: string[];
  error?: string;
}

// A note waiting to go out with the next turn.
export interface Attachment {
  path: string;
  tier: number;
}

// One TodoWrite checklist row (coding_harness/tools/todo.py).
export type TodoStatus = "pending" | "in_progress" | "done";

export interface TodoItem {
  id: string;
  text: string;
  status: TodoStatus;
}

// Where the session's plan stands, from plan_saved / plan_updated / plan_failed.
// `version` bumps on every save so the spec panel knows to reload the text.
export interface SpecStatus {
  version: number;
  bytes: number | null;
  error: string | null;
}

// Second-brain memory loop (modes/serve_brain_extra.py).
// A drafted memory waiting on the operator; `text` comes from GET .../memories.
export interface MemoryProposal {
  id: string;
  name: string;
  description: string;
  type: string;
  text?: string;
  sha256?: string; // of `text`; a decision must echo it
}

// A brain note pinned to the session: rendered into every local turn.
export interface PinnedNote {
  path: string;
  tier: number;
}

export interface SkillSuggestion {
  name: string;
  description: string;
  score: number;
}

// What the last local turn was primed with, from brain_primed.
export interface RecallInfo {
  paths: string[];
  tiers: number[];
  bytes: number;
}

// GET /v1/git (core/gitinfo.status). `error` replaces the rest outside a repo.
export interface GitFile {
  path: string;
  status: string;
  orig?: string;
}

export interface GitStatus {
  branch: string | null;
  detached: boolean;
  head: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  staged: GitFile[];
  unstaged: GitFile[];
  untracked: GitFile[];
  error?: string;
}

// GET /v1/worktrees rows, joined with the live GUI server registry.
export interface WorktreeRow {
  path: string;
  branch: string | null;
  head: string | null;
  detached: boolean;
  bare: boolean;
  locked: boolean;
  is_main: boolean;
  live: string | null;
  current: boolean;
}

// The git_commit event and the commit route's reply.
export interface GitCommitInfo {
  sha: string;
  paths: string[];
  message_first_line: string;
}
