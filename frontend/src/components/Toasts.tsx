import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from "react";

type Kind = "info" | "error" | "success";

interface Toast {
  id: number;
  text: string;
  kind: Kind;
}

interface ToastApi {
  show(text: string, kind?: Kind): void;
}

const Ctx = createContext<ToastApi>({ show: () => undefined });

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const show = useCallback((text: string, kind: Kind = "info") => {
    const id = nextId++;
    setItems((list) => [...list.filter((t) => t.text !== text), { id, text, kind }].slice(-4));
    window.setTimeout(() => setItems((list) => list.filter((t) => t.id !== id)), kind === "error" ? 7000 : 3500);
  }, []);
  const api = useMemo(() => ({ show }), [show]);
  return (
    <Ctx.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} onClick={() => setItems((l) => l.filter((x) => x.id !== t.id))}>
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToasts(): ToastApi {
  return useContext(Ctx);
}
