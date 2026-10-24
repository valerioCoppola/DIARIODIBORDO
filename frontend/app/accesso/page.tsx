"use client";

import { useState } from "react";
import { ApiError, loadSession, login } from "@/lib/api";

export default function AccessoPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      const me = await loadSession();
      window.location.replace(me?.cambia_password ? "/account/" : "/diari/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Il PC non risponde. Controlla che il servizio sia acceso.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
    <header className="copertina">
      <h1><a href="/">Diario di Bordo</a></h1>
    </header>
    <main className="wrap">
      <form className="accesso" onSubmit={submit}>
        {error && <p className="avviso grave" role="alert">{error}</p>}
        <div className="campo">
          <label htmlFor="u">Nome utente</label>
          <input id="u" autoComplete="username" autoCapitalize="none" value={username}
            onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="campo">
          <label htmlFor="p">Password</label>
          <input id="p" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <button className="primario" type="submit" disabled={busy}>{busy ? "Accesso…" : "Accedi"}</button>
        <p className="accesso-altro"><a href="/registrati/">Hai un invito? Crea il tuo account</a></p>
      </form>
    </main>
    </>
  );
}
