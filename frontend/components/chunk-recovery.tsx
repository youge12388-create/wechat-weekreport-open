"use client";

import { useEffect } from "react";

/**
 * 部署切换期间，浏览器偶尔会把旧 HTML 与新静态资源拼在一起，Next.js 会抛出
 * ChunkLoadError。当前页面收到这类错误时自动换一个 URL 重新取 HTML；根布局本身
 * 是 no-store，因此重试会拿到当前 release，而不是让用户停在白屏错误页。
 */
export default function ChunkRecovery() {
  useEffect(() => {
    const key = `zhoubao-chunk-recovery:${window.location.pathname}`;
    let recovered = false;

    const recover = (reason: unknown) => {
      if (recovered) return;
      const message = String(
        reason instanceof Error ? reason.message : reason ?? ""
      );
      if (!/ChunkLoadError|Loading chunk|Failed to load chunk/i.test(message)) return;
      if (window.sessionStorage.getItem(key) === "1") return;
      recovered = true;
      window.sessionStorage.setItem(key, "1");
      const url = new URL(window.location.href);
      url.searchParams.set("_reload", String(Date.now()));
      window.location.replace(url.toString());
    };

    const onError = (event: ErrorEvent) => recover(event.error ?? event.message);
    const onRejection = (event: PromiseRejectionEvent) => recover(event.reason);
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  return null;
}
