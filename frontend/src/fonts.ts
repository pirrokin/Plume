import { api, FamilyInfo } from "./api";

/** Known font families (document fonts and installed fonts), by key. */
const families = new Map<string, FamilyInfo>();
const loadedCss = new Set<string>();
const loadedRules = new Set<string>();
let styleEl: HTMLStyleElement | null = null;
let systemFonts: Promise<FamilyInfo[]> | null = null;

export function registerFamilies(list: FamilyInfo[]): void {
  for (const f of list) {
    const prev = families.get(f.key);
    families.set(f.key, { ...prev, ...f });
  }
}

export function familyInfo(key: string): FamilyInfo {
  return families.get(key) ?? { key, name: key, generic: "sans" };
}

export function systemFamilies(): Promise<FamilyInfo[]> {
  if (!systemFonts) {
    systemFonts = api.fonts().then((list) => {
      for (const f of list) if (!families.has(f.key)) families.set(f.key, f);
      return list;
    });
  }
  return systemFonts;
}

const cssGeneric: Record<string, string> = { sans: "sans-serif", serif: "serif", mono: "monospace" };

/** CSS font-family stack mirroring the server's glyph fallback chain. */
export function fontStack(key: string): string {
  const generic = familyInfo(key).generic || "sans";
  return `"PF ${key}", "PF generic-${generic}", "PF universal", ${cssGeneric[generic] ?? "sans-serif"}`;
}

function ensureStyleElement(): HTMLStyleElement {
  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.dataset.plume = "fonts";
    document.head.appendChild(styleEl);
  }
  return styleEl;
}

/** Load the @font-face rules of the given families (once per document). */
export async function ensureFonts(docId: string, keys: string[], sampleText = ""): Promise<void> {
  const missing = [...new Set(keys)].filter((k) => !loadedCss.has(`${docId}:${k}`));
  if (missing.length) {
    const res = await fetch(api.fontsCssUrl(docId, missing));
    if (res.ok) {
      const css = await res.text();
      const sheet = ensureStyleElement().sheet as CSSStyleSheet;
      for (const rule of css.split("\n")) {
        const r = rule.trim();
        if (!r || loadedRules.has(r)) continue;
        loadedRules.add(r);
        try {
          sheet.insertRule(r, sheet.cssRules.length);
        } catch {
          /* ignore rules the browser rejects */
        }
      }
      for (const k of missing) loadedCss.add(`${docId}:${k}`);
    }
  }
  // Make sure the faces are downloaded before anything gets measured.
  const text = sampleText || "AaBbÉé0123";
  await Promise.all(
    keys.flatMap((k) =>
      [
        `400 16px "PF ${k}"`,
        `700 16px "PF ${k}"`,
        `italic 400 16px "PF ${k}"`,
        `400 16px "PF generic-${familyInfo(k).generic || "sans"}"`,
      ].map((font) => document.fonts.load(font, text).catch(() => [])),
    ),
  );
}
