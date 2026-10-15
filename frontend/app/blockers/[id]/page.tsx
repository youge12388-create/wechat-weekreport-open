"use client";

import React from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { ArrowLeft, CheckCircle2, CircleAlert, ExternalLink, Inbox, MessageSquare, Pencil, Plus, RotateCcw } from "lucide-react";
import { ManagementActionEditor, managementActionStatusLabel } from "@/components/management-action-editor";
import { AdminOnly } from "@/components/admin-only";
import { Avatar, Badge, Button, Card, EmptyState, ErrorState, IconChip, LoadingBlock, StatusDot } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { defaultPeriodKey, formatDate, formatDateTime, periodKeyForRange, periodLabel, periodOptions } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type { BlockerItem, ManagementAction, ManagementActionListResponse } from "@/lib/types";

function riskTone(level: BlockerItem["risk_level"]): "red" | "amber" | "neutral" {
  return level === "高" ? "red" : level === "中" ? "amber" : "neutral";
}

function actionTone(status: ManagementAction["status"]): "red" | "amber" | "green" | "blue" | "neutral" {
  if (status === "RESOLVED") return "green";
  if (status === "CANCELLED") return "neutral";
  if (status === "WAITING") return "amber";
  if (status === "IN_PROGRESS") return "blue";
  return "red";
}

function priorityLabel(priority: ManagementAction["priority"]): string {
  return priority === "high" ? "高" : priority === "medium" ? "中" : "低";
}

/** 生命周期状态在详情页的展示口径，与处理中心列表一致。 */
function lifecycleMeta(status: BlockerItem["status"]): { label: string; tone: "green" | "amber" | "red" | "blue" | "neutral" } {
  switch (status) {
    case "CANDIDATE":
      return { label: "候选提及", tone: "neutral" };
    case "DORMANT":
      return { label: "休眠", tone: "amber" };
    case "ARCHIVED":
      return { label: "已归档", tone: "neutral" };
    case "RESOLVED":
      return { label: "原始卡点已解决", tone: "green" };
    default:
      return { label: "跟进中", tone: "amber" };
  }
}

