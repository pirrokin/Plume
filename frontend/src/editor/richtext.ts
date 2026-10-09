/**
 * A small rich-text editor over a contentEditable element.
 *
 * The run list is the model. Typing is left to the browser (so accents, IME
 * and spell-checking keep working); the DOM is parsed back into runs after
 * each input. Structural edits (line breaks, paste, styling) are applied to
 * the model and the DOM is rebuilt, restoring the selection by character
 * offsets.
 */
import type { Run, Style } from "../api";
import { fontStack } from "../fonts";

export const STYLE_KEYS: (keyof Style)[] = [
  "family", "weight", "italic", "size", "color", "alpha", "underline", "strike", "rise", "spacing", "render",
];

export function styleOf(r: Style): Style {
  const s = {} as Record<string, unknown>;
  for (const k of STYLE_KEYS) s[k] = r[k];
  return s as unknown as Style;
}

export function sameStyle(a: Style, b: Style): boolean {
  return STYLE_KEYS.every((k) => a[k] === b[k]);
}

export function normalizeRuns(runs: Run[]): Run[] {
  const out: Run[] = [];
  for (const r of runs) {
    if (!r.text) continue;
    const last = out[out.length - 1];
    if (last && sameStyle(last, r)) last.text += r.text;
    else out.push({ ...r });
  }
  return out;
}

export function runsText(runs: Run[]): string {
  return runs.map((r) => r.text).join("");
}

function rgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return alpha >= 1 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function applyRunStyle(el: HTMLElement, s: Style, zoom: number): void {
  el.style.fontFamily = fontStack(s.family);
  el.style.fontWeight = String(s.weight);
  el.style.fontStyle = s.italic ? "italic" : "normal";
  el.style.fontSize = `${s.size * zoom}px`;
  el.style.color = rgba(s.color, s.alpha ?? 1);
  const deco = [s.underline ? "underline" : "", s.strike ? "line-through" : ""].filter(Boolean).join(" ");
  el.style.textDecoration = deco || "none";
  el.style.letterSpacing = s.spacing ? `${s.spacing * zoom}px` : "normal";
  el.style.verticalAlign = s.rise ? `${s.rise * zoom}px` : "baseline";
  if (s.render === "stroke") {
    el.style.webkitTextStroke = `${Math.max(0.5, s.size * zoom * 0.03)}px ${rgba(s.color, s.alpha ?? 1)}`;
    el.style.color = "transparent";
  } else {
    el.style.webkitTextStroke = "";
  }
}

interface Snapshot {
  runs: Run[];
  sel: [number, number] | null;
}

export type Toggle = "bold" | "italic" | "underline" | "strike";

export class RichText {
  readonly root: HTMLDivElement;
  runs: Run[];
  private zoom: number;
  private base: Style;
  private composing = false;
  private lastSel: [number, number] | null = null;
  private history: Snapshot[] = [];
  private future: Snapshot[] = [];
  private lastPush = 0;
  onChange: () => void = () => {};
  onSelection: () => void = () => {};

  constructor(root: HTMLDivElement, runs: Run[], zoom: number, base: Style) {
    this.root = root;
    this.zoom = zoom;
    this.runs = normalizeRuns(runs.map((r) => ({ ...r })));
    this.base = styleOf(this.runs[0] ?? base);
    root.contentEditable = "true";
    root.spellcheck = false;
    root.setAttribute("role", "textbox");
    root.setAttribute("aria-multiline", "true");
    root.addEventListener("beforeinput", this.onBeforeInput);
    root.addEventListener("input", this.onInput);
    root.addEventListener("keydown", this.onKeyDown);
    root.addEventListener("paste", this.onPaste);
    root.addEventListener("drop", this.onDrop);
    root.addEventListener("compositionstart", this.onCompositionStart);
    root.addEventListener("compositionend", this.onCompositionEnd);
    document.addEventListener("selectionchange", this.onSelectionChange);
    this.render(null);
    this.history.push({ runs: this.cloneRuns(), sel: null });
  }

  destroy(): void {
    const root = this.root;
    root.removeEventListener("beforeinput", this.onBeforeInput);
    root.removeEventListener("input", this.onInput);
    root.removeEventListener("keydown", this.onKeyDown);
    root.removeEventListener("paste", this.onPaste);
    root.removeEventListener("drop", this.onDrop);
    root.removeEventListener("compositionstart", this.onCompositionStart);
    root.removeEventListener("compositionend", this.onCompositionEnd);
    document.removeEventListener("selectionchange", this.onSelectionChange);
  }

