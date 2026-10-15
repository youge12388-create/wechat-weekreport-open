"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ClipboardCheck,
  CircleAlert,
  FileText,
  Inbox,
  LogOut,
  Menu,
  Radar,
  Settings,
  Sparkles,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { subscribeToApiLoading } from "@/lib/api";
import { NotificationBell, NotificationProvider } from "@/components/notification-center";
import { SessionProvider, useSession } from "@/components/session-provider";
import { ThemeProvider } from "@/components/theme-provider";

/**
 * 导航用「任务语言」而不是「数据表语言」：用户进来就知道该点哪儿。
 * 只保留老板每天真正会走的主路径；员工/部门页从管理简报和处理中心进入具体对象。
 */
type NavItem = { href: string; label: string; icon: typeof ClipboardCheck };

const NAV_GROUPS: { label: string | null; items: NavItem[] }[] = [
  {
    label: null,
    items: [
      { href: "/dashboard", label: "管理简报", icon: ClipboardCheck },
      { href: "/operating-dashboard", label: "经营看板", icon: Radar },
      { href: "/reports", label: "周报查询", icon: FileText },
      { href: "/blockers", label: "处理中心", icon: CircleAlert },
      { href: "/ai", label: "周报智能体", icon: Sparkles },
    ],
  },
];

/**
 * 只读成员（企业微信登录的普通同事）在站内只有「发给我的事项」。
 *
 * 以前这里是把管理导航**追加**一个入口给他，其余导航照旧显示 —— 结果同事登录后
 * 能看到管理简报、周报查询、处理中心、周报智能体，点进去就是全公司周报与人事数据。
 * 那是隐私泄漏，不是「只读权限」。现在 member 只看到自己那一个入口。
 *
 * 后端 AuthMiddleware 已经把 member 限制在 `/api/my-actions`（其余一律 403），
 * 所以这里隐藏导航是**避免他点到 403**，真正的边界在后端。
 */
const MEMBER_NAV_ITEM: NavItem = {
  href: "/my-actions",
  label: "发给我的事项",
  icon: Inbox,
};

/**
 * 部门管理员（dept_admin）：看本部门的管理简报 / 周报 / 处理中心 / 周报智能体，
 * 不给经营看板（全公司经营指标）与系统设置（超管专属）。
 * 数据边界在后端 data_scope 按管辖部门子树收口，这里隐藏只是避免点到 403。
 */
const DEPT_ADMIN_NAV_ITEMS: NavItem[] = [
  { href: "/dashboard", label: "管理简报", icon: ClipboardCheck },
  { href: "/reports", label: "周报查询", icon: FileText },
  { href: "/blockers", label: "处理中心", icon: CircleAlert },
  { href: "/ai", label: "周报智能体", icon: Sparkles },
];

/** dept_admin 落到这些路径时送回管理简报（书签 / 旧链接）。
 *  员工/部门页不在此列：后端已按管辖子树收口，页面可正常展示本部门数据。 */
const DEPT_ADMIN_HIDDEN_PREFIXES = ["/operating-dashboard", "/settings", "/actions"];

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        /* 选中态：深蓝黑实心胶囊、白字、无阴影。未选中：无底色、灰蓝文字、浅灰 hover。
           只过渡颜色，不做位移/发光，150ms 足够跟手又不花哨。 */
        "flex h-12 items-center gap-3 rounded-nav px-4 text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
        active
          ? "bg-nav-active font-medium text-nav-active-foreground"
          : "text-nav-idle hover:bg-muted hover:text-foreground"
      )}
    >
      <Icon className="h-4 w-4 shrink-0" />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLogin = pathname.startsWith("/login");
  const [mobileNavOpen, setMobileNavOpen] = React.useState(false);

  React.useEffect(() => {
    setMobileNavOpen(false);
  }, [pathname]);

  // 登录页不套侧栏与导航，单独整屏呈现
  if (isLogin) {
    return <div className="min-h-screen">{children}</div>;
  }

  return (
    <ThemeProvider>
      <SessionProvider>
        <NotificationProvider>
          <ShellFrame
            pathname={pathname}
            mobileNavOpen={mobileNavOpen}
            setMobileNavOpen={setMobileNavOpen}
          >
            {children}
          </ShellFrame>
        </NotificationProvider>
      </SessionProvider>
    </ThemeProvider>
  );
}

