import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Align, api, ApiError, Block, Box, EditResult, PageInfo, Run, Style } from "../api";
import { RichText, normalizeRuns, runsText, styleOf, Toggle } from "../editor/richtext";
import { ensureFonts } from "../fonts";
import { Matrix, screenDeltaToPage } from "../geometry";
import { IconMove } from "./icons";

export interface FormatState {
  family: string | null;
  size: number | null;
  color: string;
  bold: boolean;
  italic: boolean;
  underline: boolean;
  strike: boolean;
  align: Align;
  wrap: boolean;
  isNew: boolean;
  issues: string[];
}

export interface EditorController {
  applyStyle(patch: Partial<Style>): void;
  toggle(kind: Toggle): void;
  setAlign(align: Align): void;
  commit(): Promise<boolean>;
  cancel(): void;
  remove(): Promise<void>;
  focus(): void;
}

export interface ActiveEdit {
  key: number;
  page: number;
  block: Block | null;
  box: Box;
  runs: Run[];
  click?: { x: number; y: number };
  closing?: boolean;
}

interface Props {
  docId: string;
  page: PageInfo;
  zoom: number;
  version: number;
  active: ActiveEdit;
  register(ctrl: EditorController | null): void;
  onFormat(state: FormatState): void;
  onCommitted(result: EditResult): void;
  onClose(): void;
  onError(message: string): void;
}

function sameContent(a: Run[], b: Run[]): boolean {
  return JSON.stringify(normalizeRuns(a)) === JSON.stringify(normalizeRuns(b));
}

function sameBox(a: Box, b: Box): boolean {
  const near = (x: number, y: number) => Math.abs(x - y) < 0.05;
  return (
    near(a.x, b.x) && near(a.y, b.y) && near(a.width, b.width) && a.wrap === b.wrap && a.align === b.align &&
    near(a.lineHeight, b.lineHeight) && near(a.indent, b.indent)
  );
}

function caretFromPoint(x: number, y: number): Range | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  if (doc.caretPositionFromPoint) {
    const pos = doc.caretPositionFromPoint(x, y);
    if (!pos) return null;
    const r = document.createRange();
    r.setStart(pos.offsetNode, pos.offset);
    return r;
  }
  return document.caretRangeFromPoint ? document.caretRangeFromPoint(x, y) : null;
}

