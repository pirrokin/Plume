import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { recentDocuments } from "./DocumentView";
import { IconFeather, IconFile, IconUpload } from "./icons";
import { useToasts } from "./Toasts";

interface Props {
  onOpen(id: string): void;
}

function ago(ts: number): string {
  const min = Math.round((Date.now() - ts) / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  const h = Math.round(min / 60);
  if (h < 24) return `il y a ${h} h`;
  return `il y a ${Math.round(h / 24)} j`;
}

export function Home({ onOpen }: Props) {
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const toasts = useToasts();
  const recent = recentDocuments();

  const upload = useCallback(
    async (file: File | undefined) => {
      if (!file) return;
      if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
        toasts.show("Choisissez un fichier PDF.", "error");
        return;
      }
      setUploading(file.name);
      try {
        const info = await api.upload(file);
        onOpen(info.id);
      } catch (e) {
        toasts.show(e instanceof ApiError ? e.message : "Envoi impossible.", "error");
        setUploading(null);
      }
    },
    [onOpen, toasts],
  );

  return (
    <div className="home">
      <div className="home-inner">
        <div className="home-brand">
          <span className="logo">
            <IconFeather size={26} />
          </span>
          <h1>Plume</h1>
        </div>
        <p className="tagline">Modifiez le texte de vos PDF directement, en conservant leurs polices et leur mise en page.</p>

        <div
          className={`dropzone${dragging ? " over" : ""}${uploading ? " busy" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            void upload(e.dataTransfer.files[0]);
          }}
          onClick={() => !uploading && input.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
        >
          {uploading ? (
            <>
              <div className="spinner" />
              <div className="drop-title">Ouverture de « {uploading} »…</div>
            </>
          ) : (
            <>
              <IconUpload size={34} />
              <div className="drop-title">Déposez un PDF ici</div>
              <div className="drop-sub">ou cliquez pour choisir un fichier</div>
            </>
          )}
          <input
            ref={input}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(e) => void upload(e.target.files?.[0])}
          />
        </div>

        {recent.length > 0 && (
          <div className="recent">
            <h2>Documents récents</h2>
            <ul>
              {recent.map((d) => (
                <li key={d.id}>
                  <button onClick={() => onOpen(d.id)}>
                    <IconFile size={18} />
                    <span className="recent-name">{d.name}</span>
                    <span className="recent-meta">
                      {d.pages} p. · {ago(d.at)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <p className="footnote">Les documents sont traités sur ce serveur et supprimés après 3 jours d'inactivité.</p>
      </div>
    </div>
  );
}
