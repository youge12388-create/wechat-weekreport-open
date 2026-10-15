"use client";

import React from "react";
import { AlertTriangle, Check, Send } from "lucide-react";
import { Avatar, Badge, Button, EmptyState, ErrorState, LoadingBlock } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";
import type {
  ActionMessage,
  ActionMessageDelivery,
  ActionMessageThread,
  ActionNotifyReadiness,
  ManagementActionAuthorRole,
} from "@/lib/types";

/**
 * 跟进事项的沟通会话。
 *
 * 管理侧与负责人侧共用**同一个组件**，只有三处差异由 props 决定：
 * 1. `role` 决定自己的消息靠哪边、对方的头像用哪个口径；
 * 2. `canNotify` 决定是否显示「同时通过企业微信通知他」；
 * 3. `pathPrefix` 决定请求打到 /api/management-actions 还是 /api/my-actions。
 *
 * 共用是刻意的：两侧各写一套会话 UI，迟早出现「一边显示已读一边没有」
 * 这种口径不一致，而这类差异在真机上很难被发现。
 */

type Props = {
  actionId: number;
  /** 当前使用者的身份：决定消息气泡靠左还是靠右。 */
  role: ManagementActionAuthorRole;
  pathPrefix: "/api/management-actions" | "/api/my-actions";
  /** 对方的名字（管理侧＝负责人姓名，负责人侧＝管理侧联系人）。 */
  peerName?: string | null;
  /** 管理侧才有「通知他」这个选项；负责人侧发消息一律自动通知管理侧。 */
  canNotify?: boolean;
  /** 投递前提不满足时的提示（管理侧用 notify_readiness 的结果传入）。 */
  readiness?: ActionNotifyReadiness | null;
  /** 头部右侧动作（例如「去编辑」）。 */
  headerAction?: React.ReactNode;
  className?: string;
};

function authorLabel(role: ManagementActionAuthorRole): string {
  return role === "admin" ? "管理者" : "负责人";
}

