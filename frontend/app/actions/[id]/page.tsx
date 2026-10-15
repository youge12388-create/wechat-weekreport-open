"use client";

import React from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import {
  ArrowLeft,
  CalendarClock,
  CircleAlert,
  ExternalLink,
  Link2,
  Pencil,
  UserRound,
} from "lucide-react";
import { AdminOnly } from "@/components/admin-only";
import { ConversationThread } from "@/components/conversation-thread";
import { ManagementActionEditor, managementActionStatusLabel } from "@/components/management-action-editor";
import { Avatar, Badge, Button, Card, CardHeader, CardHeading, ErrorState, LoadingBlock } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type {
  ActionMessageDelivery,
  ActionNotifyReadiness,
  ManagementAction,
  ManagementActionStatus,
} from "@/lib/types";

/**
 * 跟进事项详情（管理侧）。
 *
 * 为什么要有独立页面而不是继续用弹层：会话需要「历史滚动 + 未读定位 +
 * 从企微卡片深链进来」，弹层三样都做不了 —— 原来的编辑器刻意是「关掉就重置」，
 * 与会话要保留上下文正好相反。
 */

const STATUS_TONE: Record<ManagementActionStatus, "red" | "amber" | "green" | "blue" | "neutral"> = {
  OPEN: "red",
  IN_PROGRESS: "blue",
  WAITING: "amber",
  RESOLVED: "green",
  CANCELLED: "neutral",
};

function priorityLabel(priority: ManagementAction["priority"]): string {
  return priority === "high" ? "高" : priority === "medium" ? "中" : "低";
}

/**
 * 一行投递结果。状态变更与「重新推送」共用 —— 两处措辞必须一致，
 * 否则同一件事在页面上的说法会随入口不同而变。
 */
function DeliveryLine({
  delivery,
  label,
  prefix = "",
}: {
  delivery: ActionMessageDelivery;
  label?: string;
  prefix?: string;
}) {
  const ok = delivery.status === "sent" && delivery.undelivered.length === 0;
  const text = ok
    ? `已通知 ${delivery.delivered.map((entry) => entry.name).join("、")}`
    : delivery.skipped_reason ??
      delivery.error ??
      delivery.undelivered.map((entry) => entry.reason).join("；") ??
      "未发送";
  return (
    <p
      className={`${prefix} rounded-control px-3 py-2 text-xs ${
        ok ? "bg-success-soft text-success" : "bg-warning-soft text-warning"
      }`}
    >
      {label ? `${label}：${text}` : text}
    </p>
  );
}

