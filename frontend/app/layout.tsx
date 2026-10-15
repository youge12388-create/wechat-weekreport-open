import type { Metadata } from "next";
import { Inter, Noto_Sans_SC } from "next/font/google";
import "./globals.css";
import ChunkRecovery from "@/components/chunk-recovery";
import Shell from "@/components/shell";

const inter = Inter({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-inter",
});

/**
 * 中文字体：Noto Sans SC（思源黑体）。
 *
 * **为什么需要它**：原来的字体栈里 `Inter` 只有拉丁字形，中文一律回退到「客户端的
 * 系统字体」—— Windows 上是微软雅黑、Mac 上是苹方、Linux 上是什么完全看装了没有。
 * 同一个页面在不同人的机器上长得不一样，而且 12px 的中文在雅黑下笔画偏细、发虚。
 * 用 webfont 把中文固定下来，所有客户端看到的字形一致、笔画更实。
 *
 * **preload 必须关掉**：中文 webfont 按 unicode-range 切成 101 个分片（可变字重合计
 * 约 4.3MB）。开启 preload 会把这一百多个文件全挂成 `<link rel=preload>`，
 * 等于每次进页面都预拉全量。关掉之后浏览器只取「页面真的出现了的字」所在的分片，
 * 其余永不下载。这也是 `subsets` 只声明 latin 的原因：它仅用于选择预载文件，
 * 中文分片由 unicode-range 自己按需命中。
 */
const notoSansSC = Noto_Sans_SC({
  subsets: ["latin"],
  preload: false,
  display: "swap",
  variable: "--font-noto-sc",
});

export const metadata: Metadata = {
  title: "企业内部 AI 数据平台 · 周报",
  description: "周报数据采集、结构化、看板与 AI 查询",
};

// 页面内容依赖会话、权限和运行期配置。部署切换后如果浏览器继续复用旧的
// HTML，旧页面会去加载已经不再存在的旧分块，最终显示 Next.js 客户端异常。
// 强制每次从当前 release 生成 HTML，静态 JS 仍由内容哈希负责长期缓存。
export const dynamic = "force-dynamic";
export const revalidate = 0;

// 这个监听必须在 React/Next 的分块脚本之前执行：如果旧 HTML 引用的页面 chunk
// 已经 404，根布局里的客户端组件根本来不及挂载，用户只会看到通用异常页。
const CHUNK_RECOVERY_BOOTSTRAP = `
(function () {
  if (window.__zhoubaoChunkRecoveryInstalled) return;
  window.__zhoubaoChunkRecoveryInstalled = true;
  function recover(reason) {
    var message = reason && reason.message ? reason.message : String(reason || "");
    if (!/ChunkLoadError|Loading chunk|Failed to load chunk/i.test(message)) return;
    var url = new URL(window.location.href);
    if (url.searchParams.has("_reload")) return;
    try {
      var key = "zhoubao-chunk-recovery:" + window.location.pathname;
      if (window.sessionStorage.getItem(key) === "1") return;
      window.sessionStorage.setItem(key, "1");
    } catch (_) {}
    url.searchParams.set("_reload", String(Date.now()));
    window.location.replace(url.toString());
  }
  window.addEventListener("error", function (event) { recover(event.error || event.message); }, true);
  window.addEventListener("unhandledrejection", function (event) { recover(event.reason); }, true);
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className={`${inter.variable} ${notoSansSC.variable}`}>
      <body>
        <script
          id="zhoubao-chunk-recovery-bootstrap"
          dangerouslySetInnerHTML={{ __html: CHUNK_RECOVERY_BOOTSTRAP }}
        />
        <ChunkRecovery />
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