export function ConversationThread({
  actionId,
  role,
  pathPrefix,
  peerName,
  canNotify = false,
  readiness = null,
  headerAction,
  className,
}: Props) {
  const [thread, setThread] = React.useState<ActionMessageThread | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [body, setBody] = React.useState("");
  const [notifyPeer, setNotifyPeer] = React.useState(true);
  const [sending, setSending] = React.useState(false);
  const [sendError, setSendError] = React.useState<string | null>(null);
  const [delivery, setDelivery] = React.useState<ActionMessageDelivery | null>(null);
  const [markedRead, setMarkedRead] = React.useState(false);
  /** 正在撤回的消息 id；同一时刻只允许撤一条，避免连点。 */
  const [recalling, setRecalling] = React.useState<number | null>(null);
  const [recallNotice, setRecallNotice] = React.useState<string | null>(null);

  const load = React.useCallback(
    async (options: { silent?: boolean } = {}) => {
      if (!options.silent) setLoading(true);
      try {
        const data = await apiGet<ActionMessageThread>(
          `${pathPrefix}/${actionId}/messages`
        );
        setThread(data);
        setError(null);
        return data;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "会话加载失败");
        return null;
      } finally {
        setLoading(false);
      }
    },
    [actionId, pathPrefix]
  );

  React.useEffect(() => {
    setMarkedRead(false);
    void load();
  }, [load]);

  // 已读走**显式 POST**，不在 GET 里做：GET 会被轮询反复调用，
  // 把已读塞进去等于「打开页面看一眼就自动清未读」。
  React.useEffect(() => {
    if (!thread || thread.unread_count === 0 || markedRead) return;
    setMarkedRead(true);
    void apiPost(`${pathPrefix}/${actionId}/messages/read`, {}).catch(() => {
      // 标记失败不打扰用户：下次轮询还会再试，消息本身不会丢。
      setMarkedRead(false);
    });
  }, [thread, markedRead, actionId, pathPrefix]);

  // 轮询：页面隐藏时不请求（第 24 条的纪律，避免后台标签页持续打接口）。
  React.useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void load({ silent: true });
    }, 20000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function send() {
    const text = body.trim();
    if (!text || sending) return;
    setSending(true);
    setSendError(null);
    setDelivery(null);
    try {
      const response = await apiPost<{
        message: ActionMessage;
        delivery: ActionMessageDelivery | null;
        notified?: boolean;
      }>(`${pathPrefix}/${actionId}/messages`, {
        body: text,
        // 负责人侧不需要这个开关：他发消息就是为了让管理侧知道。
        ...(canNotify ? { notify: notifyPeer } : {}),
      });
      setBody("");
      setDelivery(response.delivery);
      // 发完立刻本地追加，不等下一次轮询 —— 否则输入的内容会「消失」半分钟。
      setThread((current) =>
        current
          ? { ...current, items: [...current.items, response.message] }
          : current
      );
      void load({ silent: true });
    } catch (reason) {
      setSendError(reason instanceof Error ? reason.message : "发送失败");
    } finally {
      setSending(false);
    }
  }

  const items = thread?.items ?? [];

  async function recall(messageId: number) {
    setRecalling(messageId);
    setSendError(null);
    try {
      const response = await apiPost<{
        recall: { recalled: boolean; reason: string | null };
        message: ActionMessage;
      }>(`${pathPrefix}/${actionId}/messages/${messageId}/recall`, {});
      setThread((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === messageId ? response.message : item
              ),
            }
          : current
      );
      if (!response.recall.recalled && response.recall.reason) {
        // 撤回没成功必须说出来：卡片还在对方手机里，而用户以为撤回完了。
        setRecallNotice(response.recall.reason);
      } else if (response.recall.reason) {
        setRecallNotice(response.recall.reason);
      }
    } catch (reason) {
      setRecallNotice(reason instanceof Error ? reason.message : "撤回失败");
    } finally {
      setRecalling(null);
    }
  }

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2 pb-3">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">
            {peerName ? `与 ${peerName} 的沟通` : "沟通记录"} · 共 {items.length} 条
          </p>
        </div>
        {headerAction}
      </div>

      {/* 投递前提分两级展示：blocking = 卡片发不出去；warning = 卡片能送到、
          但对方点开用不了（最典型的就是通知地址还指向本机）。
          两者混成一句「对方收不到通知」会让人以为消息发不出去而不敢发，
          而实际上卡片是能送到的 —— 这正是本次要修掉的误导。 */}
      {readiness && readiness.reasons.length > 0 && (
        <div
          className={cn(
            "mb-3 rounded-control px-3.5 py-3",
            readiness.can_notify ? "bg-warning-soft" : "bg-destructive-soft"
          )}
        >
          <p
            className={cn(
              "flex items-center gap-1.5 text-xs font-medium",
              readiness.can_notify ? "text-warning" : "text-destructive"
            )}
          >
            <AlertTriangle className="h-3.5 w-3.5" />
            {readiness.can_notify
              ? "卡片能送到，但对方点开后无法回复"
              : "对方收不到企业微信通知"}
          </p>
          <ul className="mt-1.5 space-y-1">
            {readiness.reasons.map((reason) => (
              <li
                key={reason.code}
                className={cn(
                  "text-xs leading-relaxed",
                  readiness.can_notify ? "text-warning" : "text-destructive"
                )}
              >
                · {reason.message}
              </li>
            ))}
          </ul>
          <p className="mt-1.5 text-xs text-muted-foreground">
            消息仍会记录在下方，对方登录后能看到。
          </p>
        </div>
      )}

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
        {loading && !thread ? (
          <LoadingBlock rows={3} />
        ) : error ? (
          <ErrorState message={error} onRetry={() => void load()} />
        ) : items.length === 0 ? (
          <EmptyState
            title="还没有沟通记录"
            desc={
              role === "admin"
                ? "发一条消息说明要推进的事，对方会收到企业微信通知。"
                : "有进展或困难时在这里说明，管理者会收到通知。"
            }
          />
        ) : (
          items.map((message) => {
            const mine = message.author_role === role;
            return (
              <div
                key={message.id}
                className={cn("flex gap-2.5", mine ? "flex-row-reverse" : "flex-row")}
              >
                <Avatar
                  name={message.author_name ?? authorLabel(message.author_role)}
                  size={28}
                  className="mt-0.5"
                />
                <div className={cn("min-w-0 max-w-[min(560px,80%)]", mine && "text-right")}>
                  <div
                    className={cn(
                      "flex flex-wrap items-center gap-2 text-xs text-muted-foreground",
                      mine && "justify-end"
                    )}
                  >
                    <span className="font-medium text-foreground">
                      {message.author_name ?? authorLabel(message.author_role)}
                    </span>
                    <span>{formatDateTime(message.created_at)}</span>
                    {message.recalled ? (
                      <Badge tone="neutral">已撤回通知</Badge>
                    ) : (
                      message.notify &&
                      message.notify.status !== "not_attempted" && (
                        <NotifyBadge notify={message.notify} />
                      )
                    )}
                    {/* 撤回入口：只给管理侧、只对真的推送过的消息。
                        可撤回性由后端判定（24 小时窗口 + 有没有 msgid），
                        前端不自己算 —— 否则会出现点了却没反应的按钮。 */}
                    {role === "admin" && message.notify?.can_recall && (
                      <button
                        type="button"
                        onClick={() => void recall(message.id)}
                        disabled={recalling !== null}
                        className="text-xs text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-destructive disabled:opacity-50"
                      >
                        {recalling === message.id ? "撤回中…" : "撤回通知"}
                      </button>
                    )}
                  </div>
                  <div
                    className={cn(
                      "mt-1 whitespace-pre-wrap rounded-control px-3.5 py-2.5 text-sm leading-relaxed",
                      message.recalled
                        ? // 已撤回的消息仍然留在会话里（记录不可篡改），但视觉上压暗，
                          // 让双方都明白「这条的卡片已经被收回去了」。
                          "bg-muted/40 text-muted-foreground line-through decoration-muted-foreground/40"
                        : mine
                          ? "bg-primary text-primary-foreground"
                          : "bg-muted/60 text-foreground"
                    )}
                  >
                    {message.body}
                  </div>
                  {message.notify?.recall_error && (
                    <p className="mt-1 text-xs text-warning">{message.notify.recall_error}</p>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* 发送区：常驻底部，不随消息滚动 */}
      <div className="mt-4 border-t border-border/70 pt-3">
        {sendError && (
          <p className="mb-2 rounded-control bg-destructive-soft px-3 py-2 text-xs text-destructive">
            {sendError}
          </p>
        )}
        {recallNotice && (
          <p className="mb-2 rounded-control bg-warning-soft px-3 py-2 text-xs text-warning">
            {recallNotice}
          </p>
        )}
        {delivery && <DeliveryNotice delivery={delivery} />}
        <textarea
          value={body}
          onChange={(event) => setBody(event.target.value)}
          onKeyDown={(event) => {
            // Ctrl/Cmd + Enter 发送：会话框里换行是常用操作，不能占掉 Enter。
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              void send();
            }
          }}
          rows={3}
          maxLength={5000}
          placeholder={
            role === "admin"
              ? "写下要他推进的事、要什么时间给结论…"
              : "说明进展、问题或需要的支持…"
          }
          className="w-full resize-y rounded-control border border-input bg-surface px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            {canNotify && (
              <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={notifyPeer}
                  onChange={(event) => setNotifyPeer(event.target.checked)}
                  className="h-3.5 w-3.5 rounded border-input"
                />
                同时通过企业微信通知他
              </label>
            )}
            <span className="text-xs text-muted-foreground">Ctrl/⌘ + Enter 发送</span>
          </div>
          <Button onClick={() => void send()} loading={sending} disabled={!body.trim()}>
            <Send className="h-3.5 w-3.5" />
            发送
          </Button>
        </div>
      </div>
    </div>
  );
}

/** 单条消息的投递状态徽标。 */
function NotifyBadge({ notify }: { notify: NonNullable<ActionMessage["notify"]> }) {
  if (notify.status === "sent") {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-success">
        <Check className="h-3 w-3" />
        已通知
      </span>
    );
  }
  if (notify.status === "undelivered") {
    return (
      <Badge tone="amber">
        未投递{notify.error ? ` · ${shorten(notify.error)}` : ""}
      </Badge>
    );
  }
  if (notify.status === "failed") {
    return <Badge tone="red">通知失败</Badge>;
  }
  if (notify.status === "skipped") {
    return <Badge tone="neutral">未重复通知</Badge>;
  }
  return null;
}

