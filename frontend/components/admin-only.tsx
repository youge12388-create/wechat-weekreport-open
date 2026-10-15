"use client";

import React from "react";
import { useSession } from "@/components/session-provider";

/**
 * 仅可写管理员可见。
 *
 * **这不是安全边界**：真正的拦截在后端 AuthMiddleware（只读成员的写请求一律 403，
 * 且企微会话每请求回源复核授权）。这里只是不让同事点了才发现「你没权限」。
 *
 * `fallback` 用于需要保留位置感的场合（例如工具条里的按钮位），
 * 默认不渲染任何内容。
 */
export function AdminOnly({
  children,
  fallback = null,
}: {
  children: React.ReactNode;
  fallback?: React.ReactNode;
}) {
  const { isAdmin, loading } = useSession();
  // 会话未就绪时按「不是管理员」处理：宁可晚一点显示按钮，
  // 也不要先渲染出来再消失（闪一下最容易被当成故障）。
  if (loading || !isAdmin) return <>{fallback}</>;
  return <>{children}</>;
}

/** 非管理员的提示条：说明为什么少了若干按钮（文案按角色区分，不能再说「可查看全部数据」）。 */
export function ReadOnlyNotice({ className }: { className?: string }) {
  const { session, isAdmin, isDeptAdmin, loading } = useSession();
  if (loading || isAdmin || !session?.authenticated) return null;
  return (
    <p
      className={
        className ??
        "rounded-control bg-muted/40 px-3.5 py-2.5 text-xs leading-relaxed text-muted-foreground"
      }
    >
      {isDeptAdmin
        ? "你以部门管理员身份登录，可以查看并管理本部门（含下级）的周报与数据；系统设置、经营看板等公司级功能仅超级管理员可用。"
        : "你以只读成员身份登录，只能访问「发给我的事项」；周报、看板等经营数据需要管理员为你开通部门管理员或管理员角色。"}
    </p>
  );
}
