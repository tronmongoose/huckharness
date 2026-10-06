// Background etching: one public-domain engraving per console view, amber-
// tinted, bleeding off the right edge behind the content. Same technique as
// An atmosphere layer, static (a console, not an article).
// Plates credited in the footer colophon.

const PLATES: Record<string, { src: string; alt: string; clip?: string }> = {
  sessions: {
    // Babbage's Difference Engine — computation under governance.
    src: "/engravings/difference-engine-amber.webp",
    alt: "",
  },
  fleet: {
    // Cole's orrery — clockwork on schedule.
    src: "/engravings/cole-orrery-amber.webp",
    alt: "",
  },
  brain: {
    // Ramelli's bookwheel (1588) — many open volumes at once.
    src: "/engravings/ramelli-bookwheel-amber.webp",
    alt: "",
  },
};

export function Etching({ view }: { view: string }) {
  const plate = PLATES[view];
  if (!plate) return null;
  return (
    <div
      className="fixed inset-0 pointer-events-none select-none overflow-hidden -z-10"
      aria-hidden="true"
    >
      <img
        src={plate.src}
        alt={plate.alt}
        className="absolute top-[8vh] right-[-10vw] w-[42vw] max-w-none opacity-[0.07] [clip-path:inset(0_0_18%_0)]"
      />
    </div>
  );
}
