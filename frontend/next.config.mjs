/** @type {import('next').NextConfig} */
const nextConfig = {
  // 由 GitHub Actions 生成可直接运行的 Node.js 部署包，服务器无需再执行 next build。
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    // 开发期把 /api 代理到本地 FastAPI，避免 CORS
    const apiBase = process.env.API_BASE || "http://127.0.0.1:8010";
    return [{ source: "/api/:path*", destination: `${apiBase}/api/:path*` }];
  },
};

export default nextConfig;
