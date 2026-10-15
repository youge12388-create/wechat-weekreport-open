"use client";

import React from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Info,
  RefreshCw,
  X,
} from "lucide-react";
import { apiGet } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime } from "@/lib/format";
import { usePolling } from "@/lib/use-polling";
import { useSession } from "@/components/session-provider";
import { useMotionPresence } from "@/components/ui";
import type { NotificationFeed, NotificationItem } from "@/lib/types";

/**
 * 消息中心：把同步报错、AI 故障和各周期提醒收进一个按钮里。
 *
 * 之前的做法是把这些提示直接铺在首页工具栏上 —— 一条可信 IP 报错占两行，
 * 把真正的业务指标（提交率、部门进度）挤出首屏，而且看完这一处还得自己
 * 去别的页面找下一处。现在统一收进消息中心，首页只留一句「有 N 条待处理」。
 *
 * 已读状态刻意放在 localStorage 而不是后端：
 *   - 这是「这台设备看到哪儿了」，不是业务数据，多端各自记忆才合理
 *   - 记忆键是 `id@发生时间`，同一问题修好又复发时会重新变成未读，
 *     不会因为「这个 id 我看过」而把新故障静默吞掉
 */

const READ_STORAGE_KEY = "weekly-report-notification-read";
/** 只保留最近若干条已读键，避免 localStorage 随消息累积无限增长。 */
const READ_KEEP = 200;
const POLL_INTERVAL_MS = 60_000;

function readKey(item: NotificationItem): string {
  return `${item.id}@${item.at ?? ""}`;
}

function loadReadKeys(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(READ_STORAGE_KEY);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? new Set(parsed.filter((value): value is string => typeof value === "string"))
      : new Set();
  } catch {
    /* 存储坏了不该让消息中心打不开，当成全未读即可 */
    return new Set();
  }
}

function saveReadKeys(keys: Set<string>): void {
  if (typeof window === "undefined") return;
  try {
    const list = Array.from(keys).slice(-READ_KEEP);
    window.localStorage.setItem(READ_STORAGE_KEY, JSON.stringify(list));
  } catch {
    /* 隐私模式下 localStorage 可能不可写，静默降级为「本次会话内已读」 */
  }
}

const LEVEL_STYLES: Record<
  NotificationItem["level"],
  { icon: typeof AlertTriangle; ring: string; chip: string; label: string }
> = {
  error: {
    icon: AlertTriangle,
    ring: "bg-destructive-soft text-destructive",
    chip: "bg-destructive-soft text-destructive ring-destructive/20",
    label: "需要处理",
  },
  warning: {
    icon: CircleAlert,
    ring: "bg-warning-soft text-warning",
    chip: "bg-warning-soft text-warning ring-warning/20",
    label: "待跟进",
  },
  info: {
    icon: Info,
    ring: "bg-primary-soft text-primary",
    chip: "bg-primary-soft text-primary ring-primary/20",
    label: "知会",
  },
};

/** 触发消息中心打开的动作，供首页「有 N 条待处理消息」这类入口复用。 */
type CenterContextValue = {
  open: boolean;
  openCenter: () => void;
  closeCenter: () => void;
  unreadCount: number;
  /** 需要注意的总数（报错 + 待跟进），不含纯知会。 */
  attentionCount: number;
  total: number;
  refresh: () => void;
};

const NotificationCenterContext = React.createContext<CenterContextValue | null>(null);

export function useNotificationCenter(): CenterContextValue {
  const value = React.useContext(NotificationCenterContext);
  if (!value) {
    throw new Error("useNotificationCenter 必须在 NotificationProvider 内部使用");
  }
  return value;
}

function ItemCard({ item }: { item: NotificationItem }) {
  const [open, setOpen] = React.useState(false);
  const style = LEVEL_STYLES[item.level] ?? LEVEL_STYLES.info;
  const Icon = style.icon;
  const when = formatDateTime(item.at);

  return (
    <li className="rounded-card border border-border bg-surface p-3.5">
      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-control",
            style.ring
          )}
        >
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <p className="text-sm font-semibold">{item.title}</p>
            <span
              className={cn(
                "rounded-pill px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset",
                style.chip
              )}
            >
              {style.label}
            </span>
            {when !== "-" && (
              <span className="text-xs text-muted-foreground">{when}</span>
            )}
          </div>
          <p className="mt-1.5 text-sm leading-relaxed text-foreground">{item.summary}</p>
          {item.hint && (
            <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
              处理：{item.hint}
            </p>
          )}

          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {item.href && (
              <Link
                href={item.href}
                className="inline-flex h-7 items-center gap-1 rounded-pill bg-primary px-3 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                {item.action_label ?? "去处理"}
              </Link>
            )}
            {item.raw && (
              <button
                type="button"
                onClick={() => setOpen((value) => !value)}
                aria-expanded={open}
                className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                {open ? "收起技术细节" : "查看技术细节"}
                <ChevronDown
                  className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")}
                />
              </button>
            )}
          </div>
          {open && item.raw && (
            <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-control bg-muted/60 px-3 py-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
              {item.raw}
            </pre>
          )}
        </div>
      </div>
    </li>
  );
}

