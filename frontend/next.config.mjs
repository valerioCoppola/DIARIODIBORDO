// In produzione il frontend è esportato come file statici (cartella out/)
// e servito dal backend FastAPI sulla stessa origine delle API.
// In sviluppo `next dev` inoltra /api al backend su localhost:8000.
const isDev = process.env.NODE_ENV === "development";

/** @type {import('next').NextConfig} */
const config = {
  reactStrictMode: true,
  trailingSlash: true,
  images: { unoptimized: true },
  ...(isDev
    ? {
        async rewrites() {
          return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" }];
        },
      }
    : { output: "export" }),
};

export default config;
