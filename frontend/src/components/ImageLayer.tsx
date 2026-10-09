import { useEffect, useRef, useState } from "react";
import type { PageImage, PageInfo } from "../api";
import { Matrix, rectToScreen, screenDeltaToPage, screenToPage } from "../geometry";

export interface ImageChange {
  image: PageImage;
  dx: number; // unrotated page space, points
  dy: number;
  scale: number;
}

interface Props {
  page: PageInfo;
  zoom: number;
  images: PageImage[];
  selected: string | null;
  busy: boolean;
  pageImg: HTMLImageElement | null;
  /** Revision of the page picture currently on screen. */
  shownRev: string | null;
  onSelect(id: string | null): void;
  onDragging(id: string | null): void;
  /** Resolves to false when the change could not be saved. */
  onChange(change: ImageChange): Promise<boolean>;
}

interface Drag {
  id: string;
  mode: "move" | "resize";
  startX: number;
  startY: number;
  // display-space rectangle (px) at the start and now
  rect: [number, number, number, number];
  current: [number, number, number, number];
}

/** Image selection, moving and resizing, in displayed page coordinates (rotation applied). */
export function ImageLayer(props: Props) {
  const { page, zoom, images, selected, busy, pageImg, shownRev, onSelect, onDragging, onChange } = props;
  const matrix = page.matrix as Matrix;
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  dragRef.current = drag;
  const ghost = useRef<HTMLCanvasElement>(null);
  // After a drop, the ghost stays put until the new page picture is on screen.
  const [committing, setCommitting] = useState<{ drag: Drag; rev: string } | null>(null);

  useEffect(() => {
    if (committing && shownRev && shownRev !== committing.rev) {
      setCommitting(null);
      onDragging(null);
    }
  }, [shownRev, committing, onDragging]);

  const display = (img: PageImage): [number, number, number, number] => {
    const r = rectToScreen(matrix, img.bbox);
    return [r[0] * zoom, r[1] * zoom, r[2] * zoom, r[3] * zoom];
  };

  // Paint the image's pixels (taken from the page picture) into the drag ghost.
  useEffect(() => {
    const canvas = ghost.current;
    if (!drag || !canvas || !pageImg || !pageImg.complete) return;
    if (canvas.width > 1 && canvas.dataset.id === drag.id) return; // already painted
    canvas.dataset.id = drag.id;
    const [x0, y0, x1, y1] = drag.rect;
    const w = Math.max(1, x1 - x0);
    const h = Math.max(1, y1 - y0);
    const ratio = pageImg.naturalWidth / (page.width * zoom);
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
    const ctx = canvas.getContext("2d");
    ctx?.drawImage(pageImg, x0 * ratio, y0 * ratio, w * ratio, h * ratio, 0, 0, canvas.width, canvas.height);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drag?.id, drag?.mode, pageImg]);

  const start = (e: React.PointerEvent, img: PageImage, mode: "move" | "resize") => {
    if (busy || !img.movable) return;
    e.preventDefault();
    e.stopPropagation();
    onSelect(img.id);
    const rect = display(img);
    const d: Drag = { id: img.id, mode, startX: e.clientX, startY: e.clientY, rect, current: rect };
    setDrag(d);
    let announced = false;
    const target = e.currentTarget as HTMLElement;
    target.setPointerCapture(e.pointerId);
    let moved = false;
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - d.startX;
      const dy = ev.clientY - d.startY;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      if (moved && !announced) {
        announced = true;
        onDragging(img.id); // the page picture switches to the version without this image
      }
      const [x0, y0, x1, y1] = d.rect;
      let next: [number, number, number, number];
      if (mode === "move") {
        next = [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
      } else {
        const w = x1 - x0;
        const h = y1 - y0;
        // Proportional resize from the bottom-right corner, following the axis
        // along which the pointer moved the most (relative to the image size).
        const ratio = Math.abs(dx) / w >= Math.abs(dy) / h ? (w + dx) / w : (h + dy) / h;
        const s = Math.max(12 / Math.max(w, h), ratio);
        next = [x0, y0, x0 + w * s, y0 + h * s];
      }
      const cur = dragRef.current;
      if (cur) setDrag({ ...cur, current: next });
    };
    const onUp = () => {
      target.removeEventListener("pointermove", onMove);
      target.removeEventListener("pointerup", onUp);
      target.removeEventListener("pointercancel", onUp);
      const cur = dragRef.current;
      if (!cur || !moved) {
        setDrag(null);
        return;
      }
      const [ox0, oy0, ox1] = cur.rect;
      const [nx0, ny0, nx1] = cur.current;
      const scale = (nx1 - nx0) / Math.max(1, ox1 - ox0);
      // The server scales about the image's top-left corner in unrotated page space;
      // keep the displayed top-left corner where the user put it.
      const anchor: [number, number] = [img.bbox[0], img.bbox[1]];
      const fixedNow = screenToPage(matrix, ox0 / zoom, oy0 / zoom);
      const [mx, my] = screenDeltaToPage(matrix, (nx0 - ox0) / zoom, (ny0 - oy0) / zoom);
      const dxPage = mx + (1 - scale) * (fixedNow[0] - anchor[0]);
      const dyPage = my + (1 - scale) * (fixedNow[1] - anchor[1]);
      setCommitting({ drag: cur, rev: page.rev });
      setDrag(null);
      void onChange({ image: img, dx: dxPage, dy: dyPage, scale }).then((ok) => {
        if (!ok) {
          setCommitting(null);
          onDragging(null);
        }
      });
    };
    target.addEventListener("pointermove", onMove);
    target.addEventListener("pointerup", onUp);
    target.addEventListener("pointercancel", onUp);
  };

  return (
    <div className="image-layer">
      {images.map((img) => {
        if (img.background && img.id !== selected) return null;
        const live = drag && drag.id === img.id ? drag : committing && committing.drag.id === img.id ? committing.drag : null;
        const [x0, y0, x1, y1] = live ? live.current : display(img);
        const isSel = img.id === selected;
        return (
          <div
            key={img.id}
            className={`image-outline${isSel ? " selected" : ""}${img.movable ? "" : " locked"}${live ? " dragging" : ""}`}
            style={{ left: x0, top: y0, width: x1 - x0, height: y1 - y0 }}
            title={img.movable ? "Image : glisser pour déplacer" : img.reason}
            onPointerDown={(e) => start(e, img, "move")}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(img.id);
            }}
          >
            {isSel && img.movable && (
              <span
                key="handle"
                className="image-handle"
                title="Redimensionner"
                onPointerDown={(e) => start(e, img, "resize")}
              />
            )}
            {live && <canvas key="ghost" ref={ghost} className="image-ghost" />}
          </div>
        );
      })}
    </div>
  );
}