  text(): string {
    return runsText(this.runs);
  }

  content(): Run[] {
    const runs = this.cloneRuns();
    // Trailing line breaks would only add empty lines.
    while (runs.length && /\n$/.test(runs[runs.length - 1].text)) {
      const last = runs[runs.length - 1];
      last.text = last.text.replace(/\n+$/, "");
      if (!last.text) runs.pop();
      else break;
    }
    return runs.length ? runs : [{ ...this.base, text: "" }];
  }

  baseStyle(): Style {
    return { ...this.base };
  }

  setZoom(zoom: number): void {
    if (zoom === this.zoom) return;
    this.zoom = zoom;
    this.render(this.getSelection());
  }

  focus(atEnd = false): void {
    this.root.focus({ preventScroll: true });
    if (atEnd) {
      const n = this.text().length;
      this.setSelection(n, n);
    }
  }

  // ------------------------------------------------------------ rendering
  render(sel: [number, number] | null): void {
    const root = this.root;
    applyRunStyle(root, this.base, this.zoom);
    root.style.textDecoration = "none";
    root.style.verticalAlign = "";
    root.textContent = "";
    this.runs.forEach((run, i) => {
      const span = document.createElement("span");
      span.dataset.i = String(i);
      applyRunStyle(span, run, this.zoom);
      span.textContent = run.text;
      root.appendChild(span);
    });
    const text = this.text();
    if (text === "" || text.endsWith("\n")) {
      const br = document.createElement("br");
      br.dataset.ph = "1";
      root.appendChild(br);
    }
    if (sel) this.setSelection(sel[0], sel[1]);
  }

  /** Re-index spans after a native edit; false when the DOM needs a rebuild. */
  private retag(): boolean {
    let offset = 0;
    let runIndex = 0;
    let runEnd = this.runs[0]?.text.length ?? 0;
    for (const child of Array.from(this.root.childNodes)) {
      if (child instanceof HTMLBRElement && child.dataset.ph) continue;
      if (!(child instanceof HTMLSpanElement) || child.dataset.i === undefined) return false;
      if (child.childNodes.length !== 1 || child.firstChild?.nodeType !== Node.TEXT_NODE) return false;
      const len = child.textContent?.length ?? 0;
      if (!len) return false;
      while (offset >= runEnd && runIndex < this.runs.length - 1) {
        runIndex += 1;
        runEnd += this.runs[runIndex].text.length;
      }
      if (offset + len > runEnd) return false;
      child.dataset.i = String(runIndex);
      offset += len;
    }
    const text = this.text();
    const needsPlaceholder = text === "" || text.endsWith("\n");
    const hasPlaceholder = this.root.lastChild instanceof HTMLBRElement;
    return needsPlaceholder === hasPlaceholder && offset === text.length;
  }

  private parse(): void {
    const pieces: Run[] = [];
    const walk = (node: Node, inherited: Style) => {
      node.childNodes.forEach((child) => {
        if (child.nodeType === Node.TEXT_NODE) {
          const t = (child.textContent ?? "").replace(/​/g, "");
          if (t) pieces.push({ ...inherited, text: t });
        } else if (child instanceof HTMLElement) {
          if (child.tagName === "BR") {
            if (!child.dataset.ph) pieces.push({ ...inherited, text: "\n" });
            return;
          }
          let style = inherited;
          const idx = child.dataset.i;
          if (idx !== undefined && this.runs[Number(idx)]) style = styleOf(this.runs[Number(idx)]);
          const isBlock = child.tagName === "DIV" || child.tagName === "P";
          if (isBlock && pieces.length && !pieces[pieces.length - 1].text.endsWith("\n")) {
            pieces.push({ ...style, text: "\n" });
          }
          walk(child, style);
        }
      });
    };
    walk(this.root, this.base);
    this.runs = normalizeRuns(pieces);
  }

  // ------------------------------------------------------------ selection
  getSelection(): [number, number] | null {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return null;
    const r = s.getRangeAt(0);
    if (!this.root.contains(r.startContainer) || !this.root.contains(r.endContainer)) return null;
    return [this.offsetOf(r.startContainer, r.startOffset), this.offsetOf(r.endContainer, r.endOffset)];
  }

