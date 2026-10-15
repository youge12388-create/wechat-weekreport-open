"use client";

import React from "react";
import Link from "next/link";
import { ArrowUpRight, CheckCircle2, ChevronDown, CircleAlert, ExternalLink, Loader2, MessageSquare, Pencil, RefreshCw, Sparkles, UsersRound, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { ManagementActionEditor } from "@/components/management-action-editor";
import { AssistantAnswer } from "@/components/assistant-answer";
import { Avatar, Badge, Button, Card, EmptyState, ErrorState, KpiCard, LoadingBlock, Modal, PageHeader, PageIntro, StatusDot } from "@/components/ui";
import { apiCacheKey, apiGet, apiPost } from "@/lib/api";
import { AdminOnly } from "@/components/admin-only";
import { useSession } from "@/components/session-provider";
import { formatDate, formatDateTime, periodLabel, periodOptions } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import { TASK_POLL_MAX_MS, usePolling } from "@/lib/use-polling";
import type { BlockerItem, BlockerPeriodSummary, EmployeeItem, ManagementAction, ManagementActionListResponse } from "@/lib/types";

type CenterFilter = "open" | "all" | "long" | "new" | "high" | "resolved" | "candidate";
type CenterTab = "all" | "unsubmitted" | "blockers" | "actions";

const FILTERS: Array<{ key: CenterFilter; label: string }> = [
  { key: "open", label: "待处理" },
  { key: "high", label: "高风险" },
  { key: "long", label: "长期未解决" },
  { key: "new", label: "本期新增" },
  { key: "candidate", label: "候选提及" },
  { key: "resolved", label: "已解决" },
  { key: "all", label: "全部状态" },
];

/** 生命周期状态在前端的展示口径；unknown 状态回退到旧的证据口径。 */
function statusMetaFor(blocker: BlockerItem, pendingConfirmation: boolean): { label: string; tone: "green" | "amber" | "red" | "blue" | "neutral" } {
  if (pendingConfirmation) return { label: "待确认", tone: "amber" };
  switch (blocker.status) {
    case "CANDIDATE":
      return { label: "候选提及", tone: "neutral" };
    case "DORMANT":
      return { label: "休眠", tone: "amber" };
    case "ARCHIVED":
      return { label: "已归档", tone: "neutral" };
    case "RESOLVED":
      return { label: "已解决", tone: "green" };
    default:
      return blocker.source_kind === "ai"
        ? { label: "AI识别", tone: "blue" }
        : { label: "有原文依据", tone: "green" };
  }
}

function filterFromUrl(searchParams: URLSearchParams): CenterFilter {
  const explicit = searchParams.get("filter");
  if (FILTERS.some((filter) => filter.key === explicit)) return explicit as CenterFilter;
  if (searchParams.get("status") === "RESOLVED") return "resolved";
  if (searchParams.get("long_only") === "true" || searchParams.get("long_only") === "1") return "long";
  if (searchParams.get("risk_level") === "高") return "high";
  return "open";
}

function tabFromUrl(searchParams: URLSearchParams): CenterTab {
  const value = searchParams.get("tab");
  return value === "all" || value === "unsubmitted" || value === "actions" ? value : "blockers";
}

function isIsoDate(value: string | null): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

/** 周期由 URL 驱动，保留首页自定义周期和浏览器前进/后退。 */

function BlockersPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlStart = searchParams.get("start");
  const urlEnd = searchParams.get("end");
  // 旧版本的已移除时间范围入口会带 range 参数。不能把旧链接误认成
  // 自定义周期，否则用户仍会看到已经下线的范围结果。
  const hasRemovedRange = searchParams.has("range");
  // 首页「本月」视图的链接会带上整月区间，而这里原本只认本周/上周，
  // 匹配不到就静默回退到默认周期 —— 页面数字与链接指向的周期对不上。
  // 这里按 /reports 的既有做法，把 URL 区间原样接成「自定义」周期。
  const urlPeriod =
    !hasRemovedRange && isIsoDate(urlStart) && isIsoDate(urlEnd) && urlStart <= urlEnd
      ? { key: "custom", label: "自定义", start: urlStart, end: urlEnd }
      : null;
  const baseOptions = periodOptions().filter((option) => option.key !== "this_month");
  // 「自定义」只在 URL 真的带来区间时才出现，否则会渲染一个点不开的空档。
  const options = urlPeriod ? [...baseOptions, urlPeriod] : baseOptions;
  const initialPeriodKey =
    options.find((option) => option.start === urlStart && option.end === urlEnd)?.key ??
    "last_week";
  const periodKey = initialPeriodKey;
  const filterKey = filterFromUrl(searchParams);
  const centerTab = tabFromUrl(searchParams);
  // 经营看板部门卡的「N 项卡点未关闭」徽标会带 department_id 跳转过来。
  // 只在卡点分页生效，渲染为可移除的筛选芯片；后端按「卡点任务的证据里有
  // 该部门员工的周报行」过滤，与徽标数字同口径（2026-09-28）。
  const urlDepartmentId = searchParams.get("department_id");
  const departmentId =
    centerTab === "blockers" && urlDepartmentId && /^\d+$/.test(urlDepartmentId)
      ? Number(urlDepartmentId)
      : null;
  const departmentName = searchParams.get("department") ?? "";
  const period = options.find((option) => option.key === periodKey) ?? options[0];
  const periodQuery = `start=${period.start}&end=${period.end}`;

  const lastWeek = baseOptions.find((option) => option.key === "last_week")!;
  const thisWeek = baseOptions.find((option) => option.key === "this_week")!;
  const blockerParams = {
    start: period.start,
    end: period.end,
    weekly_only: true,
    scope: filterKey,
    ...(departmentId ? { department_id: departmentId } : {}),
  };
  const blockers = useAsyncData<{ items: BlockerItem[] }>(
    () => apiGet("/api/blockers", blockerParams),
    [filterKey, period.start, period.end, departmentId],
    { cacheKey: apiCacheKey("/api/blockers", blockerParams) }
  );
  // 候选层（单周一次性低/中风险提及）默认不进主列表；这里单独取一份，
  // 在列表底部折叠展示「另有 N 条一次性提及」，需要时可展开核对。
  const candidateParams = { start: period.start, end: period.end, weekly_only: true, scope: "candidate" };
  const candidateBlockers = useAsyncData<{ items: BlockerItem[] }>(
    () => apiGet("/api/blockers", candidateParams),
    [period.start, period.end],
    { cacheKey: apiCacheKey("/api/blockers", candidateParams) }
  );
  const counts = useAsyncData<Record<string, number>>(
    async () => {
      const common = { weekly_only: true, count_only: true, status: "OPEN" };
      const [last, current] = await Promise.all([
        apiGet<{ total: number }>("/api/blockers", { ...common, start: lastWeek.start, end: lastWeek.end }, { bypassCache: true }),
        apiGet<{ total: number }>("/api/blockers", { ...common, start: thisWeek.start, end: thisWeek.end }, { bypassCache: true }),
      ]);
      return { last_week: last.total, this_week: current.total };
    },
    [lastWeek.start, thisWeek.start]
  );
  const periodParams = { start: period.start, end: period.end };
  const coverage = useAsyncData<{ coverage: { reports_total: number; analyzed_reports: number; rate: number } }>(
    () => apiGet("/api/insights", periodParams),
    [periodKey, period.start, period.end],
    { cacheKey: apiCacheKey("/api/insights", periodParams) }
  );
  const unsubmittedParams = {
    start: period.start,
    end: period.end,
    submit_status: "unsubmitted",
    page_size: 100,
  };
  const unsubmitted = useAsyncData<{ items: EmployeeItem[]; total: number }>(
    () => apiGet("/api/employees", unsubmittedParams),
    [periodKey, period.start, period.end],
    { cacheKey: apiCacheKey("/api/employees", unsubmittedParams) }
  );
  const actionParams = periodParams;
  // 管理动作是写操作：只读成员不打开编辑器，也不提交。
  // 管理事项列表与卡点 AI 总结是公司级能力（后端仅超管）：
  // 非超管用空占位，不发请求，也不渲染对应入口。
  const { isAdmin } = useSession();
  const actions = useAsyncData<ManagementActionListResponse>(
    () =>
      isAdmin
        ? apiGet("/api/management-actions", actionParams)
        : Promise.resolve({ items: [], total: 0 } as unknown as ManagementActionListResponse),
    [period.start, period.end, isAdmin],
    { cacheKey: apiCacheKey("/api/management-actions", actionParams) }
  );
  // 卡点 AI 总结：与首页「周期概览」同一套缓存纪律 —— 读缓存不重复烧 token，
  // 点击弹层只负责「看结论」；生成由弹层内按钮显式触发，Worker 后台执行。
  const blockerSummary = useAsyncData<BlockerPeriodSummary>(
    () =>
      isAdmin
        ? apiGet("/api/blockers/period-summary", periodParams)
        : Promise.resolve({ status: "not_generated" } as unknown as BlockerPeriodSummary),
    [periodKey, period.start, period.end, isAdmin],
    { cacheKey: apiCacheKey("/api/blockers/period-summary", periodParams) }
  );
  const [blockerSummaryOpen, setBlockerSummaryOpen] = React.useState(false);
  const [blockerSummarySubmitting, setBlockerSummarySubmitting] = React.useState(false);
  const [editingPeriod, setEditingPeriod] = React.useState({ start: period.start, end: period.end });
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [editingAction, setEditingAction] = React.useState<ManagementAction | null>(null);
  const [editingSource, setEditingSource] = React.useState<{ sourceType: "blocker" | "insight"; sourceId?: number; sourceKey?: string; title?: string } | undefined>();
  const [analysisStarting, setAnalysisStarting] = React.useState(false);
  const [analysisMessage, setAnalysisMessage] = React.useState<string | null>(null);
  const [analysisRequested, setAnalysisRequested] = React.useState(false);

  const setCenterFilter = (next: CenterFilter) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("filter", next);
    router.replace(`/blockers?${params.toString()}`, { scroll: false });
  };

  const setCenterSection = (next: CenterTab) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("tab", next);
    router.replace(`/blockers?${params.toString()}`, { scroll: false });
  };

  const setCenterPeriod = (nextKey: string) => {
    const nextPeriod = options.find((option) => option.key === nextKey) ?? lastWeek;
    const params = new URLSearchParams(`start=${nextPeriod.start}&end=${nextPeriod.end}`);
    params.set("tab", centerTab);
    params.set("filter", "open");
    router.replace(`/blockers?${params.toString()}`, { scroll: false });
  };

  /** 移除部门筛选芯片（保留其余 URL 状态）。 */
  const clearDepartmentFilter = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("department_id");
    params.delete("department");
    router.replace(`/blockers?${params.toString()}`, { scroll: false });
  };

  const openBlockerAction = (blocker: BlockerItem) => {
    // 只读成员不该打开编辑器：填写完点保存才会拿到 403，等于白填一遍。
    if (!isAdmin) return;
    // 列表接口已经带回当前周期的管理事项。优先打开已有事项，避免
    // 再次 POST 创建后用表单默认值覆盖负责人、下一步和备注。
    const existing = actionItems.find(
      (action) => action.source_type === "blocker" && action.source_id === blocker.id
    ) ?? null;
    setEditingPeriod(period);
    setEditingAction(existing);
    setEditingSource(existing ? undefined : { sourceType: "blocker", sourceId: blocker.id, title: blocker.title });
    setEditorOpen(true);
  };

  const startPeriodAnalysis = async () => {
    setAnalysisStarting(true);
    setAnalysisMessage(null);
    try {
      await apiPost("/api/ai/scan", { start: period.start, end: period.end }, { timeoutMs: 30000 });
      setAnalysisRequested(true);
      await coverage.reload();
      await blockers.reload();
      await counts.reload();
      setAnalysisMessage("已加入后台分析队列；完成后本页会自动刷新结果，资源需求无需等待 AI。");
    } catch (error) {
      setAnalysisMessage(error instanceof Error ? error.message : "提交分析失败");
    } finally {
      setAnalysisStarting(false);
    }
  };

  const allBlockers = blockers.data?.items ?? [];
  const visibleBlockers = allBlockers;
  // 紧凑行默认收起；点行首箭头才展开描述与原文来源。
  const [expandedIds, setExpandedIds] = React.useState<ReadonlySet<number>>(new Set());
  const [candidateOpen, setCandidateOpen] = React.useState(false);
  const toggleBlocker = (id: number) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };
  const candidateItems = candidateBlockers.data?.items ?? [];
  const coveragePending = Boolean(
    coverage.data && coverage.data.coverage.reports_total > coverage.data.coverage.analyzed_reports
  );

  // 只有明确点击分析后才轮询，避免处理中心打开时持续请求。
  // 超时兜底：任务卡死（Worker 挂掉等）时停止轮询并提示，不再无限请求。
  const pollGiveUp = React.useCallback(() => {
    setAnalysisMessage(
      "后台任务已超过 5 分钟未完成，可能后台服务异常；请稍后刷新页面确认，或联系管理员检查 Worker。"
    );
  }, []);
  usePolling(
    () => {
      coverage.reload();
      blockers.reload();
      counts.reload();
    },
    4000,
    {
      enabled: analysisRequested && coveragePending,
      maxDurationMs: TASK_POLL_MAX_MS,
      onGiveUp: pollGiveUp,
    }
  );

  React.useEffect(() => {
    if (!analysisRequested || !coverage.data) return;
    if (coverage.data.coverage.reports_total > 0 && coverage.data.coverage.analyzed_reports >= coverage.data.coverage.reports_total) {
      setAnalysisRequested(false);
      setAnalysisMessage("本期分析已完成，风险和异常已更新。");
    }
  }, [analysisRequested, coverage.data]);

  const blockerSummaryStatus = blockerSummary.data?.status;
  const blockerSummaryGenerating =
    blockerSummaryStatus === "pending" || blockerSummaryStatus === "running";
  const blockerSummaryReady = blockerSummaryStatus === "success";

  // 总结是后台任务，只在 pending/running 时轮询只读 GET，不重复提交模型任务。
  usePolling(() => blockerSummary.reload(), 4000, {
    enabled: blockerSummaryGenerating,
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: pollGiveUp,
  });

  const openBlockerSummary = () => {
    setBlockerSummaryOpen(true);
    blockerSummary.reload();
  };

  // 只入队；真正的模型调用由 Worker 执行。force=true 表示按最新卡点重算。
  const runBlockerSummary = async (force: boolean) => {
    setBlockerSummarySubmitting(true);
    try {
      await apiPost(
        "/api/blockers/period-summary",
        {
          start: period.start,
          end: period.end,
          force,
        },
        { timeoutMs: 30000 }
      );
      blockerSummary.reload();
    } finally {
      setBlockerSummarySubmitting(false);
    }
  };

  const unsubmittedItems = unsubmitted.data?.items ?? [];
  const actionItems = actions.data?.items ?? [];
  const openActionCount = actionItems.filter((action) => !["RESOLVED", "CANCELLED"].includes(action.status)).length;
  const unsubmittedCount = unsubmitted.data?.total ?? unsubmittedItems.length;

  return (
    <div>
      <PageHeader
        title="处理中心"
        desc={`${period.label}反馈的问题与管理跟进 · ${periodLabel(period.start, period.end)}`}
        action={centerTab !== "blockers" && <div className="flex rounded-pill border border-input bg-surface p-0.5">{options.map((option) => <button key={option.key} type="button" onClick={() => setCenterPeriod(option.key)} className={`rounded-pill px-3.5 py-1.5 text-xs transition-colors ${periodKey === option.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>{option.label}</button>)}</div>}
      />
      <PageIntro>
        按周查看反馈，持续跟进未解决的问题；每条卡点都保留周报原文依据。
      </PageIntro>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex flex-wrap rounded-control border border-input bg-surface p-1" role="tablist" aria-label="处理中心分区">
          {([
            ["all", "全部"],
            ["unsubmitted", "未提交"],
            ["blockers", "资源需求 / 卡点"],
            ["actions", "管理跟进"],
          ] as const).map(([key, label]) => (
            <button key={key} type="button" role="tab" aria-selected={centerTab === key} onClick={() => setCenterSection(key)} className={`rounded-pill px-3.5 py-1.5 text-xs font-medium transition-colors ${centerTab === key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>
              {label}
              {key === "unsubmitted" && !unsubmitted.loading && !unsubmitted.error && <span className="ml-1.5 tabular-nums">{unsubmittedCount}</span>}
              {key === "blockers" && !blockers.loading && !blockers.error && <span className="ml-1.5 tabular-nums">{visibleBlockers.length}</span>}
              {key === "actions" && !actions.loading && !actions.error && <span className="ml-1.5 tabular-nums">{openActionCount}</span>}
            </button>
          ))}
        </div>
        <Button variant="outline" size="sm" onClick={openBlockerSummary}>查看{period.label}卡点总结</Button>
      </div>

      {/* 三个汇总指标用和首页同一套 KPI 卡：图标 chip 给身份，大数字给量级 */}
      {centerTab !== "blockers" && <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <KpiCard
          label="未提交周报"
          icon={UsersRound}
          tone={unsubmittedCount > 0 ? "amber" : "green"}
          valueTone={unsubmittedCount > 0 ? "amber" : "green"}
          value={unsubmitted.error ? "—" : unsubmittedCount}
          loading={unsubmitted.loading}
          unit={unsubmitted.loading || unsubmitted.error ? undefined : "人"}
          hint="本期还没交周报的在职员工"
        />
        <KpiCard
          label={`${period.label}待处理卡点`}
          icon={CircleAlert}
          tone={visibleBlockers.length > 0 ? "red" : "green"}
          valueTone={visibleBlockers.length > 0 ? "red" : "green"}
          value={counts.error ? "—" : counts.data?.[periodKey] ?? "—"}
          loading={counts.loading}
          unit={counts.loading || counts.error || counts.data?.[periodKey] === undefined ? undefined : "条"}
          hint="点击查看本期卡点 AI 总结"
          onClick={isAdmin ? openBlockerSummary : undefined}
        />
        <KpiCard
          label="进行中跟进"
          icon={MessageSquare}
          value={actions.error ? "—" : openActionCount}
          loading={actions.loading}
          unit={actions.loading || actions.error ? undefined : "项"}
          hint="已纳入跟进、尚未结束的动作"
        />
      </div>}

      {(centerTab === "all" || centerTab === "unsubmitted") && <Card className="mb-4 overflow-hidden p-0">
        <div className="card-head card-head-line"><div><h2 className="text-sm font-semibold">未提交周报</h2><p className="mt-0.5 text-xs text-muted-foreground">只统计在职员工；点击姓名查看该周期上下文</p></div><Link href={`/dashboard?${periodQuery}`} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">去管理简报提醒 <ArrowUpRight className="h-3.5 w-3.5" /></Link></div>
        {unsubmitted.loading ? <LoadingBlock rows={2} /> : unsubmitted.error ? <div className="px-4 py-3"><ErrorState message={unsubmitted.error} onRetry={unsubmitted.reload} /></div> : unsubmittedCount === 0 ? <div className="flex items-center gap-2 px-4 py-5 text-sm text-muted-foreground"><CheckCircle2 className="h-4 w-4 text-success" />本期所有在职员工都已提交。</div> : <div className="px-4 py-4"><div className="flex flex-wrap gap-2">{unsubmittedItems.slice(0, 24).map((employee) => <Link key={employee.id} href={`/reports?employee_id=${employee.id}`} title={`查看 ${employee.name} 的历史周报`} className="inline-flex items-center gap-1.5 rounded-full border border-input bg-surface py-1 pl-1 pr-2.5 text-xs font-medium transition-colors hover:border-primary/40 hover:bg-primary-soft/40"><Avatar name={employee.name} size={20} />{employee.name}</Link>)}{unsubmittedCount > 24 && <span className="self-center text-xs text-muted-foreground">还有 {unsubmittedCount - 24} 人</span>}</div><div className="mt-4 flex flex-wrap items-center gap-3"><a href={`/api/exports/unsubmitted?${periodQuery}`} download className="text-xs font-medium text-primary hover:underline">导出未提交名单</a><Link href={`/dashboard?${periodQuery}#needs-heading`} className="text-xs font-medium text-primary hover:underline">回管理简报处理</Link></div></div>}
      </Card>}

      {(centerTab === "all" || centerTab === "blockers") && <Card className="mb-4 overflow-hidden p-0">
        <div className="px-4 pt-4 pb-3">
          <div className="grid grid-cols-2 gap-2" role="group" aria-label="卡点时间范围">
            {[["last_week", "上周卡点"], ["this_week", "本周卡点"]].map(([key, label]) => {
              const active = key === periodKey;
              return <button key={key} type="button" aria-pressed={active} aria-busy={counts.loading || undefined} onClick={() => setCenterPeriod(key)} className={`rounded-control border px-2 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active ? "border-primary bg-primary text-primary-foreground" : "border-border bg-muted/40 hover:bg-muted"}`}>
                <span className="block text-xs sm:text-sm font-medium">{label}</span>
                {counts.loading ? (
                  <span className="mt-1 flex items-center gap-1.5 text-xs font-medium opacity-80">
                    <Loader2 aria-hidden className="h-3.5 w-3.5 animate-spin" />加载中
                  </span>
                ) : (
                  <span className="mt-1 block text-lg font-semibold tabular-nums">{counts.error ? "—" : counts.data?.[key] ?? "—"}<span className="ml-1 text-xs font-normal">待处理</span></span>
                )}
              </button>;
            })}
          </div>
          <p className="mt-3 text-xs text-muted-foreground">{`${periodLabel(period.start, period.end)} · 按本周期周报中的反馈归类，不限首次出现时间。${periodKey === "this_week" ? "随本周周报提交更新，当前数据可能尚不完整。" : ""}`}</p>
          {counts.error && <ErrorState message="待处理数量加载失败" onRetry={counts.reload} />}
        </div>
        <div className="card-head card-head-line"><div><h2 className="text-sm font-semibold">资源需求 / 卡点</h2><p className="mt-0.5 text-xs text-muted-foreground">默认紧凑展示；点行首箭头展开描述与原文来源，仅高风险与重复提及进入待处理</p></div><div className="flex flex-wrap items-center gap-2"><div className="flex flex-wrap rounded-control border border-input bg-surface p-0.5">{FILTERS.map((filter) => <button key={filter.key} type="button" onClick={() => setCenterFilter(filter.key)} className={`rounded-pill px-3 py-1.5 text-xs transition-colors ${filterKey === filter.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>{filter.label}</button>)}</div>{coveragePending && <AdminOnly><Button variant="outline" size="sm" onClick={() => void startPeriodAnalysis()} loading={analysisStarting}><Sparkles className="h-3.5 w-3.5" />开始分析本期</Button></AdminOnly>}</div></div>
        {analysisMessage && <p className="border-b border-border bg-primary-soft/30 px-4 py-2 text-xs text-foreground">{analysisMessage}</p>}
        {departmentId !== null && <div className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs"><span className="text-muted-foreground">筛选</span><button type="button" onClick={clearDepartmentFilter} title="移除部门筛选" className="inline-flex items-center gap-1 rounded-pill bg-primary-soft px-2.5 py-1 font-medium text-primary transition-colors hover:bg-primary hover:text-primary-foreground">部门：{departmentName || `#${departmentId}`}<X className="h-3 w-3" /></button></div>}
        {/* 空状态标题必须先回答「有没有卡点」；本期分析未完成只是补充说明，
            不能让标题变成「等待 AI 分析完成」——那会让用户分不清是没数据还是没加载完
            （2026-10-10 线上验收反馈）。 */}
        {blockers.loading ? <LoadingBlock rows={5} /> : blockers.error ? <ErrorState message={blockers.error} onRetry={blockers.reload} /> : visibleBlockers.length === 0 ? <div className="px-4"><EmptyState title={filterKey === "new" ? "本期暂无新增卡点" : filterKey === "resolved" ? "本期暂无已解决卡点" : "本期暂无待处理卡点"} desc={coveragePending ? "本期仍有周报未完成 AI 分析；资源需求不依赖 AI、已直接显示，分析完成后可能补充新的风险来源。" : "当前筛选条件下没有带原文依据的资源需求或卡点。"} /></div> : <div className="space-y-2 px-4 pb-4">{visibleBlockers.map((blocker) => {
          const pendingConfirmation = blocker.pending_confirmation === true || blocker.confirmation_status === "pending";
          const sourceKind = blocker.source_field ?? blocker.source_kind;
          const people = [...new Set(blocker.sources.map((source) => source.employee_name).filter(Boolean))];
          const existingAction = actionItems.find(
            (action) => action.source_type === "blocker" && action.source_id === blocker.id
          );
          const blockerQuery = periodQuery;
          const statusMeta = statusMetaFor(blocker, pendingConfirmation);
          const expanded = expandedIds.has(blocker.id);
          return <article key={blocker.id} className="rounded-card border border-border bg-surface transition-colors hover:border-primary/25"><div className="flex items-center gap-2 px-3 py-2.5 sm:px-4"><button type="button" aria-expanded={expanded} aria-label={expanded ? "收起详情" : "展开详情"} onClick={() => toggleBlocker(blocker.id)} className="shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"><ChevronDown className={`h-4 w-4 transition-transform ${expanded ? "" : "-rotate-90"}`} /></button><div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1"><Link href={`/blockers/${blocker.id}?${blockerQuery}`} className="max-w-full truncate text-sm font-semibold hover:text-primary hover:underline sm:max-w-[24rem]">{blocker.title}</Link><Badge tone={blocker.risk_level === "高" ? "red" : blocker.risk_level === "中" ? "amber" : "neutral"}>{blocker.risk_level}风险</Badge>{blocker.status !== "RESOLVED" && blocker.duration_weeks >= 3 && <Badge tone="amber">长期卡点</Badge>}<StatusDot tone={statusMeta.tone}>{statusMeta.label}</StatusDot>{sourceKind && <Badge tone="neutral">{sourceKind === "resource_need" ? "资源需求" : sourceKind}</Badge>}<span className="ml-auto hidden shrink-0 text-xs text-muted-foreground lg:inline">持续 {blocker.duration_weeks} 周 · 出现 {blocker.occurrence_count} 次{people.length ? ` · ${people.join("、")}` : ""}</span></div><AdminOnly><Button variant="outline" size="sm" onClick={() => openBlockerAction(blocker)}>{existingAction ? <><Pencil className="h-3.5 w-3.5" />查看/更新跟进</> : <><MessageSquare className="h-3.5 w-3.5" />纳入跟进</>}</Button></AdminOnly></div>{expanded && <div className="border-t border-border px-3 pb-3.5 pt-3 sm:px-4"><p className="text-xs text-muted-foreground">首次出现 {formatDate(blocker.first_seen_date)} · 最近出现 {formatDate(blocker.last_seen_date)} · 持续 {blocker.duration_weeks} 周 · 出现 {blocker.occurrence_count} 次{blocker.related_project ? ` · 项目：${blocker.related_project}` : ""}{people.length ? ` · 涉及：${people.join("、")}` : ""}</p>{blocker.description && <p className="mt-2 text-sm text-muted-foreground">{blocker.description}</p>}<div className="mt-3 space-y-2"><p className="text-xs font-medium text-muted-foreground">原文来源 · {blocker.sources.length} 篇</p>{blocker.sources.length === 0 ? <p className="text-xs text-warning">暂未返回可点击的原文来源，不能作为正式确认依据。</p> : blocker.sources.map((source) => { const sourceQuery = source.period_start && source.period_end ? `start=${source.period_start}&end=${source.period_end}` : periodQuery; return <Link key={source.report_id} href={`/reports/${source.report_id}?${sourceQuery}`} className="block rounded-control bg-muted/60 px-3 py-2 transition-colors hover:bg-primary-soft"><div className="flex items-center justify-between gap-2 text-xs"><span className="font-medium">{source.employee_name ?? "未知员工"} · {source.title ?? "周报"}</span><span className="inline-flex items-center gap-1 text-primary">查看原文 <ExternalLink className="h-3 w-3" /></span></div><p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{source.source_text}</p></Link>; })}</div></div>}</article>;
        })}</div>}
        {/* 候选层：单周一次性提及不进待处理主列表，折叠在这里备查，点开可核对原文。 */}
        {(filterKey === "open" || filterKey === "all") && candidateItems.length > 0 && <div className="border-t border-border px-4 py-3"><button type="button" onClick={() => setCandidateOpen((prev) => !prev)} aria-expanded={candidateOpen} className="flex w-full items-center gap-2 text-left text-xs text-muted-foreground transition-colors hover:text-foreground"><ChevronDown className={`h-3.5 w-3.5 shrink-0 transition-transform ${candidateOpen ? "" : "-rotate-90"}`} />另有 {candidateItems.length} 条一次性提及（候选层），点开核对</button>{candidateOpen && <div className="mt-2.5 space-y-1.5">{candidateItems.map((blocker) => {
          const people = [...new Set(blocker.sources.map((source) => source.employee_name).filter(Boolean))];
          return <div key={blocker.id} className="flex items-center gap-2 rounded-control bg-muted/40 px-3 py-1.5 text-xs"><Link href={`/blockers/${blocker.id}?${periodQuery}`} className="min-w-0 flex-1 truncate text-muted-foreground hover:text-primary hover:underline">{blocker.title}</Link><Badge tone={blocker.risk_level === "高" ? "red" : blocker.risk_level === "中" ? "amber" : "neutral"}>{blocker.risk_level}</Badge><span className="hidden shrink-0 text-muted-foreground sm:inline">{people.length ? people.join("、") : "未知员工"} · {formatDate(blocker.first_seen_date)}</span></div>;
        })}</div>}</div>}
      </Card>}

      {(centerTab === "all" || centerTab === "actions") && <Card className="overflow-hidden p-0">
        <div className="card-head card-head-line"><div><h2 className="text-sm font-semibold">管理跟进</h2><p className="mt-0.5 text-xs text-muted-foreground">把卡点或洞察转成有负责人、下一步和状态记录的动作</p></div><Badge tone={openActionCount ? "blue" : "neutral"}>{actions.loading ? "加载中" : `${openActionCount} 项进行中`}</Badge></div>
        {actions.loading ? <LoadingBlock rows={3} /> : actions.error ? <ErrorState message={actions.error} onRetry={actions.reload} /> : actionItems.length === 0 ? <EmptyState title="暂无管理跟进" desc="在上面的卡点，或管理简报的管理信号中点击“纳入跟进”。" /> : <div className="space-y-2.5 px-4 pb-4">{actionItems.map((action) => <Link key={action.id} href={`/actions/${action.id}${periodQuery ? `?${periodQuery}` : ""}`} className="flex w-full items-start justify-between gap-3 rounded-card border border-border bg-surface px-4 py-3.5 text-left transition-colors hover:border-primary/25"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="truncate text-sm font-medium">{action.title}</span><Badge tone={action.status === "RESOLVED" ? "green" : action.status === "WAITING" ? "amber" : action.status === "IN_PROGRESS" ? "blue" : action.status === "CANCELLED" ? "neutral" : "red"}>{action.status === "RESOLVED" ? "已解决" : action.status === "WAITING" ? "等待反馈" : action.status === "IN_PROGRESS" ? "处理中" : action.status === "CANCELLED" ? "已取消" : "待处理"}</Badge>{action.source_stale && <Badge tone="amber">来源已更新</Badge>}{(action.unread_count ?? 0) > 0 && <span className="rounded-pill bg-destructive px-2 py-0.5 text-xs font-medium text-destructive-foreground">{action.unread_count} 条新回复</span>}</div><p className="mt-1 text-xs text-muted-foreground">{action.owner_name ? `负责人：${action.owner_name}` : "未指定负责人"}{action.next_step ? ` · 下一步：${action.next_step}` : " · 尚未填写下一步"}</p>{action.last_message && <p className="mt-1 line-clamp-1 text-xs text-muted-foreground">{action.last_message.author_role === "owner" ? (action.owner_name ?? "负责人") : "你"}：{action.last_message.excerpt}</p>}</div><ArrowUpRight className="mt-1 h-3.5 w-3.5 shrink-0 text-primary" /></Link>)}</div>}
      </Card>}

      {(editingSource || editingAction) && <ManagementActionEditor open={editorOpen} action={editingAction} source={editingSource} period={editingPeriod} onClose={() => setEditorOpen(false)} onSaved={() => { actions.reload(); blockers.reload(); counts.reload(); }} />}

      {/* 卡点 AI 总结弹层：与管理简报「周期概览」同一样式与纪律 ——
          打开只读缓存；生成/重算由按钮显式触发；pending/running 由上面的轮询刷新。
          stale 时后端不返回旧结论（卡点已变化），由琥珀色提示引导重算。 */}
      <Modal
        open={blockerSummaryOpen}
        title={`卡点与资源需求总结 · ${period.label}`}
        size="xl"
        onClose={() => setBlockerSummaryOpen(false)}
        footer={
          <>
            <Button variant="outline" onClick={() => setBlockerSummaryOpen(false)}>
              关闭
            </Button>
            {blockerSummary.data && blockerSummaryStatus !== "empty" && (
              <AdminOnly>
                <Button
                  loading={blockerSummarySubmitting}
                  disabled={blockerSummaryGenerating}
                  onClick={() => void runBlockerSummary(Boolean(blockerSummary.data?.summary) || blockerSummaryStatus === "stale")}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {blockerSummaryReady && blockerSummary.data?.summary ? "重新生成" : "生成卡点总结"}
                </Button>
              </AdminOnly>
            )}
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-xs text-muted-foreground">
            周期 {periodLabel(period.start, period.end)} · AI 基于本期 {blockerSummary.data?.blockers_total ?? visibleBlockers.length} 条卡点及其周报原文依据汇总，结论可回溯原文
          </p>

          {blockerSummaryGenerating && (
            <div className="flex items-start gap-3 rounded-control bg-muted/40 px-4 py-3">
              <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-primary" />
              <div className="min-w-0">
                <p className="text-sm font-medium">AI 正在汇总本期卡点与资源需求</p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  共 {blockerSummary.data?.blockers_total ?? 0} 条，后台跑完这里会自动显示结论。
                </p>
              </div>
            </div>
          )}

          {blockerSummaryStatus === "empty" && (
            <div className="rounded-control bg-muted/40 px-4 py-3.5 text-sm text-muted-foreground">
              本期没有可总结的卡点或资源需求。
              {coveragePending && " 本期仍有周报未完成 AI 分析，分析完成后会补充卡点，可先点上方「开始分析本期」。"}
            </div>
          )}

          {blockerSummaryStatus === "failed" && (
            <div className="rounded-control bg-destructive-soft px-4 py-3.5 text-sm leading-relaxed text-destructive">
              {blockerSummary.data?.last_error ?? "AI 总结生成失败，请检查 AI 设置后重试。"}
            </div>
          )}

          {blockerSummaryStatus === "stale" && (
            <div className="rounded-control bg-warning-soft px-4 py-3.5 text-sm leading-relaxed text-warning">
              本期卡点在总结生成后有新增、解决或依据变化，旧结论已不再对应当前数据。点「生成卡点总结」按最新卡点重算。
            </div>
          )}

          {blockerSummaryStatus === "not_generated" && (
            <div className="rounded-control bg-muted/40 px-4 py-3.5 text-sm text-muted-foreground">
              本期已有 {blockerSummary.data?.blockers_total ?? 0} 条卡点，点下方「生成卡点总结」让 AI 汇总风险、资源需求与管理建议。
            </div>
          )}

          {blockerSummaryReady && blockerSummary.data?.summary && (
            <div>
              <div className="mb-3 flex flex-wrap items-center gap-2">
                <Badge tone="red">
                  覆盖 {blockerSummary.data.covered_blockers ?? blockerSummary.data.processed_blockers}/
                  {blockerSummary.data.blockers_total} 条卡点
                </Badge>
                {/* 本期卡点多到装不下时，结论只覆盖了优先级最高的一部分，
                    必须显式标注：否则局部结论会被读成「本期全部卡点已看完」。 */}
                {blockerSummary.data.truncated && <Badge tone="amber">仅总结前若干条</Badge>}
                {blockerSummary.data.model && (
                  <span className="text-xs text-muted-foreground">{blockerSummary.data.model}</span>
                )}
                {blockerSummary.data.created_at && (
                  <span className="text-xs text-muted-foreground">
                    生成于 {formatDateTime(blockerSummary.data.created_at)}
                  </span>
                )}
              </div>
              <AssistantAnswer
                text={blockerSummary.data.summary}
                validCitationIds={blockerSummary.data.source_report_ids}
                quietCitations
                className="space-y-3 [&_h2]:text-sm [&_h3]:text-sm [&_h4]:text-sm leading-6"
              />
              <p className="mt-4 text-xs text-muted-foreground">
                {blockerSummary.data.truncated
                  ? `本期 ${blockerSummary.data.blockers_total} 条卡点超出单次读取上限，以上结论只覆盖按风险与持续周数排序的前 ${blockerSummary.data.covered_blockers ?? 0} 条，未覆盖的部分需自行核对。`
                  : "仅基于本期卡点及其周报原文依据生成，不作为绩效评价；结论中的 [#编号] 可点击回看原文。"}
              </p>
            </div>
          )}

          {blockerSummary.error && (
            <ErrorState message={blockerSummary.error} onRetry={blockerSummary.reload} />
          )}
        </div>
      </Modal>
    </div>
  );
}

export default function BlockersPage() {
  return (
    <React.Suspense fallback={<Card><LoadingBlock rows={6} /></Card>}>
      <BlockersPageContent />
    </React.Suspense>
  );
}
