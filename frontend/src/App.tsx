import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { DocumentView } from "./components/DocumentView";
import { Home } from "./components/Home";
import { Login } from "./components/Login";

function docIdFromPath(): string | null {
  const m = window.location.pathname.match(/^\/d\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : null;
}

type Auth = "checking" | "ok" | "login";

export function App() {
  const [docId, setDocId] = useState<string | null>(docIdFromPath);
  const [auth, setAuth] = useState<Auth>("checking");

  useEffect(() => {
    api
      .session()
      .then((s) => setAuth(s.required && !s.authenticated ? "login" : "ok"))
      .catch(() => setAuth("ok")); // the error is shown by the next request
    const onUnauthorized = () => setAuth("login");
    window.addEventListener("plume:unauthorized", onUnauthorized);
    return () => window.removeEventListener("plume:unauthorized", onUnauthorized);
  }, []);

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

  if (auth === "checking") return null;
  if (auth === "login") return <Login onDone={() => setAuth("ok")} />;
  return docId ? <DocumentView key={docId} docId={docId} onExit={exit} /> : <Home onOpen={open} />;
}