/** 发送后的一次性投递结果提示。 */
function DeliveryNotice({ delivery }: { delivery: ActionMessageDelivery }) {
  if (delivery.status === "sent" && delivery.undelivered.length === 0) {
    return (
      <p className="mb-2 flex items-center gap-1.5 rounded-control bg-success-soft px-3 py-2 text-xs text-success">
        <Check className="h-3.5 w-3.5" />
        已通过企业微信通知 {delivery.delivered.map((item) => item.name).join("、")}
      </p>
    );
  }
  if (delivery.status === "sent" && delivery.undelivered.length > 0) {
    return (
      <div className="mb-2 rounded-control bg-warning-soft px-3 py-2">
        <p className="text-xs font-medium text-warning">
          部分未送达：{delivery.undelivered.map((item) => item.name ?? "未知").join("、")}
        </p>
        {delivery.undelivered.map((item) => (
          <p key={`${item.id}-${item.reason}`} className="mt-1 text-xs text-warning">
            · {item.reason}
          </p>
        ))}
      </div>
    );
  }
  if (delivery.status === "skipped") {
    return (
      <p className="mb-2 rounded-control bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
        {delivery.skipped_reason ?? "未发送企业微信通知"}
      </p>
    );
  }
  if (delivery.status === "failed") {
    return (
      <p className="mb-2 rounded-control bg-destructive-soft px-3 py-2 text-xs text-destructive">
        企业微信通知发送失败：{delivery.error ?? "未知原因"}（消息已保存）
      </p>
    );
  }
  if (delivery.status === "undelivered") {
    return (
      <div className="mb-2 rounded-control bg-warning-soft px-3 py-2">
        <p className="text-xs font-medium text-warning">对方未收到企业微信通知</p>
        {delivery.undelivered.map((item) => (
          <p key={`${item.id}-${item.reason}`} className="mt-1 text-xs text-warning">
            · {item.reason}
          </p>
        ))}
      </div>
    );
  }
  return null;
}

function shorten(value: string, limit = 40): string {
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}