function BlockerDetailContent() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const id = Number(params.id);
  const options = periodOptions().filter((option) => option.key !== "this_month");
  const urlStart = searchParams.get("start");
  const urlEnd = searchParams.get("end");
  const periodKey = periodKeyForRange(urlStart, urlEnd, defaultPeriodKey());
  const selectedPeriod = options.find((option) => option.key === periodKey) ?? options[0] ?? periodOptions()[0];
  const period = urlStart && urlEnd ? { start: urlStart, end: urlEnd } : selectedPeriod;
  const periodQuery = `start=${encodeURIComponent(period.start)}&end=${encodeURIComponent(period.end)}`;
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editingAction, setEditingAction] = React.useState<ManagementAction | null>(null);

  const blocker = useAsyncData<BlockerItem>(
    // 详情接口保留周期参数，旧接口忽略未知参数时仍由页面按周期裁剪证据。
    () => apiGet(`/api/blockers/${id}`, { start: period.start, end: period.end }),
    [id, period.start, period.end]
  );
  const actions = useAsyncData<ManagementActionListResponse>(
    () => apiGet("/api/management-actions", { start: period.start, end: period.end }),
    [period.start, period.end]
  );
  const followUps = (actions.data?.items ?? []).filter(
    (action) => action.source_type === "blocker" && action.source_id === id
  );
  // 手动生命周期操作：解决 / 重开由后端推导链路保护，前端只负责触发与刷新。
  const [statusMutating, setStatusMutating] = React.useState(false);
  const [statusError, setStatusError] = React.useState<string | null>(null);

  async function mutateStatus(action: "resolve" | "reopen") {
    setStatusMutating(true);
    setStatusError(null);
    try {
      await apiPost(`/api/blockers/${id}/status`, { action });
      await blocker.reload();
    } catch (error) {
      setStatusError(error instanceof Error ? error.message : "操作失败，请重试");
    } finally {
      setStatusMutating(false);
    }
  }

  function openCreate() {
    // 当前周期已有跟进时直接进入编辑，避免再次走创建接口。
    setEditingAction(followUps[0] ?? null);
    setEditorOpen(true);
  }

  function openEdit(action: ManagementAction) {
    setEditingAction(action);
    setEditorOpen(true);
  }

  if (!Number.isInteger(id) || id <= 0) {
    return <Card><EmptyState title="卡点编号无效" desc="请从卡点中心重新进入详情。" /></Card>;
  }
  if (blocker.loading) return <Card><LoadingBlock rows={6} /></Card>;
  if (blocker.error || !blocker.data) return <Card><ErrorState message={blocker.error ?? "未找到卡点"} onRetry={blocker.reload} /></Card>;

  const item = blocker.data;
  const people = [...new Set(item.sources.map((source) => source.employee_name).filter(Boolean))];
  const periodSources = item.sources.filter((source) => {
    const sourceStart = source.period_start?.slice(0, 10);
    const sourceEnd = source.period_end?.slice(0, 10);
    return !sourceStart || !sourceEnd || (sourceStart <= period.end && sourceEnd >= period.start);
  });
  return (
    <div>
      <Link href={`/blockers?${periodQuery}`} className="mb-3 inline-flex items-center gap-1.5 rounded-control px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> 返回处理中心
      </Link>
      {/* 第一层：这个卡点有多严重、拖了多久；动作和证据放在后面两层 */}
      <Card className="overflow-hidden p-0">
        <div className="flex flex-wrap items-start gap-3 px-5 py-4">
          <IconChip icon={CircleAlert} tone={riskTone(item.risk_level)} />
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
              <h1 className="text-lg font-semibold tracking-tight">{item.title}</h1>
              <Badge tone={riskTone(item.risk_level)}>{item.risk_level}风险</Badge>
              <StatusDot tone={lifecycleMeta(item.status).tone}>{lifecycleMeta(item.status).label}</StatusDot>
              <AdminOnly>
                {item.status !== "RESOLVED" ? (
                  <Button variant="outline" size="sm" loading={statusMutating} onClick={() => void mutateStatus("resolve")}>
                    <CheckCircle2 className="h-3.5 w-3.5" />标记已解决
                  </Button>
                ) : (
                  <Button variant="outline" size="sm" loading={statusMutating} onClick={() => void mutateStatus("reopen")}>
                    <RotateCcw className="h-3.5 w-3.5" />重新打开
                  </Button>
                )}
              </AdminOnly>
            </div>
            {statusError && <p className="mt-1 text-xs text-destructive">{statusError}</p>}
            {item.description && (
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{item.description}</p>
            )}
            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>当前事项状态：<span className="text-foreground">{item.task_status}</span></span>
              {item.related_project && <span>关联项目：<span className="text-foreground">{item.related_project}</span></span>}
              {people.length > 0 && <span>涉及：{people.join("、")}</span>}
            </p>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 border-t border-border px-5 py-4 sm:grid-cols-3">
          <div>
            <p className="text-xs text-muted-foreground">首次出现</p>
            <p className="mt-1 text-sm num">{formatDate(item.first_seen_date)}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">最近出现</p>
            <p className="mt-1 text-sm num">{formatDate(item.last_seen_date)}</p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">持续 / 出现</p>
            <p className="mt-1 text-sm num">{item.duration_weeks} 周 · {item.occurrence_count} 次</p>
          </div>
        </div>
      </Card>

      <Card className="mt-4 overflow-hidden p-0">
        <div className="card-head card-head-line">
          <div>
            <h2 className="text-sm font-semibold">管理跟进</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">跟进周期：{periodLabel(period.start, period.end)}，状态更新会保留记录，便于后续回看</p>
          </div>
          {followUps.length > 0 || item.status !== "RESOLVED" ? (
            <Button size="sm" onClick={openCreate}>
              {followUps.length > 0 ? <Pencil className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
              {followUps.length > 0 ? "查看/更新跟进" : "纳入跟进"}
            </Button>
          ) : (
            <Badge tone="green">原始卡点已解决，无需新建跟进</Badge>
          )}
        </div>
        <div className="px-5 py-4">
          {actions.loading ? <LoadingBlock rows={3} /> : actions.error ? <ErrorState message={actions.error} onRetry={actions.reload} /> : followUps.length === 0 ? (
            <div className="flex items-start gap-3 rounded-control bg-muted/40 px-4 py-4">
              <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface">
                <Inbox className="h-4 w-4 text-muted-foreground" />
              </span>
              <div>
                <p className="text-sm font-medium">尚未纳入管理跟进</p>
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">将卡点纳入跟进后，可指定负责人、截止日与下一步，并持续更新处理状态。</p>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {followUps.map((action) => (
                <div key={action.id} className="rounded-control bg-muted/40 p-3.5">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                        <p className="text-sm font-medium">{action.title}</p>
                        <Badge tone={actionTone(action.status)}>{managementActionStatusLabel(action.status)}</Badge>
                        {action.source_stale && <StatusDot tone="amber">来源已更新</StatusDot>}
                      </div>
                      <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                        <span>负责人：{action.owner_name ?? "未指定"}</span>
                        <span>优先级：{priorityLabel(action.priority)}</span>
                        <span>完成日：{formatDate(action.due_date)}</span>
                        <span>更新于：{formatDateTime(action.updated_at)}</span>
                      </p>
                      {action.next_step && <p className="mt-2 text-sm text-muted-foreground">下一步：{action.next_step}</p>}
                      {action.note && <p className="mt-1 text-xs text-muted-foreground">备注：{action.note}</p>}
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-2">
                      {/* 沟通在独立页面上（会话需要滚动与未读定位，弹层做不到），
                          这里保留「更新」用于改负责人/状态等结构化字段。 */}
                      <Link
                        href={`/actions/${action.id}`}
                        className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 text-xs font-medium transition-colors hover:border-primary/35 hover:bg-muted"
                      >
                        <MessageSquare className="h-3.5 w-3.5" />
                        查看沟通
                        {(action.unread_count ?? 0) > 0 && (
                          <span className="rounded-pill bg-destructive px-1.5 text-xs text-destructive-foreground">
                            {action.unread_count}
                          </span>
                        )}
                      </Link>
                      <Button variant="outline" size="sm" onClick={() => openEdit(action)}><Pencil className="h-3.5 w-3.5" /> 更新</Button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>

      <Card className="mt-4 overflow-hidden p-0">
        <div className="card-head card-head-line">
          <div>
            <h2 className="text-sm font-semibold">原始周报依据</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">管理动作都基于下列可回看的周报证据，不替代人工判断</p>
          </div>
          <span className="text-xs text-muted-foreground num">本周期 {periodSources.length} / 共 {item.sources.length} 篇</span>
        </div>
        <div className="px-5 py-4">
          {periodSources.length === 0 ? <EmptyState title="本周期暂无周报依据" desc={item.sources.length ? "该卡点在其他周期有依据，可切换回对应周期查看。" : undefined} /> : (
            <div className="space-y-2">
              {periodSources.map((source) => (
                <Link key={source.report_id} href={`/reports/${source.report_id}?${periodQuery}`} className="block rounded-control bg-muted/60 px-3 py-2.5 transition-colors hover:bg-primary-soft">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-2 text-xs font-medium">
                      <Avatar name={source.employee_name ?? "未知员工"} size={20} />
                      <span className="truncate">{source.employee_name ?? "未知员工"} · {source.title ?? "周报"}</span>
                    </span>
                    <span className="inline-flex shrink-0 items-center gap-1 text-xs text-primary">查看原文 <ExternalLink className="h-3 w-3" /></span>
                  </div>
                  <p className="mt-1.5 line-clamp-3 text-xs text-muted-foreground">{source.source_text}</p>
                </Link>
              ))}
            </div>
          )}
        </div>
      </Card>

      <ManagementActionEditor
        open={editorOpen}
        action={editingAction}
        source={{ sourceType: "blocker", sourceId: item.id, title: item.title }}
        period={period}
        onClose={() => setEditorOpen(false)}
        onSaved={() => actions.reload()}
      />
    </div>
  );
}

export default function BlockerDetailPage() {
  return (
    <React.Suspense fallback={<Card><LoadingBlock rows={6} /></Card>}>
      <BlockerDetailContent />
    </React.Suspense>
  );
}
