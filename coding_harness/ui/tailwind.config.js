/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // San Diego sunset: dusk navy surfaces, orange interactive accent,
        // gold art ink, ember danger.
        paper: "hsl(var(--paper) / <alpha-value>)",
        card: "hsl(var(--card) / <alpha-value>)",
        ink: "hsl(var(--ink) / <alpha-value>)",
        muted: "hsl(var(--muted) / <alpha-value>)",
        rule: "hsl(var(--rule) / <alpha-value>)",
        accent: "hsl(var(--accent) / <alpha-value>)",
        gold: "hsl(var(--gold) / <alpha-value>)",
        danger: "hsl(var(--danger) / <alpha-value>)",
      },
      fontFamily: {
        serif: ["'Fraunces Variable'", "Georgia", "serif"],
        mono: ["'JetBrains Mono Variable'", "ui-monospace", "monospace"],
      },
      borderRadius: {
        DEFAULT: "0.4rem",
      },
      keyframes: {
        // Expanding pulse ring (heartbeat).
        heartbeat: {
          "0%": { boxShadow: "0 0 0 0 hsl(var(--accent) / 0.4)" },
          "70%": { boxShadow: "0 0 0 6px transparent" },
          "100%": { boxShadow: "0 0 0 0 transparent" },
        },
      },
      animation: {
        // The page's single motion: the live-connection dot.
        heartbeat: "heartbeat 3.2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
    },
  },
  plugins: [],
};
