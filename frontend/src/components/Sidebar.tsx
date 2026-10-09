import { useRef, useState } from "react";
import { api, DocInfo } from "../api";
import { renderScale } from "../geometry";
import { IconCopy, IconFilePlus, IconPlus, IconRotateLeft, IconRotateRight, IconTrash } from "./icons";

export type PageOp =
  | { op: "rotate"; angle: number }
  | { op: "delete" }
  | { op: "duplicate" }
  | { op: "insert_blank" }
  | { op: "move"; to: number };

interface Props {
  docId: string;
  info: DocInfo;
  currentPage: number;
  busy: boolean;
  onGoto(page: number): void;
  onOp(page: number, op: PageOp): void;
  onMerge(file: File): void;
}

export function Sidebar({ docId, info, currentPage, busy, onGoto, onOp, onMerge }: Props) {
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const scale = renderScale(0.18);
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <aside className="sidebar">
      {info.pages.map((p) => (
        <div
          key={`${p.index}-${p.rev}`}
          className={`thumb${currentPage === p.index ? " current" : ""}${dropAt === p.index && dragFrom !== p.index ? " drop" : ""}${dragFrom === p.index ? " dragging" : ""}`}
          draggable={!busy}
          onDragStart={(e) => {
            setDragFrom(p.index);
            e.dataTransfer.effectAllowed = "move";
          }}
          onDragOver={(e) => {
            if (dragFrom === null) return;
            e.preventDefault();
            setDropAt(p.index);
          }}
          onDragLeave={() => setDropAt((d) => (d === p.index ? null : d))}
          onDrop={(e) => {
            e.preventDefault();
            if (dragFrom !== null && dragFrom !== p.index) onOp(dragFrom, { op: "move", to: p.index });
            setDragFrom(null);
            setDropAt(null);
          }}
          onDragEnd={() => {
            setDragFrom(null);
            setDropAt(null);
          }}
          onClick={() => onGoto(p.index)}
          title={`Page ${p.index + 1} — glisser pour déplacer`}
        >
          <div className="thumb-img" style={{ aspectRatio: `${p.width} / ${p.height}` }}>
            <img loading="lazy" src={api.imageUrl(docId, p.index, scale, p.rev)} alt={`Page ${p.index + 1}`} draggable={false} />
            <div className="thumb-actions" onClick={stop}>
              <button title="Pivoter à gauche" disabled={busy} onClick={() => onOp(p.index, { op: "rotate", angle: -90 })}>
                <IconRotateLeft size={14} />
              </button>
              <button title="Pivoter à droite" disabled={busy} onClick={() => onOp(p.index, { op: "rotate", angle: 90 })}>
                <IconRotateRight size={14} />
              </button>
              <button title="Dupliquer la page" disabled={busy} onClick={() => onOp(p.index, { op: "duplicate" })}>
                <IconCopy size={14} />
              </button>
              <button title="Insérer une page blanche après" disabled={busy} onClick={() => onOp(p.index, { op: "insert_blank" })}>
                <IconPlus size={14} />
              </button>
              <button
                className="danger"
                title="Supprimer la page"
                disabled={busy || info.pages.length < 2}
                onClick={() => onOp(p.index, { op: "delete" })}
              >
                <IconTrash size={14} />
              </button>
            </div>
          </div>
          <span>{p.index + 1}</span>
        </div>
      ))}
      <button className="merge-btn" disabled={busy} onClick={() => fileInput.current?.click()} title="Ajouter les pages d'un autre PDF à la fin">
        <IconFilePlus size={18} />
        Ajouter un PDF
      </button>
      <input
        ref={fileInput}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onMerge(f);
          e.target.value = "";
        }}
      />
    </aside>
  );
}
