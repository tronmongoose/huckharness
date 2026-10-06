// Envelope presets for the new-session form. Each maps to the JSON shape
// POST /v1/sessions accepts under "envelope" (see serve_mode envelope_from_dict).

export interface EnvelopePreset {
  key: string;
  label: string;
  description: string;
  envelope: { grants: Array<Record<string, unknown>> } | null;
}

export const ENVELOPE_PRESETS: EnvelopePreset[] = [
  {
    key: "read-only-repo",
    label: "Read-only repo",
    description: "Read, Grep, Glob over the repo. Everything else asks.",
    envelope: {
      grants: [
        { tool: "Read", access: "read", path_glob: "~/projects/**" },
        { tool: "Grep", access: "read", path_glob: "~/projects/**" },
        { tool: "Glob", access: "read", path_glob: "~/projects/**" },
      ],
    },
  },
  {
    key: "scratchpad",
    label: "Read + write scratchpad",
    description: "Repo reads plus writes confined to /tmp. Bash asks.",
    envelope: {
      grants: [
        { tool: "Read", access: "read", path_glob: "~/projects/**" },
        { tool: "Grep", access: "read", path_glob: "~/projects/**" },
        { tool: "Glob", access: "read", path_glob: "~/projects/**" },
        { tool: "Read", access: "read", path_glob: "/tmp/**" },
        { tool: "Write", access: "write", path_glob: "/tmp/**" },
        { tool: "Edit", access: "write", path_glob: "/tmp/**" },
      ],
    },
  },
  {
    // No spec in the request: the server builds its preset for its own cwd
    // at the chosen autonomy level, which is what a coding session wants.
    key: "project",
    label: "This project",
    description: "Tools run inside the project at the chosen level. Anything outside asks.",
    envelope: null,
  },
];

export function presetByKey(key: string): EnvelopePreset {
  return (
    ENVELOPE_PRESETS.find((p) => p.key === key) ?? ENVELOPE_PRESETS[0]
  );
}
