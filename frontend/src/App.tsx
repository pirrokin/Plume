import { useCallback, useEffect, useState } from "react";
import { DocumentView } from "./components/DocumentView";
import { Home } from "./components/Home";

function docIdFromPath(): string | null {
  const m = window.location.pathname.match(/^\/d\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : null;
}

export function App() {
  const [docId, setDocId] = useState<string | null>(docIdFromPath);

  useEffect(() => {
    const onPop = () => setDocId(docIdFromPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const open = useCallback((id: string) => {
    window.history.pushState(null, "", `/d/${id}`);
    setDocId(id);
  }, []);

  const exit = useCallback(() => {
    window.history.pushState(null, "", "/");
    setDocId(null);
  }, []);

  return docId ? <DocumentView key={docId} docId={docId} onExit={exit} /> : <Home onOpen={open} />;
}
