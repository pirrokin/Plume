import { FormEvent, useState } from "react";
import { api, ApiError } from "../api";
import { IconFeather } from "./icons";

export function Login({ onDone }: { onDone(): void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!password) return;
    setBusy(true);
    setError(null);
    try {
      await api.login(password);
      onDone();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Connexion impossible");
      setBusy(false);
    }
  };

  return (
    <div className="home">
      <form className="login" onSubmit={submit}>
        <div className="home-brand">
          <span className="logo">
            <IconFeather size={26} />
          </span>
          <h1>Plume</h1>
        </div>
        <label htmlFor="plume-password">Mot de passe</label>
        <input
          id="plume-password"
          type="password"
          autoFocus
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div className="login-error">{error}</div>}
        <button className="btn primary" type="submit" disabled={busy || !password}>
          {busy ? "Connexion…" : "Se connecter"}
        </button>
      </form>
    </div>
  );
}
