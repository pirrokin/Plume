import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, Block, DocInfo, EditResult, FamilyInfo, PageModel, Run } from "../api";
import { registerFamilies } from "../fonts";
import type { ActiveEdit, EditorController, FormatState } from "./BlockEditor";
import type { ImageChange } from "./ImageLayer";
import { FormatBar } from "./FormatBar";
import {
  IconCheck,
  IconDownload,
  IconFeather,
  IconMinus,
  IconPlus,
  IconRedo,
  IconText,
  IconTextAdd,
  IconTrash,
  IconUndo,
  IconX,
} from "./icons";
import { PageView, Tool } from "./PageView";
import { PageOp, Sidebar } from "./Sidebar";
import { useToasts } from "./Toasts";

interface Props {
  docId: string;
  onExit(): void;
}

const ZOOMS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];

function defaultRun(model: PageModel | undefined): Run {
  // New text takes the most common style of the page.
  const counts = new Map<string, { n: number; run: Run }>();
  for (const b of model?.blocks ?? []) {
    for (const r of b.runs) {
      const k = `${r.family}|${r.size}|${r.color}|${r.weight}`;
      const c = counts.get(k) ?? { n: 0, run: r };
      c.n += r.text.length;
      counts.set(k, c);
    }
  }
  let best: Run | null = null;
  let bestN = 0;
  for (const c of counts.values()) if (c.n > bestN) [best, bestN] = [c.run, c.n];
  return {
    text: "",
    family: best?.family ?? "helvetica",
    weight: 400,
    italic: false,
    size: best?.size ?? 12,
    color: best?.color ?? "#000000",
    alpha: 1,
    underline: false,
    strike: false,
    rise: 0,
    spacing: 0,
    render: "fill",
  };
}

