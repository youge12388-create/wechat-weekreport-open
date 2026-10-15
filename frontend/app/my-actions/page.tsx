"use client";

import React from "react";
import Link from "next/link";
import { ArrowUpRight, CalendarClock, CircleAlert, Inbox } from "lucide-react";
import { Badge, Card, CardHeader, EmptyState, ErrorState, LoadingBlock, KpiCard, PageIntro } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type { ManagementAction, ManagementActionListResponse } from "@/lib/types";

/**
 * 「发给我的事项」：负责人视角的收件箱。
 *
 * 存在的理由：企微通知会被后续消息刷走，员工需要一个稳定的地方回看
 * 「现在有哪些事在等我」。这一页只看得到指派给自己的事项。
 * 后端 `/api/my-actions` 强制所有者校验，前端不做过滤也只拿得到自己的数据。
 */

function statusLabel(status: ManagementAction["status"]): string {
  return (
    {
      OPEN: "待处理",
      IN_PROGRESS: "处理中",
      WAITING: "等待反馈",
      RESOLVED: "已解决",
      CANCELLED: "已取消",
    } as Record<ManagementAction["status"], string>
  )[status];
}

function statusTone(status: ManagementAction["status"]): "red" | "amber" | "green" | "blue" | "neutral" {
  if (status === "RESOLVED") return "green";
  if (status === "CANCELLED") return "neutral";
  if (status === "WAITING") return "amber";
  if (status === "IN_PROGRESS") return "blue";
  return "red";
}

export default function MyActionsPage() {
  const [showClosed, setShowClosed] = React.useState(false);
  const params = showClosed ? { include_closed: true } : {};
  const data = useAsyncData<ManagementActionListResponse>(
    () => apiGet("/api/my-actions", params),
    [showClosed]
  );

  const items = data.data?.items ?? [];
  const unreadTotal = data.data?.unread_total ?? 0;
  const openItems = items.filter(
    (item) => item.status !== "RESOLVED" && item.status !== "CANCELLED"
  );

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">发给我的事项</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          管理者指派给你跟进的事，有问题直接在详情里回复。
        </p>
      </div>

      <PageIntro>
        这里只显示指派给你的跟进事项；回复后管理者会收到通知。
        已解决的事项默认不显示，需要回看时点右上角「包含已结束」。
      </PageIntro>

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <KpiCard
          label="待我处理"
          icon={CircleAlert}
          tone={openItems.length > 0 ? "amber" : "green"}
          valueTone={openItems.length > 0 ? "amber" : "green"}
          value={data.error ? "—" : openItems.length}
          loading={data.loading}
          unit={data.loading || data.error ? undefined : "件"}
          hint="尚未结束的事项"
        />
        <KpiCard
          label="未读回复"
          icon={Inbox}
          tone={unreadTotal > 0 ? "amber" : "green"}
          valueTone={unreadTotal > 0 ? "amber" : "green"}
          value={data.error ? "—" : unreadTotal}
          loading={data.loading}
          unit={data.loading || data.error ? undefined : "条"}
          hint="管理者发来的新消息"
        />
      </div>

      <Card className="overflow-hidden p-0">
        <div className="card-head card-head-line">
          <CardHeader
            title="事项列表"
            desc={showClosed ? "含已解决与已取消" : "只显示未结束的事项"}
            action={
              <button
                type="button"
                onClick={() => setShowClosed((value) => !value)}
                className="text-xs font-medium text-primary hover:underline"
              >
                {showClosed ? "只看未结束" : "包含已结束"}
              </button>
            }
          />
        </div>

        {data.loading ? (
          <LoadingBlock rows={4} />
        ) : data.error ? (
          <div className="px-4 py-4">
            <ErrorState message={data.error} onRetry={data.reload} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            title={showClosed ? "没有任何事项" : "当前没有需要你处理的事项"}
            desc="管理者把事项指派给你时，你会收到企业微信通知，并在这里看到它。"
          />
        ) : (
          <div className="space-y-2.5 px-4 pb-4 pt-4">
            {items.map((item) => (
              <Link
                key={item.id}
                href={`/my-actions/${item.id}`}
                className="block rounded-card border border-border bg-surface px-4 py-3.5 transition-colors hover:border-primary/25"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{item.title}</span>
                      <Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge>
                      {(item.unread_count ?? 0) > 0 && (
                        <span className="rounded-pill bg-destructive px-2 py-0.5 text-xs font-medium text-destructive-foreground">
                          {item.unread_count} 条新消息
                        </span>
                      )}
                    </div>
                    {item.last_message && (
                      <p className="mt-1.5 line-clamp-1 text-xs text-muted-foreground">
                        {item.last_message.author_role === "admin" ? "管理者" : "你"}：
                        {item.last_message.excerpt}
                      </p>
                    )}
                    <p className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      {item.due_date && (
                        <span className="inline-flex items-center gap-1">
                          <CalendarClock className="h-3 w-3" />
                          计划完成日 {formatDate(item.due_date)}
                        </span>
                      )}
                      {item.next_step && <span>下一步：{item.next_step}</span>}
                    </p>
                  </div>
                  <ArrowUpRight className="mt-1 h-3.5 w-3.5 shrink-0 text-primary" />
                </div>
              </Link>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