/**
 * 全局消息中心：负责取数、轮询、已读记忆，并渲染抽屉本体。
 * 放在 Shell 里包住主内容，任何页面都能通过 useNotificationCenter 打开它。
 */
export function NotificationProvider({ children }: { children: React.ReactNode }) {
  const { isAdmin } = useSession();
  const [open, setOpen] = React.useState(false);
  const present = useMotionPresence(open);
  const [feed, setFeed] = React.useState<NotificationFeed | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [readKeys, setReadKeys] = React.useState<Set<string>>(new Set());
  const [hydrated, setHydrated] = React.useState(false);

  // 首帧不能碰 localStorage（服务端没有 window），挂载后再读。
  React.useEffect(() => {
    setReadKeys(loadReadKeys());
    setHydrated(true);
  }, []);

  // 抽屉开合经 ref 转发进 load：只在抽屉可见时才置 loading 态，
  // 后台轮询（每 60 秒一次）不会让抽屉内已有的内容闪烁加载骨架。
  const openRef = React.useRef(open);
  openRef.current = open;
  const load = React.useCallback(async () => {
    if (openRef.current) setLoading(true);
    setError(null);
    try {
      // 消息中心是「现在有什么坏着」的唯一入口，必须每次都拿最新。
      // 它是全站共享的一次请求（不是每页各一次），绕开 GET 缓存不影响切页性能。
      setFeed(
        await apiGet<NotificationFeed>("/api/notifications/feed", undefined, {
          bypassCache: true,
        })
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载消息失败");
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    // 消息中心内容是全公司级状态（同步情况、全公司未提交名单），后端对
    // dept_admin / member 一律 403。铃铛入口已按 isAdmin 隐藏，这里把
    // 取数也一起门控：非管理员不发请求，避免控制台 403 噪音。
    if (!isAdmin) return;
    void load();
  }, [load, isAdmin]);
  // 轮询交给 usePolling：页面切到后台自动暂停，回到前台立即补刷一次。
  // 消息中心没有「任务完成」的概念，不设最大轮询时长。
  usePolling(() => void load(), POLL_INTERVAL_MS, { enabled: isAdmin });

  const items = feed?.items ?? [];
  const unreadCount = hydrated
    ? items.filter((item) => !readKeys.has(readKey(item))).length
    : 0;

  const markAllRead = React.useCallback((list: NotificationItem[]) => {
    if (!list.length) return;
    setReadKeys((previous) => {
      const next = new Set(previous);
      for (const item of list) next.add(readKey(item));
      saveReadKeys(next);
      return next;
    });
  }, []);

  const openCenter = React.useCallback(() => {
    setOpen(true);
    // ref 在渲染后才更新；这里同步置位，保证打开后的第一次 load 会显示 loading。
    openRef.current = true;
    // 打开即视为已读：徽标数字只负责「有新东西要你看」，看完就清干净。
    markAllRead(items);
    // 打开时顺手刷新一次，避免看到的是最多一分钟前的快照。
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, markAllRead, load]);

  const closeCenter = React.useCallback(() => setOpen(false), []);

  // Esc 关闭，与 Modal 保持同一套键盘约定。
  React.useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  const value = React.useMemo<CenterContextValue>(
    () => ({
      open,
      openCenter,
      closeCenter,
      unreadCount,
      // 只数「报错 + 待跟进」：纯知会（队列在跑、还没生成总结）不该被说成待处理。
      attentionCount: (feed?.counts.error ?? 0) + (feed?.counts.warning ?? 0),
      total: feed?.total ?? 0,
      refresh: () => void load(),
    }),
    [open, openCenter, closeCenter, unreadCount, feed?.counts, feed?.total, load]
  );

  return (
    <NotificationCenterContext.Provider value={value}>
      {children}
      {present && (
        <div
          className={cn("fixed inset-0 z-50 flex justify-end", !open && "pointer-events-none")}
          role="dialog"
          aria-modal={open}
          aria-hidden={!open}
          inert={!open}
          aria-label="消息中心"
        >
          <button
            type="button"
            aria-label="关闭消息中心"
            className={cn(
              "absolute inset-0 bg-ink/35",
              open ? "ui-motion-fade-in" : "ui-motion-fade-out"
            )}
            onClick={closeCenter}
          />
          {/* 右侧抽屉：列表可能很长，所以整块面板内部滚动，头部保持可见。 */}
          <div
            className={cn(
              "relative flex h-full w-full max-w-[440px] flex-col border-l border-border bg-background shadow-panel",
              open ? "ui-motion-sheet-in" : "ui-motion-sheet-out"
            )}
          >
            <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
              <div>
                <h2 className="card-title">消息中心</h2>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {feed
                    ? `共 ${feed.total} 条${feed.counts.error ? ` · ${feed.counts.error} 条需要处理` : ""}`
                    : "正在读取…"}
                </p>
              </div>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => void load()}
                  disabled={loading}
                  title="重新检查"
                  className="inline-flex h-8 items-center gap-1.5 rounded-control px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
                >
                  <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
                  重新检查
                </button>
                <button
                  type="button"
                  onClick={closeCenter}
                  aria-label="关闭"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-5 py-4">
              {error ? (
                <div className="rounded-card border border-destructive/20 bg-destructive-soft px-4 py-3.5 text-sm">
                  <p className="font-medium text-destructive">读取消息失败</p>
                  <p className="mt-1 text-muted-foreground">{error}</p>
                  <button
                    type="button"
                    onClick={() => void load()}
                    className="mt-2 text-xs font-medium text-destructive underline underline-offset-2"
                  >
                    重试
                  </button>
                </div>
              ) : !feed && loading ? (
                <p className="py-10 text-center text-sm text-muted-foreground">正在读取…</p>
              ) : items.length === 0 ? (
                <div className="flex flex-col items-center gap-2 py-14 text-center">
                  <span className="flex h-10 w-10 items-center justify-center rounded-control bg-success-soft text-success">
                    <CheckCircle2 className="h-5 w-5" />
                  </span>
                  <p className="text-sm font-medium">没有待处理的消息</p>
                  <p className="max-w-xs text-xs leading-relaxed text-muted-foreground">
                    同步、AI 分析和本周提醒都正常。出现报错或需要跟进的事项时会在这里出现。
                  </p>
                </div>
              ) : (
                <ul className="space-y-2.5">
                  {items.map((item) => (
                    <ItemCard key={readKey(item)} item={item} />
                  ))}
                </ul>
              )}
            </div>

            {/* 只读聚合，不落库；把「什么时候检查过」显式写出来，避免用户以为它一直在实时盯。 */}
            <div className="border-t border-border px-5 py-3 text-xs text-muted-foreground">
              每 60 秒自动检查一次
              {feed?.generated_at ? ` · 最近检查 ${formatDateTime(feed.generated_at)}` : ""}
              {feed ? ` · 统计周期 ${feed.period.start} ~ ${feed.period.end}` : ""}
            </div>
          </div>
        </div>
      )}
    </NotificationCenterContext.Provider>
  );
}

/** 消息入口：侧栏用整行按钮（icon 变体给移动端顶栏）。 */
export function NotificationBell({
  className,
  variant = "nav",
}: {
  className?: string;
  variant?: "nav" | "icon";
}) {
  const { openCenter, unreadCount, total } = useNotificationCenter();
  const hasUnread = unreadCount > 0;
  const hasIssues = total > 0;
  const badge = unreadCount > 99 ? "99+" : unreadCount;

  if (variant === "icon") {
    return (
      <button
        type="button"
        onClick={openCenter}
        aria-label={hasUnread ? `消息中心，${unreadCount} 条未读` : "消息中心"}
        className={cn(
          "relative inline-flex h-9 w-9 items-center justify-center rounded-control transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          hasUnread
            ? "text-destructive hover:bg-destructive-soft"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
          className
        )}
      >
        <Bell className="h-5 w-5" />
        {hasUnread && (
          <span className="absolute right-0.5 top-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-pill bg-destructive px-1 text-[10px] font-semibold text-destructive-foreground">
            {badge}
          </span>
        )}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={openCenter}
      title={
        hasIssues ? `消息中心 · ${total} 条待处理` : "消息中心 · 暂无待处理"
      }
      className={cn(
        "flex h-12 w-full items-center gap-3 rounded-nav px-4 text-sm transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        "text-nav-idle hover:bg-muted hover:text-foreground",
        className
      )}
    >
      <span className="relative flex h-4 w-4 shrink-0 items-center justify-center">
        {hasUnread ? <BellIconWithDot /> : <Bell className="h-4 w-4" />}
      </span>
      <span className="truncate">消息中心</span>
      {hasUnread ? (
        <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-pill bg-destructive px-1.5 text-[11px] font-semibold text-destructive-foreground">
          {badge}
        </span>
      ) : (
        hasIssues && (
          <span
            className="ml-auto inline-flex h-1.5 w-1.5 rounded-pill bg-muted-foreground/40"
            aria-hidden
          />
        )
      )}
    </button>
  );
}

/** 有未读时铃铛加一点重量（红色描边 + 小圆点），不额外引入第二种图标语义。 */
function BellIconWithDot() {
  return (
    <span className="relative">
      <Bell className="h-4 w-4 text-destructive" strokeWidth={2.4} />
      <span className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 rounded-pill bg-destructive" />
    </span>
  );
}
