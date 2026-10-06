// The Bjorn Harness mark: an isometric wireframe cube — the agent — nested
// inside a hexagonal envelope — the governance boundary it runs within.
// Hand-drawn line art in the single accent ink; inherits color via
// currentColor so placements choose their own weight and opacity.

export function Mark({
  size = 20,
  className = "",
  annotated = false,
}: {
  size?: number;
  className?: string;
  // Glyph-engineering-drawing mode (TX-02 datasheet convention): dimension
  // lines, ticks, and mono callouts around the mark. Hero use only.
  annotated?: boolean;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox={annotated ? "-14 -10 128 120" : "0 0 100 100"}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinejoin="round"
      strokeLinecap="round"
      className={className}
      aria-hidden="true"
    >
      {/* envelope: hexagonal boundary with doubled plate edges */}
      <path d="M50 8 86.4 29 86.4 71 50 92 13.6 71 13.6 29Z" />
      <path d="M50 12.6 17.6 31.3 M50 87.4 82.4 68.7" strokeWidth={0.9} />
      {/* agent: isometric cube, corner-on */}
      <path d="M50 28 69 39 69 61 50 72 31 61 31 39Z" />
      <path d="M31 39 50 50 69 39 M50 50 50 72" />
      {/* riso ink pulls — the two heavy edges from the reference */}
      <path d="M50 28 69 39" strokeWidth={4.2} />
      <path d="M31 61 50 72" strokeWidth={4.2} />
      {annotated && (
        <g strokeWidth={0.5} opacity={0.85}>
          {/* dimension line: envelope width, with extension lines + ticks */}
          <path d="M13.6 71 13.6 102 M86.4 71 86.4 102" />
          <path d="M13.6 99 86.4 99" />
          <path d="M13.6 96.5 13.6 101.5 M86.4 96.5 86.4 101.5" />
          <text
            x="50" y="96" textAnchor="middle" fontSize="5"
            fontFamily="monospace" fill="currentColor" stroke="none"
          >
            72.8
          </text>
          {/* dimension line: cube height, right side */}
          <path d="M69 39 104 39 M69 61 104 61" />
          <path d="M101 39 101 61" />
          <path d="M98.5 39 103.5 39 M98.5 61 103.5 61" />
          <text
            x="106" y="52" fontSize="5" fontFamily="monospace"
            fill="currentColor" stroke="none"
          >
            22.0
          </text>
          {/* centerline through the vertical axis */}
          <path d="M50 -6 50 8 M50 92 50 96" strokeDasharray="4 2" />
          {/* datum callouts */}
          <circle cx="50" cy="8" r="1.4" />
          <text
            x="56" y="4" fontSize="5" fontFamily="monospace"
            fill="currentColor" stroke="none"
          >
            V1 (50,8)
          </text>
          <text
            x="-12" y="-2" fontSize="5.5" fontFamily="monospace"
            fill="currentColor" stroke="none"
          >
            BH-MK-01
          </text>
          <text
            x="-12" y="108" fontSize="5" fontFamily="monospace"
            fill="currentColor" stroke="none"
          >
            SCALE 1:1 · ∠30° ISO
          </text>
        </g>
      )}
    </svg>
  );
}
