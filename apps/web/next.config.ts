import type { NextConfig } from "next";

// Build-time, trusted deployment configuration. Never selected by request input.
const upstream = process.env.PF_INTERNAL_API_ORIGIN;
if (upstream) {
  const url = new URL(upstream);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
      url.pathname !== "/" || url.search || url.hash || url.origin !== upstream) {
    throw new Error("PF_INTERNAL_API_ORIGIN must be a plain trusted HTTP(S) origin");
  }
}
const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    if (!upstream) return [];
    // Not an arbitrary proxy: expose only existing business API and readiness.
    return [
      { source: "/backend/api/v1/:path*", destination: `${upstream}/api/v1/:path*` },
      { source: "/backend/health", destination: `${upstream}/health` },
      { source: "/backend/ready", destination: `${upstream}/ready` },
    ];
  },
};
export default nextConfig;
