import type { Metadata, Viewport } from "next";
import "./globals.css";
import { ServiceWorker } from "@/components/ServiceWorker";

export const metadata: Metadata = {
  title: "Diario di Bordo",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icons/icon-192.png", apple: "/icons/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "Diario", statusBarStyle: "default" },
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3ecdd" },
    { media: "(prefers-color-scheme: dark)", color: "#1b1713" },
  ],
};

const VERSO = `(function(){var o={"/":0,"/archivio/":1,"/mappa/":2,"/cerca/":3,"/nota/":4};function r(u){try{var p=new URL(u,location.href).pathname;if(p.slice(-1)!=="/")p+="/";return p in o?o[p]:-1}catch(e){return -1}}function q(){try{return sessionStorage.getItem("ddb-qui")}catch(e){return null}}try{var p=location.pathname,c=/^\\/(diari|account|persone)\\//.test(p)?"scaffale":/^\\/(accesso|registrati)\\//.test(p)?null:localStorage.getItem("ddb-copertina");if(c&&c!=="cuoio")document.documentElement.dataset.copertina=c}catch(e){}addEventListener("pagereveal",function(e){var t=e.viewTransition;if(t){var a=window.navigation&&navigation.activation,d=a&&a.from?a.from.url:q(),k=a?a.navigationType:"push",x=d?r(d):-1,y=r(location.href),i=x>=0&&y>=0&&x!==y?y<x:k==="traverse",h=document.documentElement;if(x<0||y<0){[t.ready,t.finished,t.updateCallbackDone].forEach(function(p){p&&p.catch(function(){})});t.skipTransition()}else{h.dataset.verso=i?"indietro":"avanti";t.finished.finally(function(){delete h.dataset.verso})}}try{sessionStorage.setItem("ddb-qui",location.pathname)}catch(e){}})})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <head>
        {/* Prima del primo disegno: il colore della copertina del diario aperto, e se la pagina si
            volta in avanti o torna indietro (tra lo scaffale e un diario niente foglio che si volta:
            lì si apre e si chiude il libro). */}
        <script dangerouslySetInnerHTML={{ __html: VERSO }} />
      </head>
      <body>
        {children}
        <ServiceWorker />
      </body>
    </html>
  );
}
