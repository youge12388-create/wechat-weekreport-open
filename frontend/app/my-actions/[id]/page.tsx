"use client";

import React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, CalendarClock, Link2 } from "lucide-react";
import { ConversationThread } from "@/components/conversation-thread";
import { Badge, Card, CardHeading, ErrorState, LoadingBlock } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type { ManagementAction } from "@/lib/types";

/**
 * 事项详情（负责人视角）。
 *
 * 页面上**不存在**内部备注、来源快照与变更记录 —— 不是前端藏起来，
 * 而是后端 `view=member` 就不返回这些字段（见 services/management_action_view.py）。
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

function MyActionDetailContent() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const action = useAsyncData<ManagementAction>(() => apiGet(`/api/my-actions/${id}`), [id]);

  if (!Number.isInteger(id) || id <= 0) {
    return <Card><ErrorState message="事项编号无效" /></Card>;
  }
  if (action.loading) return <Card><LoadingBlock rows={6} /></Card>;
  if (action.error || !action.data) {
    return (
      <Card>
        <ErrorState
          message={action.error ?? "找不到该事项（可能未指派给你）"}
          onRetry={action.reload}
        />
      </Card>
    );
  }
  const item = action.data;

  return (
    <div>
      <Link
        href="/my-actions"
        className="mb-3 inline-flex items-center gap-1.5 rounded-control px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> 返回我的事项
      </Link>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <Card className="p-0">
          <div className="card-head card-head-line">
            <CardHeading title="事项说明" />
          </div>
          <div className="space-y-3 px-4 py-4">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-base font-semibold leading-snug">{item.title}</h1>
                <Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge>
              </div>
              {item.description && (
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                  {item.description}
                </p>
              )}
            </div>
            <dl className="space-y-2 text-xs">
              <div className="flex items-center gap-2">
                <CalendarClock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <dt className="text-muted-foreground">计划完成日</dt>
                <dd className="ml-auto font-medium">{formatDate(item.due_date)}</dd>
              </div>
              {item.next_step && (
                <div className="flex items-start gap-2">
                  <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <dt className="text-muted-foreground">下一步</dt>
                  <dd className="ml-auto max-w-[70%] text-right">{item.next_step}</dd>
                </div>
              )}
            </dl>
            {(item.status === "RESOLVED" || item.status === "CANCELLED") && (
              <p className="rounded-control bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
                该事项已结束，仍可查看沟通记录。
              </p>
            )}
          </div>
        </Card>

        <Card className="flex min-h-[520px] flex-col p-0">
          <div className="card-head card-head-line">
            <CardHeading title="沟通" desc="回复后会通知管理者；有困难也可以主动说明" />
          </div>
          <div className="flex min-h-0 flex-1 flex-col px-4 py-4">
            <ConversationThread
              actionId={id}
              role="owner"
              pathPrefix="/api/my-actions"
              peerName={null}
              className="min-h-0 flex-1"
            />
          </div>
        </Card>
      </div>
    </div>
  );
}

export default function MyActionDetailPage() {
  return (
    <React.Suspense fallback={<Card><LoadingBlock rows={6} /></Card>}>
      <MyActionDetailContent />
    </React.Suspense>
  );
}