  /** Last selection inside the editor, even after focus moved to a toolbar control. */
  selection(): [number, number] {
    const sel = this.getSelection() ?? this.lastSel;
    if (!sel) {
      const n = this.text().length;
      return [n, n];
    }
    return sel[0] <= sel[1] ? sel : [sel[1], sel[0]];
  }

  private offsetOf(node: Node, offset: number): number {
    const range = document.createRange();
    range.setStart(this.root, 0);
    try {
      range.setEnd(node, offset);
    } catch {
      return 0;
    }
    let n = 0;
    const walk = (x: Node) => {
      x.childNodes.forEach((c) => {
        if (c.nodeType === Node.TEXT_NODE) n += (c.textContent ?? "").replace(/​/g, "").length;
        else if (c instanceof HTMLElement && c.tagName === "BR" && !c.dataset.ph) n += 1;
        else walk(c);
      });
    };
    walk(range.cloneContents());
    return n;
  }

  setSelection(start: number, end: number): void {
    const texts: Text[] = [];
    const walker = document.createTreeWalker(this.root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) texts.push(walker.currentNode as Text);
    const locate = (target: number): [Node, number] => {
      let n = 0;
      for (const t of texts) {
        const len = t.length;
        if (target <= n + len) return [t, target - n];
        n += len;
      }
      if (texts.length) {
        const last = texts[texts.length - 1];
        return [last, last.length];
      }
      return [this.root, 0];
    };
    const [sn, so] = locate(start);
    const [en, eo] = locate(end);
    const sel = window.getSelection();
    if (!sel) return;
    const range = document.createRange();
    range.setStart(sn, so);
    range.setEnd(en, eo);
    sel.removeAllRanges();
    sel.addRange(range);
    this.lastSel = [start, end];
  }

  // ------------------------------------------------------------ model edits
  private cloneRuns(): Run[] {
    return this.runs.map((r) => ({ ...r }));
  }

  /** Split runs so that one starts at `offset`; returns its index. */
  private splitAt(offset: number): number {
    let n = 0;
    for (let i = 0; i < this.runs.length; i++) {
      const r = this.runs[i];
      const len = r.text.length;
      if (offset === n) return i;
      if (offset < n + len) {
        const head = { ...r, text: r.text.slice(0, offset - n) };
        const tail = { ...r, text: r.text.slice(offset - n) };
        this.runs.splice(i, 1, head, tail);
        return i + 1;
      }
      n += len;
    }
    return this.runs.length;
  }

  styleAt(offset: number): Style {
    let n = 0;
    for (const r of this.runs) {
      if (offset < n + r.text.length) return styleOf(r);
      n += r.text.length;
    }
    return styleOf(this.runs[this.runs.length - 1] ?? this.base);
  }

  /** Styles found in the current selection (or at the caret). */
  selectionStyles(): Style[] {
    const [s, e] = this.selection();
    if (s === e) return [this.styleAt(Math.max(0, s - 1))];
    const out: Style[] = [];
    let n = 0;
    for (const r of this.runs) {
      const rs = n;
      const re = n + r.text.length;
      if (re > s && rs < e) out.push(styleOf(r));
      n = re;
    }
    return out.length ? out : [this.styleAt(s)];
  }

  /** Apply a style to the selection, or to everything when nothing is selected. */
  applyStyle(patch: Partial<Style>, forceAll = false): void {
    const sel = this.selection();
    let [s, e] = sel;
    const all = forceAll || s === e;
    if (all) {
      s = 0;
      e = this.text().length;
      Object.assign(this.base, patch);
    }
    if (e > s) {
      const i0 = this.splitAt(s);
      const i1 = this.splitAt(e);
      for (let i = i0; i < i1; i++) Object.assign(this.runs[i], patch);
      this.runs = normalizeRuns(this.runs);
    }
    this.render(sel);
    this.commitChange(true);
  }

  toggle(kind: Toggle): void {
    const styles = this.selectionStyles();
    switch (kind) {
      case "bold":
        this.applyStyle({ weight: styles.every((s) => s.weight >= 600) ? 400 : 700 });
        break;
      case "italic":
        this.applyStyle({ italic: !styles.every((s) => s.italic) });
        break;
      case "underline":
        this.applyStyle({ underline: !styles.every((s) => s.underline) });
        break;
      case "strike":
        this.applyStyle({ strike: !styles.every((s) => s.strike) });
        break;
    }
  }

