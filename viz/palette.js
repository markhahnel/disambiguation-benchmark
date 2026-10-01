// Canonical palette for all figures in this project. Okabe-Ito, colourblind-safe.
// Categories are never encoded in colour alone; pair with shape, position,
// or direct labels. Python figure scripts mirror these values; this file is
// the source of truth.
//
// The base palette below is deliberately IDENTICAL to the sibling collision
// project's viz/palette.js. Figures from both projects are published under one
// byline on one site and should read as one body of work, so a change here is a
// change there, made by hand in both repos rather than allowed to drift.
//
// Three consumers read these definitions:
//   1. in-browser figure code (imports this module),
//   2. src/disambig/figures.py (mirrors the constants for the SVG exporters),
//   3. viz/figure_template.html (mirrors the theme values as CSS variables).
// tests/test_figures.py parses this file and fails if (2) or (3) drifts from it,
// because a silent colour drift between the interactive figure and its own PNG
// fallback is the kind of thing nobody notices until it is printed.
export const palette = {
  light: {
    background: "#ffffff",
    text: "#1a1a1a",
    muted: "#6b6b6b",
    grid: "#e0e0e0",
    accent: "#0072B2", // blue
    accent2: "#E69F00", // orange
    accent3: "#009E73", // bluish green
    accent4: "#CC79A7", // reddish purple
  },
  dark: {
    background: "#111418",
    text: "#f0f0f0",
    muted: "#9a9a9a",
    grid: "#33383f",
    accent: "#56B4E9", // sky blue
    accent2: "#E69F00",
    accent3: "#009E73",
    accent4: "#CC79A7",
  },
};

// Categorical order, each colour paired with a marker shape. Colour never
// carries a category on its own (CLAUDE.md section 4), so anything encoding a
// series takes the shape from the same entry it takes the colour from.
export const categorical = [
  { key: "accent", shape: "circle" },
  { key: "accent2", shape: "square" },
  { key: "accent3", shape: "triangle" },
  { key: "accent4", shape: "diamond" },
];

// Scales are derived from the palette above rather than being a second set of
// colours to keep in sync. Both derivation parameters are named constants
// because they are the only place the JS and Python implementations could
// disagree numerically; the test asserts both sides use these values.
export const SEQUENTIAL_FLOOR = 0.15; // lightest stop, so it stays visible on the page
export const DIVERGING_MID_MIX = 0.12; // neutral midpoint: background mixed towards text

// Channel-wise mix in 8-bit sRGB. Not perceptually uniform, and deliberately
// simple: figures.py reproduces this arithmetic exactly, including rounding half
// up, so an SVG exported by Python and a scale computed in the browser agree.
export function mix(a, b, t) {
  const from = parseHex(a);
  const to = parseHex(b);
  const channels = [0, 1, 2].map((i) => Math.round(from[i] + (to[i] - from[i]) * t));
  return "#" + channels.map((c) => c.toString(16).padStart(2, "0")).join("");
}

function parseHex(hex) {
  const value = hex.replace("#", "");
  if (value.length !== 6) throw new Error("expected a 6-digit hex colour, got " + hex);
  return [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
}

// Sequential: background towards the theme accent. Use for one ordered
// quantity, never for categories.
export function sequential(themeName, steps) {
  const theme = requireTheme(themeName);
  if (steps < 1) throw new Error("sequential needs at least one step");
  if (steps === 1) return [theme.accent];
  return range(steps).map((i) =>
    mix(theme.background, theme.accent, SEQUENTIAL_FLOOR +
      (1 - SEQUENTIAL_FLOOR) * (i / (steps - 1)))
  );
}

// Diverging: Okabe-Ito orange to a near-neutral midpoint to the theme accent.
// Orange against blue survives all three common forms of colour blindness,
// which is why the diverging pair is not the usual red and green.
export function diverging(themeName, steps) {
  const theme = requireTheme(themeName);
  if (steps < 2) throw new Error("diverging needs at least two steps");
  const mid = mix(theme.background, theme.text, DIVERGING_MID_MIX);
  return range(steps).map((i) => {
    const t = i / (steps - 1);
    return t <= 0.5 ? mix(theme.accent2, mid, t * 2) : mix(mid, theme.accent, (t - 0.5) * 2);
  });
}

function requireTheme(themeName) {
  const theme = palette[themeName];
  if (!theme) throw new Error("unknown theme " + themeName);
  return theme;
}

function range(n) {
  return Array.from({ length: n }, (_, i) => i);
}