/** Show a short route cue and keep it active for slower background network reads. */
function ApiLoadingIndicator({ pathname }: { pathname: string }) {
  const [routeActive, setRouteActive] = React.useState(false);
  const [networkActive, setNetworkActive] = React.useState(false);

  React.useEffect(() => {
    setRouteActive(true);
    const timer = window.setTimeout(() => setRouteActive(false), 260);
    return () => window.clearTimeout(timer);
  }, [pathname]);

  React.useEffect(() => {
    let showTimer: number | undefined;
    const unsubscribe = subscribeToApiLoading((activeCount) => {
      if (activeCount > 0) {
        if (showTimer === undefined) {
          showTimer = window.setTimeout(() => {
            showTimer = undefined;
            setNetworkActive(true);
          }, 180);
        }
        return;
      }

      if (showTimer !== undefined) {
        window.clearTimeout(showTimer);
        showTimer = undefined;
      }
      setNetworkActive(false);
    });

    return () => {
      if (showTimer !== undefined) window.clearTimeout(showTimer);
      unsubscribe();
    };
  }, []);

  const visible = routeActive || networkActive;

  return (
    <div
      className={cn(
        "pointer-events-none fixed inset-x-0 top-0 z-[70] h-0.5 overflow-hidden transition-opacity duration-150",
        visible ? "opacity-100" : "opacity-0"
      )}
      aria-hidden="true"
    >
      {visible && <span className="api-loading-progress absolute inset-y-0 left-0 w-[38%] rounded-full bg-accent" />}
    </div>
  );
}

