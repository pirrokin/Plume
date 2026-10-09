import { memo, useEffect, useRef, useState } from "react";
import { api, Block, EditResult, PageInfo, PageModel } from "../api";
import { cssMatrix, Matrix, renderScale, screenToPage } from "../geometry";
import { ActiveEdit, BlockEditor, EditorController, FormatState } from "./BlockEditor";

export type Tool = "edit" | "add";

interface Props {
  docId: string;
  page: PageInfo;
  version: number;
  zoom: number;
  tool: Tool;
  model: PageModel | undefined;
  active: ActiveEdit | null;
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
  const scale = renderScale(zoom);
  const src = visible ? api.imageUrl(docId, page.index, scale, page.rev, hide) : null;
  const shown = usePageImage(src, () => {
    if (!hide) props.onImageShown(page.index, page.rev);
  });

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
      {shown ? <img className="page-img" src={shown} alt="" draggable={false} /> : <div className="page-loading" />}
      <div className="page-surface" onClick={onSurfaceClick} />
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
                className={`block-outline${b.editable ? "" : " locked"}${b.issues.length ? " warn" : ""}`}
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
      <div className="page-number">{page.index + 1}</div>
    </div>
  );
}

export const PageView = memo(PageViewInner);
