"use client";

import React from "react";
import { apiGet } from "@/lib/api";
import type { SessionInfo } from "@/lib/types";

/**
 * 当前登录会话。
 *
 * 只用来决定「界面显示什么」，**不作为权限判断依据** —— 真正的拦截在后端
 * AuthMiddleware：只读成员的任何写操作都会被 403。
 * 前端据此隐藏写入口，是为了不让用户点了才被拒，而不是为了安全。
 */

interface SessionContextValue {
  session: SessionInfo | null;
  loading: boolean;
  /** 是否为全公司管理员；加载中时为 false（先不显示写入口，避免闪烁）。 */
  isAdmin: boolean;
  /** 是否为部门管理员（dept_admin）：可见本部门数据面，无系统设置等公司级能力。 */
  isDeptAdmin: boolean;
  reload: () => void;
}

const SessionContext = React.createContext<SessionContextValue>({
  session: null,
  loading: true,
  isAdmin: false,
  isDeptAdmin: false,
  reload: () => {},
});

export function SessionProvider({
  children,
  initial = null,
}: {
  children: React.ReactNode;
  initial?: SessionInfo | null;
}) {
  const [session, setSession] = React.useState<SessionInfo | null>(initial);
  const [loading, setLoading] = React.useState(initial === null);
  const [tick, setTick] = React.useState(0);

  React.useEffect(() => {
    let cancelled = false;
    apiGet<SessionInfo>("/api/auth/me")
      .then((data) => {
        if (!cancelled) setSession(data);
      })
      .catch(() => {
        // 拿不到会话按未登录处理；具体的 401 跳转由 api.ts 统一负责
        if (!cancelled) setSession({ authenticated: false });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  const value = React.useMemo<SessionContextValue>(
    () => ({
      session,
      loading,
      isAdmin: Boolean(session?.authenticated && session.is_admin),
      isDeptAdmin: Boolean(
        session?.authenticated && session.role === "dept_admin"
      ),
      reload: () => setTick((t) => t + 1),
    }),
    [session, loading]
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  return React.useContext(SessionContext);
}