/** 侧栏主体。需要读会话（身份与角色），因此放在 SessionProvider 内层。 */
function ShellFrame({
  children,
  pathname,
  mobileNavOpen,
  setMobileNavOpen,
}: {
  children: React.ReactNode;
  pathname: string;
  mobileNavOpen: boolean;
  setMobileNavOpen: (open: boolean) => void;
}) {
  const { session, isAdmin, isDeptAdmin, loading } = useSession();

  // 只读成员只有「发给我的事项」这一页。若他停在管理页（旧链接、书签、
  // 手输地址），这里把他送回自己那一页：后端对这些路径一律 403，
  // 留在原页只会满屏报错。等会话加载完再判断，避免刚进来就误跳。
  const authenticated = Boolean(session?.authenticated);
  const isMemberOnly = !loading && authenticated && !isAdmin && !isDeptAdmin;
  const onMemberPage = pathname.startsWith(MEMBER_NAV_ITEM.href);
  const onDeptAdminHiddenPage =
    isDeptAdmin && DEPT_ADMIN_HIDDEN_PREFIXES.some((p) => pathname.startsWith(p));
  React.useEffect(() => {
    if (!loading && !authenticated) {
      window.location.replace("/login");
    }
  }, [authenticated, loading]);
  React.useEffect(() => {
    if (isMemberOnly && !onMemberPage) {
      window.location.replace(MEMBER_NAV_ITEM.href);
    }
  }, [isMemberOnly, onMemberPage]);
  React.useEffect(() => {
    if (onDeptAdminHiddenPage) {
      window.location.replace("/dashboard");
    }
  }, [onDeptAdminHiddenPage]);

  // 会话确认前不渲染任何业务页或管理导航，避免普通成员在这段窗口里看到、
  // 甚至点到管理页面。旧链接落到管理路径时也只显示转场提示，等 replace 完成。
  if (loading || !authenticated) {
    return <AuthLoadingState />;
  }
  if (isMemberOnly && !onMemberPage) {
    return <MemberRedirectState />;
  }
  if (onDeptAdminHiddenPage) {
    return <DeptAdminRedirectState />;
  }

  const navGroups = isMemberOnly
    ? [{ label: null, items: [MEMBER_NAV_ITEM] }]
    : isDeptAdmin
      ? [{ label: null, items: DEPT_ADMIN_NAV_ITEMS }]
      : NAV_GROUPS;

  const logout = async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", cache: "no-store" });
    } finally {
      window.location.href = "/login";
    }
  };

  const isActive = (href: string) =>
    pathname === href || pathname.startsWith(href + "/");

  return (
    <>
      <ApiLoadingIndicator pathname={pathname} />
      <div className="flex min-h-screen">
        <button
          type="button"
          aria-label="关闭导航"
          aria-hidden={!mobileNavOpen}
          tabIndex={mobileNavOpen ? 0 : -1}
          className={cn(
            "fixed inset-0 z-30 bg-foreground/30 transition-opacity duration-200 ease-out md:hidden",
            mobileNavOpen ? "opacity-100" : "pointer-events-none opacity-0"
          )}
          onClick={() => setMobileNavOpen(false)}
        />
        {/* 悬浮卡片式侧栏：四周留 16px，高度 calc(100vh - 32px)，24px 圆角 + 极弱大扩散阴影，
            让面板浮在浅冷灰画布之上。不是玻璃（不做模糊），移动端仍是整高抽屉。 */}
        <aside className={cn(
          "fixed left-4 top-4 z-40 flex w-56 flex-col overflow-hidden panel transition-transform duration-[250ms] ease-[cubic-bezier(.22,1,.36,1)] md:z-30 md:translate-x-0",
          "h-[calc(100vh-2rem)]",
          mobileNavOpen ? "translate-x-0" : "-translate-x-full"
        )}>
          {/* 品牌区：紧凑，不抢导航的位置 */}
          <div className="flex items-center gap-2.5 px-5 py-4">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-control bg-nav-active text-nav-active-foreground">
              <ClipboardCheck className="h-4 w-4" />
            </div>
            <div className="min-w-0 leading-tight">
              <p className="truncate text-sm font-semibold">周报平台</p>
              <p className="truncate text-xs text-muted-foreground">企业微信 · 自动汇总</p>
            </div>
          </div>

          {/* 导航紧接品牌下方自然展开；不居中悬空，但项与项之间留足呼吸感。
              member 看到的是 navGroups 换过的单页导航，不在这里再做条件分支。 */}
          <nav className="flex-1 overflow-y-auto px-3 pt-1">
            {navGroups.map((group, index) => (
              <div key={group.label ?? `group-${index}`} className={cn(index > 0 && "mt-5")}>
                {group.label && (
                  <p className="px-4 pb-2 text-xs font-medium text-muted-foreground/80">
                    {group.label}
                  </p>
                )}
                <div className="space-y-1.5">
                  {group.items.map((item) => (
                    <NavLink key={item.href} item={item} active={isActive(item.href)} />
                  ))}
                </div>
              </div>
            ))}
          </nav>

          {/* 底部动作区固定在面板底部，与主导航形成视觉分区。
              消息中心与系统设置都只给管理员：它们的内容是全公司级的状态与配置，
              后端对 member 一律 403（消息中心读的 /api/notifications/feed
              含全公司同步状态与未提交名单）。给 member 显示只会让他点到报错。 */}
          <div className="space-y-1.5 px-3 pb-3 pt-3">
            {isAdmin && <NotificationBell />}
            {isAdmin && (
              <NavLink
                item={{ href: "/settings", label: "系统设置", icon: Settings }}
                active={isActive("/settings")}
              />
            )}
            {!loading && session?.authenticated && (
              <div className="flex items-center gap-2 px-4 pt-1 text-xs text-muted-foreground">
                <span className="truncate">{session.name}</span>
                {!isAdmin && (
                  <span className="shrink-0 rounded-pill bg-muted px-2 py-0.5">
                    {isDeptAdmin ? "部门管理员" : "只读"}
                  </span>
                )}
              </div>
            )}
            <button
              onClick={logout}
              className="flex h-12 w-full items-center gap-3 rounded-nav px-4 text-sm text-nav-idle transition-colors duration-150 hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <LogOut className="h-4 w-4 shrink-0" />
              退出登录
            </button>
          </div>
        </aside>

        <div className="fixed inset-x-0 top-0 z-20 flex h-14 items-center justify-between gap-3 border-b border-border bg-surface px-4 md:hidden">
          <button
            type="button"
            aria-label={mobileNavOpen ? "关闭导航" : "打开导航"}
            aria-expanded={mobileNavOpen}
            onClick={() => setMobileNavOpen(!mobileNavOpen)}
            className="inline-flex h-9 w-9 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="relative h-5 w-5">
              <Menu
                aria-hidden
                className={cn(
                  "absolute inset-0 h-5 w-5 transition-[opacity,transform] duration-150",
                  mobileNavOpen ? "rotate-45 scale-75 opacity-0" : "rotate-0 scale-100 opacity-100"
                )}
              />
              <X
                aria-hidden
                className={cn(
                  "absolute inset-0 h-5 w-5 transition-[opacity,transform] duration-150",
                  mobileNavOpen ? "rotate-0 scale-100 opacity-100" : "-rotate-45 scale-75 opacity-0"
                )}
              />
            </span>
          </button>
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-control bg-nav-active text-nav-active-foreground">
              <ClipboardCheck className="h-3.5 w-3.5" />
            </span>
            <span className="text-sm font-semibold">周报平台</span>
          </div>
          {/* 移动端顶栏也需要消息入口：侧栏是抽屉，收起来就看不见底部按钮了。
              同样只给管理员 —— member 没有全公司级的待处理项。 */}
          {isAdmin && <NotificationBell variant="icon" />}
        </div>

        {/* 主内容左边距 = 侧栏 left 16px + 宽 224px + 16px 间隙 = 256px */}
        <main className="ml-0 min-w-0 flex-1 px-4 pb-10 pt-20 md:ml-[16rem] md:py-6 md:pl-0 md:pr-6">
          <div className="mx-auto w-full max-w-[1400px]">{children}</div>
        </main>
      </div>
    </>
  );
}

function AuthLoadingState() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 text-sm text-muted-foreground">
      正在确认登录状态…
    </div>
  );
}

function MemberRedirectState() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 text-sm text-muted-foreground">
      正在打开发给你的事项…
    </div>
  );
}

function DeptAdminRedirectState() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 text-sm text-muted-foreground">
      正在打开本部门管理简报…
    </div>
  );
}
