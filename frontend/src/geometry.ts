/** Page coordinate helpers. PDF matrices map (x, y) to (a·x + c·y + e, b·x + d·y + f). */

export type Matrix = [number, number, number, number, number, number];

export function cssMatrix(m: Matrix, zoom: number): string {
  return `matrix(${m[0]}, ${m[1]}, ${m[2]}, ${m[3]}, ${m[4] * zoom}, ${m[5] * zoom})`;
}

/** Convert a displacement on screen into a displacement in unrotated page space. */
export function screenDeltaToPage(m: Matrix, dx: number, dy: number): [number, number] {
  const [a, b, c, d] = m;
  const det = a * d - b * c || 1;
  return [(d * dx - c * dy) / det, (-b * dx + a * dy) / det];
}

/** Map a point given in unrotated page space to displayed page space. */
export function pageToScreen(m: Matrix, x: number, y: number): [number, number] {
  return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
}

/** Map a displayed point back to unrotated page space. */
export function screenToPage(m: Matrix, x: number, y: number): [number, number] {
  return screenDeltaToPage(m, x - m[4], y - m[5]);
}

export const RENDER_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4];

export function renderScale(zoom: number): number {
  const target = zoom * Math.min(window.devicePixelRatio || 1, 3);
  return RENDER_STEPS.find((s) => s >= target - 0.01) ?? RENDER_STEPS[RENDER_STEPS.length - 1];
}
