// Chart colours validated with the dataviz palette checker (CVD + contrast) for each surface.
// ``series``: categorical slots in fixed order (dataviz reference palette), one entity per slot, never cycled.
const LIGHT = { primary: "#4f46e5", secondary: "#d97706", grid: "#e2e8f0", axis: "#64748b", muted: "#94a3b8",
  series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"], pos: "#2a78d6", neg: "#eb6834" };
const DARK = { primary: "#6366f1", secondary: "#d97706", grid: "#1e293b", axis: "#94a3b8", muted: "#475569",
  series: ["#3987e5", "#d95926", "#199e70", "#c98500"], pos: "#3987e5", neg: "#d95926" };

export function chartColors() {
  const dark = typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return dark ? DARK : LIGHT;
}
