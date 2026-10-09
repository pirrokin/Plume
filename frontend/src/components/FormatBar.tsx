import { useEffect, useMemo, useState } from "react";
import type { Align, FamilyInfo } from "../api";
import { ensureFonts, familyInfo, systemFamilies } from "../fonts";
import type { EditorController, FormatState } from "./BlockEditor";
import {
  IconAlignCenter,
  IconAlignJustify,
  IconAlignLeft,
  IconAlignRight,
  IconBold,
  IconCheck,
  IconItalic,
  IconStrike,
  IconTrash,
  IconUnderline,
  IconWarning,
  IconX,
} from "./icons";

interface Props {
  docId: string;
  state: FormatState;
  controller: EditorController;
  docFamilies: FamilyInfo[];
  busy: boolean;
}

const SIZES = [6, 7, 8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 48, 60, 72];

const keep = (e: React.MouseEvent) => e.preventDefault();

export function FormatBar({ docId, state, controller, docFamilies, busy }: Props) {
  const [system, setSystem] = useState<FamilyInfo[]>([]);
  const [sizeText, setSizeText] = useState("");

  useEffect(() => {
    systemFamilies().then(setSystem).catch(() => setSystem([]));
  }, []);

  useEffect(() => {
    setSizeText(state.size ? String(Math.round(state.size * 10) / 10) : "");
  }, [state.size]);

  const docKeys = useMemo(() => new Set(docFamilies.map((f) => f.key)), [docFamilies]);
  const others = useMemo(() => system.filter((f) => !docKeys.has(f.key)), [system, docKeys]);
  const current = state.family ? familyInfo(state.family) : null;

  const setFamily = async (key: string) => {
    await ensureFonts(docId, [key]);
    controller.applyStyle({ family: key });
  };

  const applySize = (text: string) => {
    const v = parseFloat(text.replace(",", "."));
    if (Number.isFinite(v) && v >= 1 && v <= 500) controller.applyStyle({ size: v });
  };

  const alignButtons: [Align, typeof IconAlignLeft, string][] = [
    ["left", IconAlignLeft, "Aligner à gauche"],
    ["center", IconAlignCenter, "Centrer"],
    ["right", IconAlignRight, "Aligner à droite"],
    ["justify", IconAlignJustify, "Justifier"],
  ];

  return (
    <div className="format-bar" role="toolbar" aria-label="Mise en forme du texte">
      <select
        className="font-select"
        value={state.family ?? ""}
        onChange={(e) => void setFamily(e.target.value)}
        title="Police"
      >
        {!state.family && <option value="">Plusieurs polices</option>}
        {state.family && !docKeys.has(state.family) && !system.some((f) => f.key === state.family) && (
          <option value={state.family}>{current?.name ?? state.family}</option>
        )}
        {docFamilies.length > 0 && (
          <optgroup label="Polices du document">
            {docFamilies.map((f) => (
              <option key={f.key} value={f.key}>
                {f.name}
                {f.substituted ? " (remplacée)" : ""}
              </option>
            ))}
          </optgroup>
        )}
        <optgroup label="Polices installées">
          {others.map((f) => (
            <option key={f.key} value={f.key}>
              {f.name}
            </option>
          ))}
        </optgroup>
      </select>

      <input
        className="size-input"
        list="plume-sizes"
        value={sizeText}
        placeholder="—"
        title="Taille (pt)"
        onChange={(e) => setSizeText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            applySize(sizeText);
          }
        }}
        onBlur={() => sizeText && state.size !== parseFloat(sizeText) && applySize(sizeText)}
      />
      <datalist id="plume-sizes">
        {SIZES.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>

      <div className="btn-group">
        <button className={`icon-btn${state.bold ? " on" : ""}`} onMouseDown={keep} onClick={() => controller.toggle("bold")} title="Gras (⌘B)">
          <IconBold />
        </button>
        <button className={`icon-btn${state.italic ? " on" : ""}`} onMouseDown={keep} onClick={() => controller.toggle("italic")} title="Italique (⌘I)">
          <IconItalic />
        </button>
        <button className={`icon-btn${state.underline ? " on" : ""}`} onMouseDown={keep} onClick={() => controller.toggle("underline")} title="Souligné (⌘U)">
          <IconUnderline />
        </button>
        <button className={`icon-btn${state.strike ? " on" : ""}`} onMouseDown={keep} onClick={() => controller.toggle("strike")} title="Barré">
          <IconStrike />
        </button>
      </div>

      <label className="color-btn" title="Couleur du texte">
        <span className="swatch" style={{ background: state.color }} />
        <input type="color" value={state.color} onChange={(e) => controller.applyStyle({ color: e.target.value })} />
      </label>

      <div className="btn-group">
        {alignButtons.map(([a, Icon, label]) => (
          <button
            key={a}
            className={`icon-btn${state.align === a ? " on" : ""}`}
            onMouseDown={keep}
            onClick={() => controller.setAlign(a)}
            title={label}
            disabled={a === "justify" && !state.wrap}
          >
            <Icon />
          </button>
        ))}
      </div>

      {state.issues.length > 0 && (
        <span className="format-warning" title={state.issues.join("\n")}>
          <IconWarning size={16} />
        </span>
      )}

      <div className="spacer" />

      {!state.isNew && (
        <button className="btn ghost danger" onMouseDown={keep} onClick={() => void controller.remove()} disabled={busy} title="Supprimer ce bloc de texte">
          <IconTrash size={16} /> Supprimer
        </button>
      )}
      <button className="btn ghost" onMouseDown={keep} onClick={() => controller.cancel()} disabled={busy} title="Annuler (Échap)">
        <IconX size={16} /> Annuler
      </button>
      <button className="btn primary" onMouseDown={keep} onClick={() => void controller.commit()} disabled={busy} title="Valider (⌘Entrée)">
        <IconCheck size={16} /> Valider
      </button>
    </div>
  );
}
