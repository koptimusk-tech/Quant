// Chart colours validated with the dataviz palette checker (CVD + contrast) for each surface.
const LIGHT = { primary: "#4f46e5", secondary: "#d97706", grid: "#e2e8f0", axis: "#64748b", muted: "#94a3b8" };
const DARK = { primary: "#6366f1", secondary: "#d97706", grid: "#1e293b", axis: "#94a3b8", muted: "#475569" };

export function chartColors() {
  const dark = typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return dark ? DARK : LIGHT;
}
