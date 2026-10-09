import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { IconFeather, IconUpload } from "./icons";
import { useToasts } from "./Toasts";

interface Props {
  onOpen(id: string): void;
}

export function Home({ onOpen }: Props) {
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const toasts = useToasts();

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

        <ul className="promises">
          <li>Le texte garde sa police, sa taille et sa mise en page.</li>
          <li>Images déplaçables, pages réorganisables, OCR des documents scannés.</li>
          <li>
            Rien n'est conservé : votre document est supprimé du serveur dès que vous fermez l'onglet (ou après 30 min
            d'inactivité).
          </li>
        </ul>
        <p className="home-source">
          Logiciel libre (licence AGPL) :{" "}
          <a href="https://github.com/pirrokin/Plume" target="_blank" rel="noopener noreferrer">
            code source
          </a>
        </p>
      </div>
    </div>
  );
}
