"use client";

import Link from "next/link";
import { useEffect } from "react";
import { Button, Card, ErrorState } from "@/components/ui";

/**
 * 路由级错误边界（Next.js App Router 约定文件）。
 *
 * 此前全站没有 error.tsx：任何页面 render 抛错（典型如后端返回了前端类型
 * 声明之外的结构），生产环境直接白屏「Application error」，用户无路可走。
 * 这个文件兜住**页面渲染期**的错误 —— 请求失败仍由各页面的 ErrorState 处理，
 * 两者互补，不重叠。
 *
 * 注意：生产环境下 `error.message` 可能是被脱敏的通用文案（Next.js 只在
 * 开发期透传原始错误），所以界面上以通用文案为主、digest 用于服务端日志对账。
 */
export default function GlobalRouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 保留浏览器控制台证据，便于对照后端日志（digest 可在服务端检索）。
    console.error("[route-error]", error.digest ?? "", error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <Card className="w-full max-w-md">
        <ErrorState
          message={
            error.digest
              ? `页面渲染时发生异常，已自动记录（错误编号 ${error.digest}）。可以重试，或回到管理简报继续其他工作。`
              : "页面渲染时发生异常，已自动记录。可以重试，或回到管理简报继续其他工作。"
          }
          onRetry={reset}
        />
        <div className="flex justify-center gap-2 pb-6">
          <Button size="sm" onClick={reset}>
            重试
          </Button>
          <Link
            href="/dashboard"
            className="inline-flex h-9 items-center rounded-control border border-input bg-surface px-4 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted"
          >
            回管理简报
          </Link>
        </div>
      </Card>
    </div>
  );
}
