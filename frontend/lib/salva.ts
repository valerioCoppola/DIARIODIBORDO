// Salvare un file dal Diario senza lasciare l'app.
// Nell'app sul telefono (schermata Home) un link a un file apre una vista da cui
// non si torna indietro: il file si scarica qui e si consegna al foglio di
// condivisione del telefono (Salva su File, AirDrop…); sul computer si scarica.

export async function prepara(url: string, nome: string): Promise<File> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    let msg = `Errore ${res.status}`;
    try { msg = (await res.json()).messaggio ?? msg; } catch { /* risposta non JSON */ }
    throw new Error(msg);
  }
  const cd = res.headers.get("Content-Disposition") ?? "";
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) ?? /filename="?([^";]+)"?/i.exec(cd);
  const blob = await res.blob();
  const name = m ? decodeURIComponent(m[1]) : nome;
  // Alcuni telefoni non condividono application/json: come testo passa ovunque.
  const type = name.endsWith(".json") ? "text/plain" : blob.type || "application/octet-stream";
  return new File([blob], name, { type });
}

/** "serve-tocco": il telefono vuole un nuovo tocco prima di aprire la condivisione. */
export async function consegna(file: File): Promise<"fatto" | "annullato" | "serve-tocco"> {
  const touch = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
  if (touch && navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      return "fatto";
    } catch (e) {
      const name = e instanceof DOMException ? e.name : "";
      if (name === "AbortError") return "annullato";
      if (name === "NotAllowedError") return "serve-tocco";
      throw e;
    }
  }
  const u = URL.createObjectURL(file);
  const a = document.createElement("a");
  a.href = u;
  a.download = file.name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 60_000);
  return "fatto";
}
