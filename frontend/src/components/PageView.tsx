import { memo, useEffect, useRef, useState } from "react";
import { api, Block, EditResult, PageInfo, PageModel } from "../api";
import { cssMatrix, Matrix, renderScale, screenToPage } from "../geometry";
import { ActiveEdit, BlockEditor, EditorController, FormatState } from "./BlockEditor";
import { ImageChange, ImageLayer } from "./ImageLayer";

export type Tool = "edit" | "add";

interface Props {
  docId: string;
  page: PageInfo;
  version: number;
  zoom: number;
  tool: Tool;
  model: PageModel | undefined;
  active: ActiveEdit | null;
  scanned: boolean;
  scannedCount: number;
  ocrRunning: boolean;
  onOcrAll(): void;
  selectedImage: string | null;
  imageBusy: boolean;
  onSelectImage(page: number, id: string | null): void;
  onImageChange(page: number, change: ImageChange): Promise<boolean>;
  onOcr(page: number): void;
  onVisible(index: number): void;
  onBlockClick(page: number, block: Block, click: { x: number; y: number }): void;
  onAddAt(page: number, x: number, y: number): void;
  onImageShown(page: number, rev: string): void;
  register(ctrl: EditorController | null): void;
  onFormat(state: FormatState): void;
  onCommitted(result: EditResult): void;
  onCloseEditor(): void;
  onError(message: string): void;
}

/** Double-buffered page image: the previous picture stays until the next one is decoded. */
function usePageImage(src: string | null, onShown: () => void) {
  const [shown, setShown] = useState<string | null>(null);
  const shownCb = useRef(onShown);
  shownCb.current = onShown;
  useEffect(() => {
    if (!src || src === shown) return;
    let cancelled = false;
    const img = new Image();
    img.decoding = "async";
    img.src = src;
    img
      .decode()
      .catch(() => undefined)
      .then(() => {
        if (!cancelled) {
          setShown(src);
          shownCb.current();
        }
      });
    return () => {
      cancelled = true;
    };
  }, [src, shown]);
  return shown;
}

function PageViewInner(props: Props) {
  const { docId, page, zoom, tool, model, active } = props;
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current!;
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            setVisible(true);
            props.onVisible(page.index);
          }
        }
      },
      { rootMargin: "800px 0px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page.index]);

  const editing = active && active.page === page.index ? active : null;
  const hide = editing?.block && !editing.closing ? editing.block.id : undefined;
  const [draggedImage, setDraggedImage] = useState<string | null>(null);
  const scale = renderScale(zoom);
  const src = visible ? api.imageUrl(docId, page.index, scale, page.rev, hide, draggedImage ?? undefined) : null;
  const [shownRev, setShownRev] = useState<string | null>(null);
  const shown = usePageImage(src, () => {
    if (!hide && !draggedImage) {
      setShownRev(page.rev);
      props.onImageShown(page.index, page.rev);
    }
  });
  const imgRef = useRef<HTMLImageElement>(null);

  // Prepare the picture without the selected image, shown while it is dragged.
  useEffect(() => {
    if (!visible || !props.selectedImage) return;
    const pre = new Image();
    pre.src = api.imageUrl(docId, page.index, scale, page.rev, undefined, props.selectedImage);
  }, [visible, props.selectedImage, docId, page.index, scale, page.rev]);

  const [uw, uh] = page.unrotated;
  const matrix = page.matrix as Matrix;

  const onSurfaceClick = (ev: React.MouseEvent) => {
    if (tool !== "add" || ev.target !== ev.currentTarget) return;
    const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
    const [x, y] = screenToPage(matrix, (ev.clientX - rect.left) / zoom, (ev.clientY - rect.top) / zoom);
    props.onAddAt(page.index, x, y);
  };

  return (
    <div
      ref={ref}
      className={`page tool-${tool}`}
      data-page={page.index}
      style={{ width: page.width * zoom, height: page.height * zoom }}
    >
      {shown ? <img ref={imgRef} className="page-img" src={shown} alt="" draggable={false} /> : <div className="page-loading" />}
      <div className="page-surface" onClick={onSurfaceClick} />
      {tool === "edit" && !editing && model?.rev === page.rev && model.images?.length > 0 && (
        <ImageLayer
          page={page}
          zoom={zoom}
          images={model.images}
          selected={props.selectedImage}
          busy={props.imageBusy}
          pageImg={imgRef.current}
          shownRev={shownRev}
          onSelect={(id) => props.onSelectImage(page.index, id)}
          onDragging={setDraggedImage}
          onChange={(change) => props.onImageChange(page.index, change)}
        />
      )}
      <div
        className="page-overlay"
        style={{ width: uw * zoom, height: uh * zoom, transform: cssMatrix(matrix, zoom) }}
      >
        {tool === "edit" &&
          model?.rev === page.rev &&
          model.blocks.map((b) =>
            editing?.block?.id === b.id ? null : (
              <div
                key={b.id}
                className={`block-outline${b.editable ? "" : " locked"}${b.scan ? " scan" : b.issues.length ? " warn" : ""}`}
                style={{
                  left: b.bbox[0] * zoom - 2,
                  top: b.bbox[1] * zoom - 1,
                  width: (b.bbox[2] - b.bbox[0]) * zoom + 4,
                  height: (b.bbox[3] - b.bbox[1]) * zoom + 2,
                }}
                title={b.issues.join("\n") || undefined}
                onMouseDown={(e) => e.preventDefault()}
                onClick={(e) => {
                  e.stopPropagation();
                  if (b.editable) props.onBlockClick(page.index, b, { x: e.clientX, y: e.clientY });
                  else props.onError(b.issues.join(" ") || "Ce texte ne peut pas être modifié.");
                }}
              />
            ),
          )}
        {editing && (
          <BlockEditor
            key={editing.key}
            docId={docId}
            page={page}
            zoom={zoom}
            version={props.version}
            active={editing}
            register={props.register}
            onFormat={props.onFormat}
            onCommitted={props.onCommitted}
            onClose={props.onCloseEditor}
            onError={props.onError}
          />
        )}
      </div>
      {(props.scanned || props.ocrRunning) && (
        <div className="scan-banner" onClick={(e) => e.stopPropagation()}>
          {props.ocrRunning ? (
            <>
              <span className="spinner small" /> Reconnaissance du texte en cours…
            </>
          ) : (
            <>
              <span>Page numérisée : le texte n'est pas encore modifiable.</span>
              <button className="btn primary small" onClick={() => props.onOcr(page.index)}>
                Reconnaître le texte (OCR)
              </button>
              {props.scannedCount > 1 && (
                <button className="btn small on-dark" onClick={() => props.onOcrAll()}>
                  Toutes les pages ({props.scannedCount})
                </button>
              )}
            </>
          )}
        </div>
      )}
      <div className="page-number">{page.index + 1}</div>
    </div>
  );
}

export const PageView = memo(PageViewInner);