  insertText(text: string): void {
    const [s, e] = this.selection();
    const style = s > 0 ? this.styleAt(s - 1) : this.runs.length ? this.styleAt(0) : this.baseStyle();
    const i0 = this.splitAt(s);
    const i1 = this.splitAt(e);
    this.runs.splice(i0, i1 - i0, { ...style, text });
    this.runs = normalizeRuns(this.runs);
    const caret = s + text.length;
    this.render([caret, caret]);
    this.commitChange(true);
  }

  selectAll(): void {
    this.setSelection(0, this.text().length);
  }

  // ------------------------------------------------------------ history
  private commitChange(force: boolean): void {
    const now = Date.now();
    const snap = { runs: this.cloneRuns(), sel: this.getSelection() };
    if (!force && now - this.lastPush < 800 && this.history.length > 1) {
      this.history[this.history.length - 1] = snap;
    } else {
      this.history.push(snap);
      if (this.history.length > 200) this.history.shift();
    }
    this.lastPush = force ? 0 : now;
    this.future = [];
    this.onChange();
  }

  undo(): void {
    if (this.history.length < 2) return;
    const current = this.history.pop()!;
    this.future.push(current);
    const prev = this.history[this.history.length - 1];
    this.runs = prev.runs.map((r) => ({ ...r }));
    this.render(prev.sel ?? current.sel);
    this.onChange();
  }

  redo(): void {
    const next = this.future.pop();
    if (!next) return;
    this.history.push(next);
    this.runs = next.runs.map((r) => ({ ...r }));
    this.render(next.sel);
    this.onChange();
  }

  // ------------------------------------------------------------ events
  private onBeforeInput = (ev: InputEvent) => {
    switch (ev.inputType) {
      case "insertParagraph":
      case "insertLineBreak":
        ev.preventDefault();
        this.insertText("\n");
        break;
      case "insertFromDrop":
      case "insertFromPaste":
        // Handled by the paste listener; never let HTML in.
        ev.preventDefault();
        break;
      case "historyUndo":
        ev.preventDefault();
        this.undo();
        break;
      case "historyRedo":
        ev.preventDefault();
        this.redo();
        break;
      case "formatBold":
        ev.preventDefault();
        this.toggle("bold");
        break;
      case "formatItalic":
        ev.preventDefault();
        this.toggle("italic");
        break;
      case "formatUnderline":
        ev.preventDefault();
        this.toggle("underline");
        break;
      case "formatStrikeThrough":
        ev.preventDefault();
        this.toggle("strike");
        break;
      default:
        if (ev.inputType.startsWith("format")) ev.preventDefault();
    }
  };

  private onInput = () => {
    if (this.composing) return;
    const sel = this.getSelection();
    this.parse();
    if (!this.retag()) this.render(sel);
    this.commitChange(false);
  };

  private onKeyDown = (ev: KeyboardEvent) => {
    const mod = ev.metaKey || ev.ctrlKey;
    if (!mod || ev.altKey) return;
    const key = ev.key.toLowerCase();
    if (key === "z") {
      ev.preventDefault();
      if (ev.shiftKey) this.redo();
      else this.undo();
    } else if (key === "y") {
      ev.preventDefault();
      this.redo();
    } else if (key === "b" || key === "i" || key === "u") {
      ev.preventDefault();
      this.toggle(key === "b" ? "bold" : key === "i" ? "italic" : "underline");
    }
  };

  private onPaste = (ev: ClipboardEvent) => {
    ev.preventDefault();
    const text = ev.clipboardData?.getData("text/plain") ?? "";
    if (text) this.insertText(text.replace(/\r\n?/g, "\n"));
  };

  private onDrop = (ev: DragEvent) => {
    ev.preventDefault();
  };

  private onCompositionStart = () => {
    this.composing = true;
  };

  private onCompositionEnd = () => {
    this.composing = false;
    this.onInput();
  };

  private onSelectionChange = () => {
    const sel = this.getSelection();
    if (sel) {
      this.lastSel = sel;
      this.onSelection();
    }
  };
}