export function BlockEditor(props: Props) {
  const { docId, page, zoom, active } = props;
  const textRef = useRef<HTMLDivElement>(null);
  const rtRef = useRef<RichText | null>(null);
  const [box, setBox] = useState<Box>(active.box);
  const boxRef = useRef(box);
  boxRef.current = box;
  const [baseline, setBaseline] = useState(0);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [maxSize, setMaxSize] = useState(active.box.size);
  const [busy, setBusy] = useState(false);
  const ratio = active.box.lineHeight / (active.box.size || 12);
  const propsRef = useRef(props);
  propsRef.current = props;
  const matrix = page.matrix as Matrix;

  const measure = useCallback(() => {
    const el = textRef.current;
    if (!el || !el.parentElement) return;
    const clone = el.cloneNode(true) as HTMLDivElement;
    clone.removeAttribute("contenteditable");
    clone.style.visibility = "hidden";
    clone.style.pointerEvents = "none";
    clone.style.top = "0px";
    const marker = document.createElement("span");
    marker.style.cssText = "display:inline-block;width:0;height:0;vertical-align:baseline;padding:0;margin:0";
    clone.insertBefore(marker, clone.firstChild);
    el.parentElement.appendChild(clone);
    const offset = marker.offsetTop;
    el.parentElement.removeChild(clone);
    setBaseline(offset);
    setSize({ w: el.offsetWidth, h: el.offsetHeight });
    const rt = rtRef.current;
    if (rt) setMaxSize(Math.max(...rt.runs.map((r) => r.size), rt.baseStyle().size));
  }, []);

  const emitFormat = useCallback(() => {
    const rt = rtRef.current;
    if (!rt) return;
    const styles = rt.selectionStyles();
    const first = styles[0];
    propsRef.current.onFormat({
      family: styles.every((s) => s.family === first.family) ? first.family : null,
      size: styles.every((s) => s.size === first.size) ? first.size : null,
      color: first.color,
      bold: styles.every((s) => s.weight >= 600),
      italic: styles.every((s) => s.italic),
      underline: styles.every((s) => s.underline),
      strike: styles.every((s) => s.strike),
      align: boxRef.current.align,
      wrap: boxRef.current.wrap,
      isNew: !propsRef.current.active.block,
      issues: propsRef.current.active.block?.issues ?? [],
    });
  }, []);

  // Create the rich-text editor once per edited block.
  useEffect(() => {
    const el = textRef.current!;
    const base: Style = styleOf(active.runs[0]);
    const rt = new RichText(el, active.runs, zoom, base);
    rtRef.current = rt;
    rt.onChange = () => {
      measure();
      emitFormat();
    };
    rt.onSelection = emitFormat;
    measure();
    emitFormat();
    let cancelled = false;
    const families = [...new Set(active.runs.map((r) => r.family))];
    ensureFonts(docId, families, runsText(active.runs)).then(() => {
      if (cancelled) return;
      measure();
      requestAnimationFrame(() => {
        if (cancelled) return;
        measure();
        if (active.click) {
          const range = caretFromPoint(active.click.x, active.click.y);
          if (range && el.contains(range.startContainer)) {
            el.focus({ preventScroll: true });
            const sel = window.getSelection();
            sel?.removeAllRanges();
            sel?.addRange(range);
            return;
          }
        }
        rt.focus(true);
      });
    });
    return () => {
      cancelled = true;
      rt.destroy();
      rtRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active.key]);

  useEffect(() => {
    rtRef.current?.setZoom(zoom);
    measure();
  }, [zoom, measure]);

  useLayoutEffect(() => {
    measure();
  }, [box.width, box.wrap, box.align, measure]);

  const finalBox = useCallback((): Box => {
    const rt = rtRef.current!;
    const runs = rt.content();
    const top = Math.max(...runs.map((r) => r.size));
    return { ...boxRef.current, lineHeight: +(ratio * top).toFixed(3), size: top };
  }, [ratio]);

  const commit = useCallback(async (): Promise<boolean> => {
    const rt = rtRef.current;
    const p = propsRef.current;
    if (!rt || busy) return false;
    const runs = rt.content();
    const target = finalBox();
    const block = p.active.block;
    const unchanged = block ? sameContent(runs, block.runs) && sameBox(target, p.active.box) : !runsText(runs).trim();
    if (unchanged) {
      p.onClose();
      return true;
    }
    setBusy(true);
    try {
      const result = block
        ? await api.edit(p.docId, p.page.index, block.id, { version: p.version, sig: block.sig, runs, box: target })
        : await api.add(p.docId, p.page.index, { version: p.version, runs, box: target });
      p.onCommitted(result);
      return true;
    } catch (e) {
      setBusy(false);
      p.onError(e instanceof ApiError ? e.message : "La modification a échoué.");
      return false;
    }
  }, [busy, finalBox]);

  const remove = useCallback(async () => {
    const p = propsRef.current;
    const block = p.active.block;
    if (!block) {
      p.onClose();
      return;
    }
    setBusy(true);
    try {
      p.onCommitted(await api.remove(p.docId, p.page.index, block.id, p.version, block.sig));
    } catch (e) {
      setBusy(false);
      p.onError(e instanceof ApiError ? e.message : "La suppression a échoué.");
    }
  }, []);

  useEffect(() => {
    const ctrl: EditorController = {
      applyStyle: (patch) => {
        rtRef.current?.applyStyle(patch);
        rtRef.current?.root.focus({ preventScroll: true });
      },
      toggle: (kind) => {
        rtRef.current?.toggle(kind);
        rtRef.current?.root.focus({ preventScroll: true });
      },
      setAlign: (align) => {
        setBox((b) => ({ ...b, align }));
        requestAnimationFrame(emitFormat);
      },
      commit,
      cancel: () => propsRef.current.onClose(),
      remove,
      focus: () => rtRef.current?.root.focus({ preventScroll: true }),
    };
    props.register(ctrl);
    return () => props.register(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [commit, remove, emitFormat]);

  // Keyboard: Escape cancels, Cmd/Ctrl+Enter validates.
  useEffect(() => {
    const el = textRef.current!;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.preventDefault();
        propsRef.current.onClose();
      } else if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        void commit();
      }
    };
    el.addEventListener("keydown", onKey);
    return () => el.removeEventListener("keydown", onKey);
  }, [commit]);

  // Dragging (move) and resizing (width).
  const startDrag = (ev: React.PointerEvent, mode: "move" | "resize") => {
    ev.preventDefault();
    ev.stopPropagation();
    const start = { x: ev.clientX, y: ev.clientY, box: boxRef.current };
    const target = ev.currentTarget as HTMLElement;
    target.setPointerCapture(ev.pointerId);
    const onMove = (e: PointerEvent) => {
      const [dx, dy] = screenDeltaToPage(matrix, (e.clientX - start.x) / zoom, (e.clientY - start.y) / zoom);
      if (mode === "move") {
        setBox({ ...start.box, x: start.box.x + dx, y: start.box.y + dy });
      } else {
        const width = Math.max(8, (start.box.wrap ? start.box.width : size.w / zoom) + dx);
        setBox({ ...start.box, width, wrap: true });
      }
    };
    const onUp = () => {
      target.removeEventListener("pointermove", onMove);
      target.removeEventListener("pointerup", onUp);
      target.removeEventListener("pointercancel", onUp);
      emitFormat();
      rtRef.current?.root.focus({ preventScroll: true });
    };
    target.addEventListener("pointermove", onMove);
    target.addEventListener("pointerup", onUp);
    target.addEventListener("pointercancel", onUp);
  };

  const widthPx = box.width * zoom;
  const actualW = box.wrap ? widthPx : Math.max(widthPx, size.w);
  let left = box.x * zoom;
  if (!box.wrap) {
    if (box.align === "right") left = (box.x + box.width) * zoom - actualW;
    else if (box.align === "center") left = (box.x + box.width / 2) * zoom - actualW / 2;
  }
  const top = box.y * zoom - baseline;
  const lineHeight = ratio * maxSize * zoom;

  return (
    <div className={`block-editor${busy ? " busy" : ""}${active.closing ? " closing" : ""}`}>
      <div
        className="block-frame"
        style={{ left: left - 4, top: top - 3, width: actualW + 8, height: size.h + 6 }}
      >
        <button
          className="frame-handle move"
          title="Déplacer le bloc"
          onPointerDown={(e) => startDrag(e, "move")}
          tabIndex={-1}
        >
          <IconMove size={12} />
        </button>
        <span className="frame-handle resize" title="Largeur du bloc" onPointerDown={(e) => startDrag(e, "resize")} />
      </div>
      <div
        ref={textRef}
        className="block-text"
        data-wrap={box.wrap ? "1" : "0"}
        style={{
          left,
          top,
          width: box.wrap ? widthPx : "max-content",
          minWidth: box.wrap ? undefined : widthPx,
          lineHeight: `${lineHeight}px`,
          textAlign: box.align,
          textIndent: box.indent ? `${box.indent * zoom}px` : undefined,
          whiteSpace: box.wrap ? "pre-wrap" : "pre",
        }}
      />
    </div>
  );
}