export function DocumentView({ docId, onExit }: Props) {
  const [info, setInfo] = useState<DocInfo | null>(null);
  const [models, setModels] = useState<Record<number, PageModel>>({});
  const [zoom, setZoom] = useState(() => {
    const saved = Number(localStorage.getItem("plume.zoom"));
    return ZOOMS.includes(saved) ? saved : 1.25;
  });
  const [tool, setTool] = useState<Tool>("edit");
  const [active, setActive] = useState<ActiveEdit | null>(null);
  const [format, setFormat] = useState<FormatState | null>(null);
  const [busy, setBusy] = useState(false);
  const [currentPage, setCurrentPage] = useState(0);
  const [ocrPages, setOcrPages] = useState<Set<number>>(new Set());
  const [pagesBusy, setPagesBusy] = useState(false);
  const [selectedImage, setSelectedImage] = useState<{ page: number; id: string } | null>(null);
  const [imageBusy, setImageBusy] = useState(false);
  const controller = useRef<EditorController | null>(null);
  const activeRef = useRef(active);
  activeRef.current = active;
  const keyRef = useRef(1);
  const commitActiveRef = useRef<() => Promise<boolean>>(async () => true);
  const loading = useRef(new Set<string>());
  const scroller = useRef<HTMLDivElement>(null);
  const toasts = useToasts();

  const fail = useCallback(
    (e: unknown) => toasts.show(e instanceof ApiError ? e.message : String(e), "error"),
    [toasts],
  );
  const showError = useCallback((m: string) => toasts.show(m, "error"), [toasts]);

  useEffect(() => {
    api
      .info(docId)
      .then((i) => {
        setInfo(i);
        document.title = `${i.name} — Plume`;
      })
      .catch((e) => {
        fail(e);
        onExit();
      });
    return () => {
      document.title = "Plume — éditeur PDF";
    };
  }, [docId, fail, onExit]);

  // The document only lives while this tab is open: heartbeat, and tell the
  // server when the tab goes away (a reload sends a heartbeat right after).
  useEffect(() => {
    let stopped = false;
    const beat = () => {
      api
        .ping(docId)
        .then((r) => {
          if (!r.alive && !stopped) {
            toasts.show("Ce document a expiré : rouvrez le fichier PDF.", "error");
            onExit();
          }
        })
        .catch(() => undefined);
    };
    const timer = window.setInterval(beat, 20000);
    const onVisible = () => document.visibilityState === "visible" && beat();
    const onHide = () => api.leaving(docId);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pagehide", onHide);
    beat();
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pagehide", onHide);
    };
  }, [docId, onExit, toasts]);

  // Warn before leaving with changes that were not downloaded.
  const unsavedRef = useRef(false);
  unsavedRef.current = !!info?.unsaved || !!active;
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!unsavedRef.current) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  const closeDocument = useCallback(async () => {
    if (activeRef.current && !(await commitActiveRef.current())) return;
    if (
      unsavedRef.current &&
      !window.confirm("Fermer le document ? Les modifications non téléchargées seront perdues.")
    )
      return;
    await api.closeDocument(docId);
    onExit();
  }, [docId, onExit]);

  useEffect(() => {
    localStorage.setItem("plume.zoom", String(zoom));
  }, [zoom]);

  // Page text models, loaded when a page becomes visible and when it changes.
  const loadModel = useCallback(
    (index: number) => {
      if (!info || !info.pages[index]) return;
      const rev = info.pages[index].rev;
      const key = `${index}:${rev}`;
      if (models[index]?.rev === rev || loading.current.has(key)) return;
      loading.current.add(key);
      api
        .page(docId, index)
        .then((m) => {
          registerFamilies(m.families);
          setModels((prev) => ({ ...prev, [index]: m }));
        })
        .catch(fail)
        .finally(() => loading.current.delete(key));
    },
    [docId, info, models, fail],
  );

  const onVisible = useCallback((index: number) => loadModel(index), [loadModel]);

  useEffect(() => {
    // Reload models of visible pages after an edit / undo changed their revision.
    if (!info) return;
    const gone = Object.keys(models).map(Number).filter((i) => i >= info.pages.length);
    if (gone.length) {
      setModels((prev) => {
        const next = { ...prev };
        for (const i of gone) delete next[i];
        return next;
      });
      return;
    }
    for (const key of Object.keys(models)) {
      const i = Number(key);
      if (models[i] && models[i].rev !== info.pages[i].rev) loadModel(i);
    }
  }, [info, models, loadModel]);

  const register = useCallback((ctrl: EditorController | null) => {
    controller.current = ctrl;
    if (!ctrl) setFormat(null);
  }, []);

  /** Validate the block being edited; false if it could not be saved. */
  const commitActive: () => Promise<boolean> = useCallback(async (): Promise<boolean> => {
    const a = activeRef.current;
    if (!a || a.closing) return true;
    if (!controller.current) return true;
    setBusy(true);
    try {
      return await controller.current.commit();
    } finally {
      setBusy(false);
    }
  }, []);

  commitActiveRef.current = commitActive;

  const openBlock = useCallback(
    async (page: number, block: Block, click: { x: number; y: number }) => {
      if (activeRef.current && !(await commitActive())) return;
      setActive({
        key: keyRef.current++,
        page,
        block,
        runs: block.runs,
        click,
        box: {
          x: block.x,
          y: block.baseline,
          width: block.width,
          wrap: block.wrap,
          align: block.align,
          lineHeight: block.lineHeight,
          indent: block.indent,
          size: block.size,
        },
      });
    },
    [commitActive],
  );

  const addAt = useCallback(
    async (page: number, x: number, y: number) => {
      if (activeRef.current && !(await commitActive())) return;
      const run = defaultRun(models[page]);
      setActive({
        key: keyRef.current++,
        page,
        block: null,
        runs: [run],
        box: {
          x,
          y: y + run.size * 0.8,
          width: Math.max(40, Math.min(300, (info?.pages[page].unrotated[0] ?? 595) - x - 20)),
          wrap: false,
          align: "left",
          lineHeight: run.size * 1.2,
          indent: 0,
          size: run.size,
        },
      });
      setTool("edit");
    },
    [commitActive, info, models],
  );

  const onCommitted = useCallback(
    (result: EditResult) => {
      registerFamilies(result.page.families);
      setInfo(result.info);
      setModels((prev) => ({ ...prev, [result.page.page]: result.page }));
      // Keep the edited text on screen until the new page image is displayed.
      setActive((a) => (a ? { ...a, closing: true } : a));
      const pageIndex = result.page.page;
      window.setTimeout(() => {
        setActive((a) => (a && a.closing && a.page === pageIndex ? null : a));
      }, 4000);
      if (result.report.restoredChars) {
        toasts.show("Du texte voisin a été réécrit pour être préservé.", "info");
      }
    },
    [toasts],
  );

  const onImageShown = useCallback((page: number, rev: string) => {
    setActive((a) => {
      if (a && a.closing && a.page === page && info?.pages[page]?.rev === rev) return null;
      return a;
    });
  }, [info]);

  const closeEditor = useCallback(() => setActive(null), []);

  const pageOp = useCallback(
    async (page: number, op: PageOp) => {
      if (!info) return;
      if (activeRef.current && !(await commitActive())) return;
      setPagesBusy(true);
      try {
        const next = await api.pageOp(docId, page, { version: info.version, ...op });
        setActive(null);
        setInfo(next);
      } catch (e) {
        fail(e);
      } finally {
        setPagesBusy(false);
      }
    },
    [info, commitActive, docId, fail],
  );

  const merge = useCallback(
    async (file: File) => {
      if (!info) return;
      if (activeRef.current && !(await commitActive())) return;
      setPagesBusy(true);
      try {
        const before = info.pages.length;
        const next = await api.merge(docId, file, info.version);
        setActive(null);
        setInfo(next);
        toasts.show(`${next.pages.length - before} page(s) ajoutée(s) depuis « ${file.name} ».`, "success");
      } catch (e) {
        fail(e);
      } finally {
        setPagesBusy(false);
      }
    },
    [info, commitActive, docId, fail, toasts],
  );

  const selectImage = useCallback(
    async (page: number, id: string | null) => {
      if (id && activeRef.current && !(await commitActiveRef.current())) return;
      setSelectedImage(id ? { page, id } : null);
    },
    [],
  );

  const imageAction = useCallback(
    async (page: number, imageId: string, body: { action: "move" | "delete"; dx?: number; dy?: number; scale?: number }) => {
      const model = models[page];
      const img = model?.images.find((i) => i.id === imageId);
      if (!info || !img) return false;
      setImageBusy(true);
      try {
        const res = await api.imageOp(docId, page, imageId, { version: info.version, sig: img.sig, ...body });
        registerFamilies(res.page.families);
        setInfo(res.info);
        setModels((prev) => ({ ...prev, [page]: res.page }));
        setSelectedImage(null);
        return true;
      } catch (e) {
        fail(e);
        return false;
      } finally {
        setImageBusy(false);
      }
    },
    [info, models, docId, fail],
  );

  const onImageChange = useCallback(
    (page: number, change: ImageChange) =>
      imageAction(page, change.image.id, { action: "move", dx: change.dx, dy: change.dy, scale: change.scale }),
    [imageAction],
  );

  const deleteSelectedImage = useCallback(() => {
    if (selectedImage) void imageAction(selectedImage.page, selectedImage.id, { action: "delete" });
  }, [selectedImage, imageAction]);

  const runOcr = useCallback(
    async (page: number) => {
      if (!info || ocrPages.has(page)) return;
      if (activeRef.current && !(await commitActive())) return;
      setOcrPages((s) => new Set(s).add(page));
      try {
        const res = await api.ocr(docId, page, info.version);
        registerFamilies(res.page.families);
        setInfo(res.info);
        setModels((prev) => ({ ...prev, [page]: res.page }));
        toasts.show(
          res.words ? `${res.words} mots reconnus : cliquez sur un texte pour le modifier.` : "Aucun texte reconnu sur cette page.",
          res.words ? "success" : "info",
        );
      } catch (e) {
        fail(e);
      } finally {
        setOcrPages((s) => {
          const next = new Set(s);
          next.delete(page);
          return next;
        });
      }
    },
    [info, ocrPages, commitActive, docId, toasts, fail],
  );

  const undo = useCallback(async () => {
    if (activeRef.current) {
      setActive(null);
      return;
    }
    try {
      setInfo(await api.undo(docId));
    } catch (e) {
      fail(e);
    }
  }, [docId, fail]);

  const redo = useCallback(async () => {
    if (activeRef.current) return;
    try {
      setInfo(await api.redo(docId));
    } catch (e) {
      fail(e);
    }
  }, [docId, fail]);

  const download = useCallback(async () => {
    if (!(await commitActive())) return;
    const a = document.createElement("a");
    a.href = api.downloadUrl(docId);
    a.download = info?.name ?? "document.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => {
      api.info(docId).then(setInfo).catch(() => undefined);
    }, 1500);
  }, [commitActive, docId, info]);

  const zoomBy = useCallback((dir: 1 | -1) => {
    setZoom((z) => {
      const i = ZOOMS.findIndex((v) => v >= z - 0.001);
      const next = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, (i < 0 ? ZOOMS.length - 1 : i) + dir))];
      return next;
    });
  }, []);

  const fitWidth = useCallback(() => {
    const el = scroller.current;
    if (!el || !info) return;
    const w = Math.max(...info.pages.map((p) => p.width));
    const z = (el.clientWidth - 64) / w;
    setZoom(ZOOMS.reduce((best, v) => (Math.abs(v - z) < Math.abs(best - z) && v <= z + 0.01 ? v : best), ZOOMS[0]));
  }, [info]);

  // Clicking elsewhere drops the image selection; Delete removes the image.
  useEffect(() => {
    if (!selectedImage) return;
    const onDown = (ev: PointerEvent) => {
      const t = ev.target as HTMLElement;
      if (!t.closest(".image-outline, .subbar, .toasts")) setSelectedImage(null);
    };
    const onKey = (ev: KeyboardEvent) => {
      if ((ev.target as HTMLElement).closest("input, select, textarea, [contenteditable='true']")) return;
      if (ev.key === "Delete" || ev.key === "Backspace") {
        ev.preventDefault();
        deleteSelectedImage();
      } else if (ev.key === "Escape") {
        setSelectedImage(null);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [selectedImage, deleteSelectedImage]);

  // Click anywhere outside the edited block validates it.
  useEffect(() => {
    const onDown = (ev: PointerEvent) => {
      const a = activeRef.current;
      if (!a || a.closing) return;
      const t = ev.target as HTMLElement;
      if (t.closest(".block-editor, .format-bar, .block-outline, .toasts, .topbar")) return;
      if (tool === "add" && t.closest(".page-surface")) return;
      void commitActive();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [commitActive, tool]);

  // Global shortcuts (outside of the text being edited).
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const mod = ev.metaKey || ev.ctrlKey;
      const inField = (ev.target as HTMLElement).closest("input, select, textarea, [contenteditable='true']");
      if (mod && (ev.key === "=" || ev.key === "+")) {
        ev.preventDefault();
        zoomBy(1);
      } else if (mod && ev.key === "-") {
        ev.preventDefault();
        zoomBy(-1);
      } else if (!inField && mod && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) void redo();
        else void undo();
      } else if (!inField && mod && ev.key.toLowerCase() === "s") {
        ev.preventDefault();
        void download();
      } else if (!inField && !mod && ev.key.toLowerCase() === "t") {
        setTool((t) => (t === "add" ? "edit" : "add"));
      } else if (!inField && ev.key === "Escape") {
        setTool("edit");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomBy, undo, redo, download]);

  // Track the page in view for the page counter.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const onScroll = () => {
      const pages = el.querySelectorAll<HTMLElement>(".page");
      const mid = el.getBoundingClientRect().top + el.clientHeight / 3;
      let current = 0;
      pages.forEach((p, i) => {
        if (p.getBoundingClientRect().top <= mid) current = i;
      });
      setCurrentPage(current);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [info]);

  const docFamilies = useMemo(() => {
    const seen = new Map<string, FamilyInfo>();
    for (const m of Object.values(models)) for (const f of m.families) seen.set(f.key, f);
    return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [models]);

  if (!info) {
    return (
      <div className="loading-screen">
        <div className="spinner" />
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <button className="brand" onClick={() => void closeDocument()} title="Fermer et ouvrir un autre document">
          <IconFeather size={20} />
          <span>Plume</span>
        </button>
        <div className="doc-name" title={info.name}>
          {info.name}
        </div>
        <button className="icon-btn close-doc" onClick={() => void closeDocument()} title="Fermer le document (supprimé du serveur)">
          <IconX size={16} />
        </button>
        <div className="toolbar">
          <div className="btn-group">
            <button className="icon-btn" onClick={() => void undo()} disabled={!info.canUndo && !active} title="Annuler (⌘Z)">
              <IconUndo />
            </button>
            <button className="icon-btn" onClick={() => void redo()} disabled={!info.canRedo || !!active} title="Rétablir (⇧⌘Z)">
              <IconRedo />
            </button>
          </div>
          <div className="btn-group segmented">
            <button className={`tool-btn${tool === "edit" ? " on" : ""}`} onClick={() => setTool("edit")} title="Cliquer sur un texte pour le modifier">
              <IconText /> Modifier
            </button>
            <button className={`tool-btn${tool === "add" ? " on" : ""}`} onClick={() => setTool("add")} title="Cliquer sur la page pour ajouter du texte (T)">
              <IconTextAdd /> Ajouter du texte
            </button>
          </div>
          <div className="btn-group zoom">
            <button className="icon-btn" onClick={() => zoomBy(-1)} title="Zoom arrière (⌘−)">
              <IconMinus />
            </button>
            <button className="zoom-label" onClick={fitWidth} title="Ajuster à la largeur">
              {Math.round(zoom * 100)} %
            </button>
            <button className="icon-btn" onClick={() => zoomBy(1)} title="Zoom avant (⌘+)">
              <IconPlus />
            </button>
          </div>
        </div>
        <button className="btn primary" onClick={() => void download()} title="Télécharger le PDF modifié (⌘S)">
          <IconDownload size={16} /> Télécharger
        </button>
      </header>

      <div className={`subbar${(format && controller.current) || selectedImage ? " open" : ""}`}>
        {format && controller.current ? (
          <FormatBar docId={docId} state={format} controller={controller.current} docFamilies={docFamilies} busy={busy} />
        ) : selectedImage ? (
          <div className="format-bar image-bar">
            <span className="image-bar-title">Image sélectionnée</span>
            <span className="hint">Glissez-la pour la déplacer, tirez le coin pour la redimensionner.</span>
            <div className="spacer" />
            <button className="btn ghost danger" onClick={deleteSelectedImage} disabled={imageBusy} title="Supprimer l'image (Suppr)">
              <IconTrash size={16} /> Supprimer l'image
            </button>
            <button className="btn primary" onClick={() => setSelectedImage(null)} disabled={imageBusy}>
              <IconCheck size={16} /> Terminé
            </button>
          </div>
        ) : (
          <div className="hint">
            {tool === "add"
              ? "Cliquez sur la page à l'endroit où ajouter du texte."
              : "Cliquez sur un texte pour le modifier, ou sur une image pour la déplacer."}
          </div>
        )}
      </div>

      <div className="workspace">
        <Sidebar
          docId={docId}
          info={info}
          currentPage={currentPage}
          busy={pagesBusy}
          onGoto={(i) =>
            scroller.current?.querySelector(`[data-page="${i}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" })
          }
          onOp={pageOp}
          onMerge={merge}
        />
        <main className="pages" ref={scroller}>
          {info.pages.map((p) => (
            <PageView
              key={p.index}
              docId={docId}
              page={p}
              version={info.version}
              zoom={zoom}
              tool={tool}
              model={models[p.index]}
              active={active}
              scanned={info.scanned.includes(p.index)}
              ocrRunning={ocrPages.has(p.index)}
              selectedImage={selectedImage?.page === p.index ? selectedImage.id : null}
              imageBusy={imageBusy}
              onSelectImage={selectImage}
              onImageChange={onImageChange}
              onOcr={runOcr}
              onVisible={onVisible}
              onBlockClick={openBlock}
              onAddAt={addAt}
              onImageShown={onImageShown}
              register={register}
              onFormat={setFormat}
              onCommitted={onCommitted}
              onCloseEditor={closeEditor}
              onError={showError}
            />
          ))}
        </main>
        <div className="page-counter">
          Page {currentPage + 1} / {info.pages.length}
        </div>
      </div>
    </div>
  );
}
