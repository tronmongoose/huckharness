// Session creation shared by the rail's form and the one-click / launch paths.

import { apiPost } from "./api";
import { presetByKey } from "./presets";

export interface NewSessionOptions {
  autonomy: string;
  presetKey: string;
  identity?: string;
  kind?: "code" | "chat";
}

export async function createSession(opts: NewSessionOptions): Promise<string | null> {
  // interactive is what builds the permission broker server-side. Without
  // it every out-of-envelope call hard-denies and the approval card can
  // never receive a request. A human is watching, so ask them.
  const body: Record<string, unknown> = { autonomy: opts.autonomy, interactive: true };
  if (opts.identity?.trim()) body.identity = opts.identity.trim();
  if (opts.kind === "chat") body.kind = "chat";
  const preset = presetByKey(opts.presetKey);
  if (preset.envelope) body.envelope = preset.envelope;
  const res = await apiPost<{ session_id: string }>("/v1/sessions", body);
  return res?.session_id ?? null;
}

export async function setSessionKind(sessionId: string, kind: "code" | "chat"): Promise<boolean> {
  return (await apiPost(`/v1/sessions/${sessionId}/kind`, { kind })) !== null;
}

export async function setSessionModel(sessionId: string, model: string): Promise<boolean> {
  const res = await apiPost(`/v1/sessions/${sessionId}/model`, { model });
  return res !== null;
}