function ActionDetailContent() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const id = Number(params.id);

  const action = useAsyncData<ManagementAction>(() => apiGet(`/api/management-actions/${id}`), [id]);
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [statusSaving, setStatusSaving] = React.useState(false);
  const [statusError, setStatusError] = React.useState<string | null>(null);
  /** 改状态时是否同时通知负责人（默认开）。 */
  const [notifyStatus, setNotifyStatus] = React.useState(true);
  /** 改状态后的通知结果，显示在状态按钮下方（与「重新推送」的结果分开，避免误读）。 */
  const [statusNotice, setStatusNotice] = React.useState<ActionMessageDelivery | null>(null);
  const [delivery, setDelivery] = React.useState<ActionMessageDelivery | null>(null);
  const [notifying, setNotifying] = React.useState(false);
  // 会话组件自己管消息；这里只保存投递前提，用于在会话上方给出警示。
  const [readiness, setReadiness] = React.useState<ActionNotifyReadiness | null>(null);

  React.useEffect(() => {
    if (!action.data) return;
    let cancelled = false;
    apiGet<{ readiness?: ActionNotifyReadiness }>(`/api/management-actions/${id}/messages`)
      .then((data) => {
        if (!cancelled) setReadiness(data.readiness ?? null);
      })
      .catch(() => {
        // 就绪检查拿不到不阻塞页面：会话本身仍能正常读写。
      });
    return () => {
      cancelled = true;
    };
  }, [id, action.data]);

  async function changeStatus(next: ManagementActionStatus) {
    if (!action.data || statusSaving) return;
    setStatusSaving(true);
    setStatusError(null);
    setStatusNotice(null);
    try {
      // notifyStatus 决定是否同时通知负责人。默认开：他被告知要跟进这件事，
      // 状态被改却不告诉他，等于把他排除在外。
      const response = await apiPost<
        ManagementAction & { status_notice?: ActionMessageDelivery | null }
      >(`/api/management-actions/${id}/status`, { status: next, notify: notifyStatus });
      if (response.status_notice) setStatusNotice(response.status_notice);
      await action.reload();
    } catch (reason) {
      setStatusError(reason instanceof Error ? reason.message : "状态更新失败");
    } finally {
      setStatusSaving(false);
    }
  }

  async function renotify(force: boolean) {
    setNotifying(true);
    setDelivery(null);
    try {
      const response = await apiPost<{ delivery: ActionMessageDelivery }>(
        `/api/management-actions/${id}/notify`,
        { force }
      );
      setDelivery(response.delivery);
    } catch (reason) {
      setStatusError(reason instanceof Error ? reason.message : "通知发送失败");
    } finally {
      setNotifying(false);
    }
  }

  if (!Number.isInteger(id) || id <= 0) {
    return <Card><ErrorState message="事项编号无效" /></Card>;
  }
  if (action.loading) return <Card><LoadingBlock rows={6} /></Card>;
  if (action.error || !action.data) {
    return <Card><ErrorState message={action.error ?? "未找到该事项"} onRetry={action.reload} /></Card>;
  }
  const item = action.data;
  const backQuery = searchParams.toString();
  const closed = item.status === "RESOLVED" || item.status === "CANCELLED";
  // 编辑器只在「新建」时用 period 组装请求；这里是编辑已有事项，所以从快照里
  // 取它建立时的周期，避免传一个空区间（空值会让后端校验失败）。
  const snapshotPeriod = (item.source_snapshot?.period ?? null) as
    | { start?: string; end?: string }
    | null;
  const editorPeriod = {
    start: snapshotPeriod?.start ?? "",
    end: snapshotPeriod?.end ?? "",
  };

  return (
    <div>
      <Link
        href={`/blockers?tab=actions${backQuery ? `&${backQuery}` : ""}`}
        className="mb-3 inline-flex items-center gap-1.5 rounded-control px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> 返回处理中心
      </Link>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        {/* 左侧：会话是主角，占主要宽度与高度 */}
        <Card className="flex min-h-[520px] flex-col p-0">
          <div className="card-head card-head-line">
            <CardHeading
              title="沟通"
              desc="消息会记入事项档案；勾选通知会同时推送企业微信"
            />
          </div>
          <div className="flex min-h-0 flex-1 flex-col px-4 py-4">
            <ConversationThread
              actionId={id}
              role="admin"
              pathPrefix="/api/management-actions"
              peerName={item.owner_name}
              canNotify
              readiness={readiness}
              className="min-h-0 flex-1"
            />
          </div>
        </Card>

        {/* 右侧：事项档案与操作 */}
        <div className="space-y-4">
          <Card className="p-0">
            <div className="card-head card-head-line">
              <CardHeader
                title="事项"
                action={
                  <AdminOnly>
                    <Button variant="outline" size="sm" onClick={() => setEditorOpen(true)}>
                      <Pencil className="h-3.5 w-3.5" />
                      编辑
                    </Button>
                  </AdminOnly>
                }
              />
            </div>
            <div className="space-y-3 px-4 py-4">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="text-base font-semibold leading-snug">{item.title}</h1>
                  <Badge tone={STATUS_TONE[item.status]}>
                    {managementActionStatusLabel(item.status)}
                  </Badge>
                  {item.source_stale && <Badge tone="amber">来源已更新</Badge>}
                </div>
                {item.description && (
                  <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                    {item.description}
                  </p>
                )}
              </div>

              <dl className="space-y-2 text-xs">
                <div className="flex items-start gap-2">
                  <UserRound className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <dt className="text-muted-foreground">负责人</dt>
                  <dd className="ml-auto flex items-center gap-1.5 text-right font-medium">
                    {item.owner_name ? (
                      <>
                        <Avatar name={item.owner_name} size={18} />
                        {item.owner_name}
                      </>
                    ) : (
                      <span className="text-warning">未指定</span>
                    )}
                  </dd>
                </div>
                <div className="flex items-center gap-2">
                  <CalendarClock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <dt className="text-muted-foreground">计划完成日</dt>
                  <dd className="ml-auto font-medium">{formatDate(item.due_date)}</dd>
                </div>
                <div className="flex items-center gap-2">
                  <CircleAlert className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <dt className="text-muted-foreground">优先级</dt>
                  <dd className="ml-auto font-medium">{priorityLabel(item.priority)}</dd>
                </div>
                {item.next_step && (
                  <div className="flex items-start gap-2">
                    <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <dt className="text-muted-foreground">下一步</dt>
                    <dd className="ml-auto max-w-[70%] text-right">{item.next_step}</dd>
                  </div>
                )}
              </dl>

              {item.source_href && (
                <Link
                  href={item.source_href}
                  className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                >
                  查看来源依据 <ExternalLink className="h-3 w-3" />
                </Link>
              )}
            </div>
          </Card>

          {/* 状态操作：员工不能改状态，所以这块只在管理侧出现 */}
          <AdminOnly>
            <Card className="p-0">
              <div className="card-head card-head-line">
                <CardHeading title="推进状态" desc="每次变更都会留下记录" />
              </div>
              <div className="px-4 py-4">
                <div className="flex flex-wrap gap-2">
                  {(["OPEN", "IN_PROGRESS", "WAITING", "RESOLVED", "CANCELLED"] as ManagementActionStatus[]).map(
                    (status) => (
                      <Button
                        key={status}
                        size="sm"
                        variant={item.status === status ? "primary" : "outline"}
                        disabled={statusSaving || item.status === status}
                        onClick={() => void changeStatus(status)}
                      >
                        {managementActionStatusLabel(status)}
                      </Button>
                    )
                  )}
                </div>
                {statusError && (
                  <p className="mt-2 rounded-control bg-destructive-soft px-3 py-2 text-xs text-destructive">
                    {statusError}
                  </p>
                )}
                {/* 通知开关放在按钮下方：改状态会推送企微给负责人，
                    但有时只是修正填错的备注，不想再打扰他 —— 给他一个明确的关法。 */}
                {item.owner_employee_id && (
                  <label className="mt-3 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={notifyStatus}
                      onChange={(event) => setNotifyStatus(event.target.checked)}
                      className="h-3.5 w-3.5 rounded border-input"
                    />
                    改状态时通知 {item.owner_name ?? "负责人"}
                  </label>
                )}
                {statusNotice && (
                  <DeliveryLine delivery={statusNotice} label="状态通知" prefix="mt-2" />
                )}
                {closed && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    该事项已结束，会话记录仍会保留。
                  </p>
                )}
                <div className="mt-3 border-t border-border/70 pt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    loading={notifying}
                    onClick={() => void renotify(true)}
                  >
                    重新推送最近一条消息
                  </Button>
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    用于对方没收到通知时补发（会跳过 10 分钟冷却限制）。
                  </p>
                  {delivery && <DeliveryLine delivery={delivery} prefix="mt-2" />}
                </div>
              </div>
            </Card>
          </AdminOnly>

          {item.note && (
            <Card className="p-0">
              <div className="card-head card-head-line">
                <CardHeading title="内部备注" desc="不对负责人展示" />
              </div>
              <p className="whitespace-pre-wrap px-4 py-4 text-sm leading-relaxed text-muted-foreground">
                {item.note}
              </p>
            </Card>
          )}

          {item.events.length > 0 && (
            <Card className="p-0">
              <div className="card-head card-head-line">
                <CardHeading title="变更记录" />
              </div>
              <ol className="space-y-2 px-4 py-4">
                {item.events
                  .slice()
                  .reverse()
                  .map((event) => (
                    <li key={event.id} className="text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">
                        {event.to_status
                          ? managementActionStatusLabel(event.to_status)
                          : event.event_type === "created"
                            ? "创建跟进"
                            : event.event_type === "updated"
                              ? "更新内容"
                              : "备注"}
                      </span>
                      {event.note ? ` · ${event.note}` : ""}
                      <span className="ml-2">{formatDateTime(event.created_at)}</span>
                    </li>
                  ))}
              </ol>
            </Card>
          )}
        </div>
      </div>

      <ManagementActionEditor
        open={editorOpen}
        action={item}
        period={editorPeriod}
        onClose={() => setEditorOpen(false)}
        onSaved={() => void action.reload()}
      />
    </div>
  );
}

export default function ActionDetailPage() {
  return (
    <React.Suspense fallback={<Card><LoadingBlock rows={6} /></Card>}>
      <ActionDetailContent />
    </React.Suspense>
  );
}
