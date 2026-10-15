"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { EChartsOption } from "echarts";
import {
  Activity,
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  CircleAlert,
  Copy,
  Download,
  FileText,
  Loader2,
  MessageSquare,
  RefreshCw,
  Sparkles,
  UsersRound,
} from "lucide-react";
import { ManagementActionEditor, managementActionStatusLabel } from "@/components/management-action-editor";
import { AssistantAnswer } from "@/components/assistant-answer";
import Chart from "@/components/chart";
import {
  Avatar,
  AvatarStack,
  Badge,
  Button,
  Card,
  CardHeader,
  CardHeading,
  Collapsible,
  DeltaPill,
  ErrorState,
  HeroMetricCard,
  LoadingBlock,
  Modal,
  ProgressBar,
  SectionHeader,
} from "@/components/ui";
import { chartColorsForTheme, lineSeries, tooltipStyle } from "@/lib/chart-theme";
import { apiCacheKey, apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/cn";
import { describeReportJob } from "@/lib/sync-job";
import {
  defaultPeriodKey,
  formatDate,
  formatDateTime,
  periodFromUrl,
  periodLabel,
} from "@/lib/format";
import { PeriodPicker, useAsyncData } from "@/lib/use-data";
import { TASK_POLL_MAX_MS, usePolling } from "@/lib/use-polling";
import { useTheme } from "@/components/theme-provider";
import { AdminOnly, ReadOnlyNotice } from "@/components/admin-only";
import { useSession } from "@/components/session-provider";
import type {
  DashboardDepartments,
  DashboardOverview,
  DashboardPeriodSummary,
  DashboardStatus,
  EmployeeItem,
  InsightItem,
  ManagementAction,
  ManagementActionListResponse,
  RemindPreview,
  SyncJobItem,
  SyncStatus,
} from "@/lib/types";

/**
 * 工具栏的三个手动同步口径：
 *   current_period = 抓当前周期开始至今（截止后补抓晚交周报）
 *   last_period = 只抓上一个完整周期（日常补数）
 *   all_history = 抓企微可返回的全部历史（首次接入 / 完整补档）
 * 概览跟随右上角周期：上周用 last_period，本周用 current_period，本月补抓近 35 天。
 */
type SyncMode = "current_period" | "last_period" | "all_history";

const SYNC_LABELS: Record<SyncMode, string> = {
  current_period: "同步本周周报",
  last_period: "同步上周周报",
  all_history: "同步全部历史周报",
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type ActionItem = {
  key: string;
  kind: string;
  label: string;
  title: string;
  summary: string;
  riskLevel: "低" | "中" | "高";
  durationWeeks: number | null;
  evidenceCount: number;
  departments: string[];
  href: string;
  sourceReportId: number | null;
  sourceType: "blocker" | "insight";
  sourceId: number | null;
  sourceKey: string | null;
};

const insightLabels: Record<string, string> = {
  long_blocker: "长期卡点",
  blocker_growth: "问题增加",
  cross_department: "跨部门协同",
  owner_missing: "责任待确认",
  stalled_plan: "计划未推进",
  employee_change: "事项量变化",
};

/** 洞察行的序号 chip 底色直接承载风险等级：低风险的序号不该和红色一样抢眼。 */
const INSIGHT_TONE_CLASS: Record<"red" | "amber" | "blue" | "accent" | "neutral", string> = {
  red: "bg-destructive-soft text-destructive",
  amber: "bg-warning-soft text-warning",
  blue: "bg-primary-soft text-primary",
  accent: "bg-accent-soft text-accent",
  neutral: "bg-muted text-muted-foreground",
};

/** 待处理列表的风险色圆点：一行标题只留一个色点，不再用整块徽标。 */
const SIGNAL_DOT_CLASS: Record<"red" | "amber" | "blue" | "neutral", string> = {
  red: "bg-destructive",
  amber: "bg-warning",
  blue: "bg-primary",
  neutral: "bg-muted-foreground/40",
};

/** 与上一等长周期的差值；任一周期缺数据时返回 undefined，宁可不显示也不显示一个假的 0。 */
function deltaOf(current: number | null | undefined, previous: number | null | undefined) {
  return typeof current === "number" && typeof previous === "number"
    ? current - previous
    : undefined;
}

/**
 * 把 Markdown 总览压成卡片提要用的纯文本。
 *
 * 卡片只有两行空间，直接截断原文会露出 `## 本周重点`、`- ` 和 `[#347]` 这些标记，
 * 两行里一半是符号。这里去掉标记、只留正文文字；完整格式化的结论在弹层里看。
 */
function summaryPreview(markdown: string): string {
  return markdown
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/\[#(?:周报|报告)?\s*\d+\]/g, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\s*\n\s*/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

export default function DashboardPage() {
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  // 只读成员也能看总结（读缓存），只是不能触发生成；按钮文案要跟着变，
  // 否则点开才发现没有生成按钮。
  const { isAdmin } = useSession();
  const router = useRouter();
  const { theme } = useTheme();
  const chartColors = useMemo(() => chartColorsForTheme(theme), [theme]);
  // 周五 21:00 之前默认看上周 —— 本周还没到交周报的时候，看本周只会满屏假警报
  const [period, setPeriod] = useState(() => {
    if (typeof window === "undefined") return periodFromUrl(null, null, undefined, defaultPeriodKey());
    const params = new URLSearchParams(window.location.search);
    return periodFromUrl(params.get("start"), params.get("end"), undefined, defaultPeriodKey());
  });
  const periodLabelShort = period.key === "this_week" ? "本周" : period.key === "last_week" ? "上周" : period.label;
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const [unsubmittedOpen, setUnsubmittedOpen] = useState(false);
  const [summaryModalOpen, setSummaryModalOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingAction, setEditingAction] = useState<ManagementAction | null>(null);
  const [editingSource, setEditingSource] = useState<ActionItem | null>(null);
  const [periodSummaryStarting, setPeriodSummaryStarting] = useState(false);
  const [periodSummaryMessage, setPeriodSummaryMessage] = useState<string | null>(null);
  const [analysisStarting, setAnalysisStarting] = useState(false);
  const [analysisMessage, setAnalysisMessage] = useState<string | null>(null);
  const [analysisRequested, setAnalysisRequested] = useState(false);

  // —— 当前筛选周期概览：周报抓完就自动汇总，这里只负责「看结论」——
  // 唯一真源是右上角的本周 / 上周 / 本月；截止前默认选上周，但用户主动切换后
  // 日期、篇数、缓存结论和提示词语义都必须一起切换。
  // 打开面板读的是缓存结论，不会因为「点开」才去调模型、也不重复烧 token。
  const briefPeriod = period;
  const briefPeriodName = periodLabelShort;
  const briefSyncRequest =
    period.key === "last_week"
      ? { mode: "last_period" }
      : period.key === "this_week"
        ? { mode: "current_period" }
        : {
            // 月/季/年/自定义档：按区间跨度抓历史（多留 7 天缓冲，封顶 366 天）
            mode: "history",
            days: Math.min(
              366,
              Math.ceil(
                (new Date(period.end).getTime() - new Date(period.start).getTime()) / 86400000
              ) + 7
            ),
          };
  const weekBrief = useAsyncData<DashboardPeriodSummary>(
    () =>
      // 全员周报总览仅超管可用（后端 403）；部门管理员拿到固定占位，
      // 不发请求也不显示入口
      isAdmin
        ? apiGet<DashboardPeriodSummary>("/api/dashboard/period-summary", {
            start: briefPeriod.start,
            end: briefPeriod.end,
          })
        : Promise.resolve({ status: "not_generated" } as unknown as DashboardPeriodSummary),
    [briefPeriod.start, briefPeriod.end, isAdmin]
  );
  const [weekBriefOpen, setWeekBriefOpen] = useState(false);
  // 手动兜底（自动链路失败或想立刻重算时用）触发的额外加载态。
  const [weekBriefRunning, setWeekBriefRunning] = useState(false);
  const [weekBriefMessage, setWeekBriefMessage] = useState<string | null>(null);
  const weekBriefStatus = weekBrief.data?.status;
  const weekBriefGenerating = weekBriefStatus === "pending" || weekBriefStatus === "running";
  const weekBriefReady = weekBriefStatus === "success";
  const weekBriefBusy = weekBriefRunning || weekBriefGenerating;

  // —— 企微提醒：二次确认 + 安全预览（默认不真发，避免打扰真人）——
  const [remindOpen, setRemindOpen] = useState(false);
  const [remindLoading, setRemindLoading] = useState(false);
  const [remindPreview, setRemindPreview] = useState<RemindPreview | null>(null);
  const [remindError, setRemindError] = useState<string | null>(null);

  const openRemind = async () => {
    setRemindOpen(true);
    setRemindLoading(true);
    setRemindError(null);
    try {
      const preview = await apiGet<RemindPreview>("/api/notifications/remind/targets", {
        start: period.start,
        end: period.end,
      });
      setRemindPreview(preview);
    } catch (e) {
      setRemindError(e instanceof Error ? e.message : "加载预览失败");
    } finally {
      setRemindLoading(false);
    }
  };

  // dryRun=true 只刷新预览；真正发送必须显式 dry_run=false，且先弹浏览器二次确认。
  const sendRemind = async (dryRun: boolean) => {
    setRemindLoading(true);
    setRemindError(null);
    try {
      const result = await apiPost<RemindPreview>("/api/notifications/remind", {
        start: period.start,
        end: period.end,
        dry_run: dryRun,
      });
      setRemindPreview(result);
    } catch (e) {
      setRemindError(e instanceof Error ? e.message : "发送失败");
    } finally {
      setRemindLoading(false);
    }
  };

  const confirmSendRemind = () => {
    const count = remindPreview?.target_count ?? 0;
    if (
      window.confirm(
        `将向 ${count} 位未提交周报的同事真实发送企微提醒卡片，确认发送？`
      )
    ) {
      void sendRemind(false);
    }
  };

  const deps = useMemo(
    () => [period.key, period.start, period.end],
    [period.key, period.start, period.end]
  );
  // 各请求的缓存键与周期参数：交给 useAsyncData 做「切走再切回即时出内容」的短期缓存。
  const periodParams = useMemo(
    () => ({ start: period.start, end: period.end }),
    [period.start, period.end]
  );

  const overview = useAsyncData<DashboardOverview>(
    () => apiGet("/api/dashboard/overview", periodParams),
    deps,
    { cacheKey: apiCacheKey("/api/dashboard/overview", periodParams) }
  );
  // 首页只展示提交情况与参考分（部门进度条），不渲染事项数/卡点/温度/覆盖率。
  // 传 detail=false 让后端跳过整轮部门分析 —— 那是这个接口最贵的部分。
  const departmentParams = useMemo(
    () => ({ ...periodParams, detail: false }),
    [periodParams]
  );
  const departments = useAsyncData<DashboardDepartments>(
    () => apiGet("/api/dashboard/departments", departmentParams),
    deps,
    { cacheKey: apiCacheKey("/api/dashboard/departments", departmentParams) }
  );
  // 未提交名单：口径与看板一致（在职 - 已提交），离职员工不进名单
  const unsubmittedParams = useMemo(
    () => ({
      start: period.start,
      end: period.end,
      submit_status: "unsubmitted",
      page_size: 100,
    }),
    [period.start, period.end]
  );
  const unsubmitted = useAsyncData<{ items: EmployeeItem[]; total: number }>(
    () => apiGet("/api/employees", unsubmittedParams),
    deps,
    { cacheKey: apiCacheKey("/api/employees", unsubmittedParams) }
  );
  const managementActions = useAsyncData<ManagementActionListResponse>(
    () =>
      // 管理事项是公司级能力，后端对 dept_admin / member 一律 403；
      // 非管理员直接给空列表占位，不发请求也不在「异常 / 风险 / 待处理」
      // 卡里渲染 403 错误条（与 weekBrief 同一处理模式）。
      isAdmin
        ? apiGet("/api/management-actions", periodParams)
        : Promise.resolve({ items: [] } as unknown as ManagementActionListResponse),
    [...deps, isAdmin],
    { cacheKey: apiCacheKey("/api/management-actions", periodParams) }
  );
  // 平台配置（周期规则等说明性信息，公开接口），用于把「按什么规则统计」显式透出
  const config = useAsyncData<{
    period_rule: string;
    period_rule_label: string;
    timezone_offset_hours: number;
    due_weekday: number;
    due_hour: number;
  }>(() => apiGet("/api/config"), [], {
    cacheKey: apiCacheKey("/api/config"),
  });
  const syncStatus = useAsyncData<SyncStatus>(() => apiGet("/api/sync/status"), [], {
    cacheKey: apiCacheKey("/api/sync/status"),
  });

  // 手动同步只负责排队，真正拉取由后台 Worker 执行；页面轮询状态，避免长请求卡死。
  // 默认只抓周报：整批历史重列一次成本不低，而部门/员工通讯录几乎不变。
  const [syncing, setSyncing] = useState<SyncMode | null>(null);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const runSync = async (mode: SyncMode, label: string) => {
    setSyncing(mode);
    setSyncMsg(`${label}已提交后台，正在执行…`);
    try {
      const result = await apiPost<{ message?: string; created?: boolean; job: SyncJobItem }>(
        "/api/sync/wecom/reports/queue",
        { mode }
      );
      // 被去重时继续等待返回的那条任务，结束后仍要给用户看处理数量。
      if (result.created === false) {
        setSyncMsg(result.message ?? "已有同类型同步任务在执行，正在等待结果…");
      }
      const finished = await waitForReportJob(result.job.id);
      if (!mountedRef.current) return;
      syncStatus.reload();
      overview.reload();
      departments.reload();
      unsubmitted.reload();
      if (finished.timedOut) {
        setSyncMsg("同步仍在后台执行；完成后可在「系统设置 → 同步与抓取」查看新增和失败数量。");
      } else {
        setSyncMsg(`${result.job.window ?? label}${describeReportJob(finished.job)}`);
      }
    } catch (e) {
      if (mountedRef.current) setSyncMsg(e instanceof Error ? e.message : "同步失败");
    } finally {
      if (mountedRef.current) setSyncing(null);
    }
  };

  const waitForReportJob = async (jobId: number): Promise<{ job: SyncJobItem; timedOut: boolean }> => {
    // 全历史要逐月扫描，正常完成可能超过十分钟；保持页面打开时直接回报本次结果。
    const deadline = Date.now() + 30 * 60 * 1000;
    let job = await apiGet<SyncJobItem>(`/api/sync/jobs/${jobId}`, undefined, { bypassCache: true });
    while (mountedRef.current && (job.status === "pending" || job.status === "running") && Date.now() < deadline) {
      await sleep(5000);
      job = await apiGet<SyncJobItem>(`/api/sync/jobs/${jobId}`, undefined, { bypassCache: true });
    }
    return { job, timedOut: job.status === "pending" || job.status === "running" };
  };

  /**
   * 等当前排队的同步任务跑完。
   * 返回最新状态与「是否超时」：超时不是失败，任务还在后台跑，页面不该谎报结果。
   */
  const waitForQueue = async (): Promise<{ latest: SyncStatus; timedOut: boolean }> => {
    const deadline = Date.now() + 5 * 60 * 1000;
    let latest = await apiGet<SyncStatus>("/api/sync/status");
    while ((latest.pending_count > 0 || latest.running_count > 0) && Date.now() < deadline) {
      await sleep(2000);
      latest = await apiGet<SyncStatus>("/api/sync/status");
    }
    return { latest, timedOut: latest.pending_count > 0 || latest.running_count > 0 };
  };

  /**
   * 打开周期概览：只看右上角当前筛选范围的结论。
   *
   * 总览是周报抓取后的自动产物（`_enqueue_auto_period_summaries`），所以这里不再
   * 顺手触发抓取或模型调用 —— 每次点开都重跑一遍，等于点一次烧一次 token。
   * 若后台还在生成，下面的轮询会把结论刷新出来。
   */
  const openWeekBrief = () => {
    setWeekBriefOpen(true);
    setWeekBriefMessage(null);
    weekBrief.reload();
  };

  /**
   * 手动兜底：补抓目标周期的周报 → 生成/重算该周期总览。
   * 只在自动链路没跑到、或管理员想立刻重算时才用，成功后在关掉重新跑这条链路。
   */
  const runWeekBrief = async ({ sync, force }: { sync: boolean; force: boolean }) => {
    setWeekBriefRunning(true);
    setWeekBriefMessage(null);
    try {
      if (sync) {
        const queued = await apiPost<{ created?: boolean }>(
          "/api/sync/wecom/reports/queue",
          briefSyncRequest
        );
        if (queued.created !== false) {
          const { latest, timedOut } = await waitForQueue();
          syncStatus.reload();
          // 抓到新周报就要把页面上的提交/未提交口径一起刷新，否则「看结论」变了、
          // 上面的提交率还是旧的，同一个页面两套数字。
          overview.reload();
          departments.reload();
          unsubmitted.reload();
          if (timedOut) {
            // 抓取没跑完就先别汇总：基于半批正文的结论会误导人。
            setWeekBriefMessage(`${briefPeriodName}周报仍在后台抓取，完成后会自动生成总览。`);
            return;
          }
          const failure = latest.reports.last_error_detail;
          if (failure) {
            setWeekBriefMessage(
              failure.hint
                ? `抓取${briefPeriodName}周报失败：${failure.summary} 处理：${failure.hint}`
                : `抓取${briefPeriodName}周报失败：${failure.summary}`
            );
            return;
          }
        }
      }
      await apiPost("/api/dashboard/period-summary", {
        start: briefPeriod.start,
        end: briefPeriod.end,
        force,
      });
      setWeekBriefMessage("已加入后台队列，完成后这里会自动显示结论。");
      weekBrief.reload();
      overview.reload();
    } catch (e) {
      setWeekBriefMessage(e instanceof Error ? e.message : `${briefPeriodName}概览生成失败`);
    } finally {
      setWeekBriefRunning(false);
    }
  };

  const generatePeriodSummary = async (force = false) => {
    setPeriodSummaryStarting(true);
    setPeriodSummaryMessage(null);
    try {
      await apiPost("/api/dashboard/period-summary", {
        start: period.start,
        end: period.end,
        force,
      });
      await overview.reload();
      setPeriodSummaryMessage("已加入后台队列，完成后会自动更新。");
    } catch (e) {
      setPeriodSummaryMessage(e instanceof Error ? e.message : "提交周度总结失败");
    } finally {
      setPeriodSummaryStarting(false);
    }
  };

  const startPeriodAnalysis = async () => {
    setAnalysisStarting(true);
    setAnalysisMessage(null);
    try {
      await apiPost("/api/ai/scan", { start: period.start, end: period.end }, { timeoutMs: 30000 });
      setAnalysisRequested(true);
      await overview.reload();
      setAnalysisMessage("已加入后台分析队列；完成后本页会自动刷新结果。");
    } catch (e) {
      setAnalysisMessage(e instanceof Error ? e.message : "提交分析失败");
    } finally {
      setAnalysisStarting(false);
    }
  };

  const data = overview.data;
  const deptData = departments.data?.items ?? [];
  const unsubmittedList = unsubmitted.data?.items ?? [];
  const periodQuery = `start=${period.start}&end=${period.end}`;
  const withPeriod = (href: string) => `${href}${href.includes("?") ? "&" : "?"}${periodQuery}`;
  const analysisCoverage = data?.analysis_coverage;
  const analysisReady = Boolean(
    analysisCoverage &&
      analysisCoverage.reports_total > 0 &&
      analysisCoverage.analyzed_reports >= analysisCoverage.reports_total
  );
  const analysisPending = Boolean(
    analysisCoverage &&
      analysisCoverage.reports_total > 0 &&
      analysisCoverage.analyzed_reports < analysisCoverage.reports_total
  );
  const periodSummary = data?.ai_period_summary;

  // 后台任务（本期 AI 分析 / 周期总结）进行中时的轮询。
  // 先打轻量状态接口，只有**确有进展**时才整页重拉 —— 这样进度数字会走，
  // 但不会每 4 秒把趋势、异常、洞察全部重算一遍。
  // 上一轮的进度指纹，用来判断「有没有新进展」。
  const lastStatusKeyRef = React.useRef<string | null>(null);
  const pollStatus = React.useCallback(async () => {
    try {
      const status = await apiGet<DashboardStatus>(
        "/api/dashboard/status",
        { start: period.start, end: period.end },
        { bypassCache: true }
      );
      // 指纹带上周期：换周期后即便进度数字恰好一样（例如两周都是 10/10 已完成），
      // 也必须整页重拉，否则新周期的趋势/异常不会出现。
      const key = `${period.start}~${period.end}|${status.analysis_coverage.analyzed_reports}/${status.analysis_coverage.reports_total}|${status.ai_period_summary.status}`;
      if (lastStatusKeyRef.current === key) return; // 没有进展，不白刷整页
      lastStatusKeyRef.current = key;
      await overview.reload();
    } catch {
      // 单次轮询失败保留已展示内容，下一轮再试（与 useAsyncData 的刷新语义一致）。
    }
  }, [period.start, period.end, overview.reload]);

  // 上一等长周期的对照基线：直接取 8 期趋势里的倒数第二个点，不额外请求接口
  const trendPoints = data?.submission_trend ?? [];
  const previousPoint = trendPoints.length >= 2 ? trendPoints[trendPoints.length - 2] : null;
  const previousRate =
    previousPoint && previousPoint.submitted + previousPoint.unsubmitted > 0
      ? (previousPoint.submitted / (previousPoint.submitted + previousPoint.unsubmitted)) * 100
      : null;
  const rateDelta =
    previousRate == null || !data
      ? undefined
      : Math.round((data.submit_rate - previousRate) * 10) / 10;

  // AI 洞察卡的信号列表：只用后端已返回的真实字段（insight 的结论/证据数/部门/持续周数，
  // 或 action_item 的摘要/风险/依据数）。不拼接、不推测，没有数据就返回空数组让卡片走空状态。
  const analysisInsights = useMemo(() => {
    if (!data) return [];
    type Insight = {
      key: string;
      label: string;
      title: string;
      meta: string | null;
      href: string;
      tone: "red" | "amber" | "blue" | "accent" | "neutral";
      /** 序号 chip 的 tooltip 文案；风险已由 chip 底色表达，正文里不再重复一遍。 */
      riskLabel: string;
      detailLabel?: string;
      weight: number;
    };
    const riskTone = (level: string): Insight["tone"] =>
      level === "高" ? "red" : level === "中" ? "amber" : "blue";
    const riskWeight = (level: string) => (level === "高" ? 3 : level === "中" ? 2 : 1);

    const items: Insight[] = [];

    // 1) 管理信号：长期卡点优先展示具体名称，其他洞察展示统计结论。
    (data.insights ?? []).forEach((insight, index) => {
      const isLongBlocker = insight.kind === "long_blocker" && insight.blocker_id != null;
      const latestSource = insight.sources?.[0];
      const where = insight.departments?.length
        ? insight.departments.join("、")
        : insight.employees?.length
          ? insight.employees.join("、")
          : null;
      items.push({
        key: `insight-${insight.kind}-${index}`,
        label: insightLabels[insight.kind] ?? "管理信号",
        title: isLongBlocker ? insight.title.replace(/^长期未解决[：:]\s*/, "") : insight.conclusion || insight.title,
        meta: (isLongBlocker
          ? [insight.duration_weeks ? `持续 ${insight.duration_weeks} 周` : null, `累计出现 ${insight.evidence_count} 次`, where]
          : [where, typeof insight.evidence_count === "number" ? `${insight.evidence_count} 条依据` : null])
          .filter(Boolean)
          .join(" · ") || null,
        href: isLongBlocker
          ? `/blockers/${insight.blocker_id}?${latestSource?.period_start && latestSource.period_end
            ? `start=${encodeURIComponent(latestSource.period_start)}&end=${encodeURIComponent(latestSource.period_end)}`
            : periodQuery}`
          : withPeriod(latestSource?.report_id ? `/reports/${latestSource.report_id}` : "/blockers"),
        tone: riskTone(insight.risk_level),
        riskLabel: insight.risk_level ? `${insight.risk_level}风险` : "管理信号",
        detailLabel: isLongBlocker ? "查看卡点" : undefined,
        weight: riskWeight(insight.risk_level) * 100 + (insight.evidence_count ?? 0),
      });
    });

    // 2) 补充候选卡点：洞察不足时用它补齐，保证卡片不会是空的
    if (items.length < 3) {
      (data.action_items ?? []).forEach((item, index) => {
        items.push({
          key: `action-${item.kind}-${index}`,
          label: item.kind === "blocker" ? "开放卡点" : "待跟进",
          title: item.title,
          meta: [
            item.departments?.length ? item.departments.join("、") : null,
            `${item.source_count} 条依据`,
            item.duration_weeks ? `持续 ${item.duration_weeks} 周` : null,
          ]
            .filter(Boolean)
            .join(" · "),
          href: withPeriod(item.target_href),
          tone: riskTone(item.risk_level),
          riskLabel: `${item.risk_level}风险`,
          weight: riskWeight(item.risk_level) * 100 + (item.duration_weeks ?? 0),
        });
      });
    }

    // 去重（同一事项可能同时出现在洞察与候选中），按风险与证据量排序后取前 3 条
    const seen = new Set<string>();
    return items
      .sort((a, b) => b.weight - a.weight)
      .filter((item) => {
        const key = item.title.trim().toLocaleLowerCase("zh-CN");
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }, [data, withPeriod]);

  // 焦点面板的按钮在页面上方，展开的名单在下方：点完要真的滚过去，否则用户以为按钮没反应
  const revealUnsubmitted = () => {
    setUnsubmittedOpen(true);
    document
      .getElementById("unsubmitted-card")
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // 只有管理员明确点击「开始分析本期」后才轮询，避免页面打开即持续请求。
  // 轮询走轻量状态接口：只需要分析进度，不该重拉整个概览。
  // 超过 5 分钟未完成说明后台任务可能卡死（Worker 挂掉等），停止轮询并提示，
  // 而不是永远「生成中」+ 无限请求。
  const analysisPollGiveUp = React.useCallback(() => {
    setAnalysisMessage(
      "后台分析已超过 5 分钟未完成，可能后台服务异常；请稍后刷新页面确认，或联系管理员检查 Worker。"
    );
  }, []);
  usePolling(() => void pollStatus(), 4000, {
    enabled: analysisRequested && analysisPending,
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: analysisPollGiveUp,
  });

  React.useEffect(() => {
    if (!analysisRequested || !analysisReady) return;
    setAnalysisRequested(false);
    setAnalysisMessage("本期分析已完成，风险和异常已更新。");
  }, [analysisRequested, analysisReady]);

  // 周度总结是后台任务，只在 pending/running 时轮询状态；不会再次提交模型任务。
  usePolling(() => void pollStatus(), 4000, {
    enabled: data?.ai_period_summary.status === "pending" || data?.ai_period_summary.status === "running",
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: analysisPollGiveUp,
  });

  // 概览跟随右上角筛选，但使用独立只读接口轮询，避免 POST 重置后台任务。
  usePolling(() => weekBrief.reload(), 4000, {
    enabled: weekBriefGenerating,
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: analysisPollGiveUp,
  });

  // 提交缺口优先；分数只作为同一提交率下的次级参考。
  const sortedDepts = useMemo(
    () =>
      [...deptData]
        .filter((d) => d.employee_count > 0)
        .sort(
          (a, b) =>
            b.unsubmitted - a.unsubmitted ||
            (a.average_score ?? 101) - (b.average_score ?? 101) ||
            a.name.localeCompare(b.name, "zh-CN")
        ),
    [deptData]
  );

  // 未提交名单按部门分组，一眼看出哪个部门欠最多
  const groupedUnsubmitted = useMemo(() => {
    const map = new Map<string, EmployeeItem[]>();
    for (const emp of unsubmittedList) {
      const key = emp.department_name ?? "未分配部门";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(emp);
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
  }, [unsubmittedList]);

  // 管理行动优先取开放卡点，再补充规则洞察；每一条都必须能回到原始周报依据。
  const actionItems = useMemo<ActionItem[]>(() => {
    if (!data) return [];
    if (data.action_items) {
      const sourceReportFor = (title: string) =>
        data.current_blockers.find((blocker) => blocker.title === title)?.sources[0]?.report_id ??
        data.insights.find((insight) => insight.title === title)?.sources[0]?.report_id ??
        null;
      return data.action_items.map((item, index) => ({
        key: `action-${item.kind}-${index}`,
        kind: item.kind,
        label: item.kind === "blocker" ? "开放卡点" : insightLabels[item.kind] ?? "管理信号",
        title: item.title,
        summary: item.summary,
        riskLevel: item.risk_level,
        durationWeeks: item.duration_weeks || null,
        evidenceCount: item.source_count,
        departments: item.departments,
        href: withPeriod(item.target_href),
        sourceReportId: sourceReportFor(item.title),
        sourceType: item.source_type ?? (item.kind === "blocker" ? "blocker" : "insight"),
        sourceId: item.source_id ?? null,
        sourceKey: item.source_key ?? null,
      }));
    }
    const blockers: ActionItem[] = data.current_blockers.map((blocker) => {
      const departments = [...new Set(blocker.sources.map((source) => source.department_name).filter(Boolean) as string[])];
      return {
        key: `blocker-${blocker.id}`,
        kind: "blocker",
        label: "开放卡点",
        title: blocker.title,
        summary:
          blocker.description ??
          (blocker.risk_level === "高"
            ? "高风险事项仍处于开放状态，需要尽快确认推进节点。"
            : "该事项仍处于开放状态，需要确认下一步推进节点。"),
        riskLevel: blocker.risk_level,
        durationWeeks: blocker.duration_weeks,
        evidenceCount: blocker.sources.length,
        departments,
        href: withPeriod(`/blockers/${blocker.id}`),
        sourceReportId: blocker.sources[0]?.report_id ?? null,
        sourceType: "blocker",
        sourceId: blocker.id,
        sourceKey: `blocker:${blocker.id}`,
      };
    });
    const blockerTitles = new Set(blockers.map((item) => item.title));
    const insights: ActionItem[] = data.insights
      .filter((insight) => !blockerTitles.has(insight.title))
      .map((insight: InsightItem, index) => ({
        key: `insight-${insight.kind}-${index}`,
        kind: insight.kind,
        label: insightLabels[insight.kind] ?? "管理信号",
        title: insight.title,
        summary: insight.conclusion,
        riskLevel: insight.risk_level,
        durationWeeks: insight.kind === "long_blocker" || insight.kind === "stalled_plan" ? 3 : null,
        evidenceCount: insight.evidence_count,
        departments: insight.departments,
        href: insight.sources[0]?.report_id ? withPeriod(`/reports/${insight.sources[0].report_id}`) : `/blockers?${periodQuery}`,
        sourceReportId: insight.sources[0]?.report_id ?? null,
        sourceType: "insight",
        sourceId: null,
        sourceKey: null,
      }));
    const rank = { 高: 3, 中: 2, 低: 1 };
    return [...blockers, ...insights]
      .sort((a, b) => rank[b.riskLevel] - rank[a.riskLevel] || (b.durationWeeks ?? 0) - (a.durationWeeks ?? 0))
      .slice(0, 6);
  }, [data, periodQuery]);

  const actionFor = (item: ActionItem): ManagementAction | null => {
    const items = managementActions.data?.items ?? [];
    return items.find((action) => {
      if (item.sourceType === "blocker") {
        return action.source_type === "blocker" && action.source_id === item.sourceId;
      }
      return action.source_type === "insight" && Boolean(item.sourceKey) && action.source_key === item.sourceKey;
    }) ?? null;
  };

  // 已经进入跟进的事项不再作为候选重复展示，保持「需要处理」区块一事一处。
  const suggestedActionItems = useMemo(
    () => actionItems.filter((item) => !actionFor(item)),
    [actionItems, managementActions.data]
  );

  const activeFollowUps = useMemo(
    () =>
      (managementActions.data?.items ?? [])
        .filter((action) => action.status !== "RESOLVED" && action.status !== "CANCELLED")
        .sort((a, b) => (a.due_date ?? "9999-12-31").localeCompare(b.due_date ?? "9999-12-31")),
    [managementActions.data]
  );

  // 「异常 / 风险 / 待处理」只保留一行标题，所以三组来源合成一个去重后的列表：
  // 跟进中排最前（已经有人负责），其次候选卡点，最后异常周报；同标题只留一条。
  // 徽标的条数直接取这个列表长度，保证「看到几条 = 说几条」。
  const signalRows = useMemo(() => {
    type SignalRow = {
      key: string;
      title: string;
      /** 行尾的小字说明：跟进状态 / 事项类别 / 异常原因 */
      tag: string | null;
      tone: "red" | "amber" | "blue" | "neutral";
      href: string;
      stale?: boolean;
      followUp?: ManagementAction;
    };
    const seen = new Set<string>();
    const rows: SignalRow[] = [];
    const push = (row: SignalRow) => {
      const key = row.title.trim().toLocaleLowerCase("zh-CN");
      if (!key || seen.has(key)) return;
      seen.add(key);
      rows.push(row);
    };

    activeFollowUps.forEach((action) =>
      push({
        key: `follow-${action.id}`,
        title: action.title,
        tag: managementActionStatusLabel(action.status),
        tone:
          action.status === "IN_PROGRESS" ? "blue" : action.status === "WAITING" ? "amber" : "red",
        href: action.source_href ?? `/blockers?${periodQuery}`,
        stale: action.source_stale,
        followUp: action,
      })
    );

    suggestedActionItems.forEach((item) =>
      push({
        key: item.key,
        title: item.title,
        // 不写类别标签：这批行本来就全是卡点候选，逐行重复「开放卡点」只是噪音，
        // 风险等级已经由圆点颜色表达。
        tag: null,
        tone: item.riskLevel === "高" ? "red" : item.riskLevel === "中" ? "amber" : "blue",
        href: item.href,
      })
    );

    (data?.abnormal_reports ?? []).forEach((report) =>
      push({
        key: `abnormal-${report.id}`,
        title: report.title || "无标题周报",
        tag: report.reasons[0] ?? "内容异常",
        tone: "red",
        href: `${report.href}?${periodQuery}`,
      })
    );

    return rows;
  }, [activeFollowUps, suggestedActionItems, data?.abnormal_reports, periodQuery, data]);

  const managementSignalCount = signalRows.length;

  const copyNames = async () => {
    const text = unsubmittedList.map((e) => e.name).join("、");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setCopyError(false);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyError(true);
      setTimeout(() => setCopyError(false), 2600);
    }
  };

  // 趋势图：三条线里只有「已提交」带面积（它是本页的核心量），
  // 未提交与均分保持细线，避免三条面积互相遮挡。
  const trendOption = useMemo<EChartsOption>(() => {
    const points = data?.submission_trend ?? [];
    const label = (point: (typeof points)[number]) => formatDate(point.period_start).slice(5);
    return {
      grid: { left: 4, right: 8, top: 32, bottom: 0, containLabel: true },
      tooltip: {
        trigger: "axis",
        ...tooltipStyle(),
        formatter: (params: unknown) => {
          const first = (Array.isArray(params) ? params[0] : params) as { dataIndex?: number } | undefined;
          const point = points[first?.dataIndex ?? 0];
          if (!point) return "";
          return [
            `<b>${formatDate(point.period_start)} ~ ${formatDate(point.period_end)}</b>`,
            `已提交：${point.submitted} 人`,
            `未提交：${point.unsubmitted} 人`,
            `内容均分：${point.average_score == null ? "待分析" : point.average_score}`,
          ].join("<br />");
        },
      },
      xAxis: { type: "category", boundaryGap: false, data: points.map(label) },
      yAxis: [
        { type: "value", minInterval: 1 },
        { type: "value", min: 0, max: 100, splitLine: { show: false }, axisLabel: { formatter: "{value}" } },
      ],
      series: [
        lineSeries({
          name: "已提交",
          data: points.map((point) => point.submitted),
          color: chartColors.accent,
          area: true,
          width: 2.6,
        }),
        lineSeries({
          name: "未提交",
          data: points.map((point) => point.unsubmitted),
          color: chartColors.neutral,
          dashed: true,
          width: 2,
        }),
        lineSeries({
          name: "内容均分",
          data: points.map((point) => point.average_score),
          color: chartColors.primary,
          yAxisIndex: 1,
          width: 2,
        }),
      ],
    };
  }, [chartColors, data]);

  // 部门占比环图：直接用 dashboard 已返回的人数口径，不再单独请求接口。
  // 图例由卡片内的自定义行承担（带具体人数），所以不启用 ECharts 自带图例。
  const deptSplitOption = useMemo<EChartsOption>(() => {
    const submitted = data?.submitted ?? 0;
    const unsubmitted = data?.unsubmitted ?? 0;
    if (submitted + unsubmitted === 0) return {};
    return {
      tooltip: { trigger: "item", ...tooltipStyle() },
      grid: { left: 0, right: 0, top: 0, bottom: 0 },
      legend: { show: false },
      series: [
        {
          type: "pie",
          radius: ["60%", "82%"],
          center: ["50%", "50%"],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: chartColors.surface, borderWidth: 3, borderRadius: 6 },
          label: { show: false },
          labelLine: { show: false },
          emphasis: { scale: true, scaleSize: 4 },
          data: [
            { value: submitted, name: "已提交", itemStyle: { color: chartColors.accent } },
            { value: unsubmitted, name: "未提交", itemStyle: { color: chartColors.track } },
          ],
        },
      ],
    };
  }, [chartColors, data]);

  return (
    <div>
      {/* 顶部工具栏：标题、周期、动作、周期切换压成一条。
          故意不放「最后同步 / 下次运行」这类系统状态小字 —— 它们是运维信息，
          放在业务指标正上方只会挤占首屏；同步异常的结论统一走侧栏消息中心。 */}
      <header className="mb-4">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
            <h1 className="text-xl font-semibold tracking-tight">{periodLabelShort}管理简报</h1>
            <p className="text-xs text-muted-foreground">
              {periodLabel(period.start, period.end)}
              {config.data ? ` · ${config.data.period_rule_label}` : ""}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {/* 抓取/催交/重算是写操作：只读成员后端会 403，这里直接不显示 */}
            <AdminOnly>
              <Button
                variant="outline"
                size="sm"
                onClick={() => runSync("current_period", SYNC_LABELS.current_period)}
                loading={syncing === "current_period"}
                disabled={syncing !== null}
                title="抓取本周开始至今的周报，补回截止时间后提交的内容"
              >
                {syncing !== "current_period" && <RefreshCw className="h-3.5 w-3.5" />}
                {SYNC_LABELS.current_period}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => runSync("last_period", SYNC_LABELS.last_period)}
                loading={syncing === "last_period"}
                disabled={syncing !== null}
                title="只抓上一个完整周期的周报（日常补数）；重复点击不会重复抓取"
              >
                {syncing !== "last_period" && <RefreshCw className="h-3.5 w-3.5" />}
                {SYNC_LABELS.last_period}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => runSync("all_history", SYNC_LABELS.all_history)}
                loading={syncing === "all_history"}
                disabled={syncing !== null}
                title="按月抓取企微可返回的全部历史周报；内容未变不会重复入库，首次执行时间较长"
              >
                {SYNC_LABELS.all_history}
              </Button>
            </AdminOnly>
            <PeriodPicker value={period} onChange={setPeriod} />
          </div>
        </div>

        {/* 第二行只保留「自动同步是否在跑」这一个状态。
            「最后同步 / 下次运行 / 队列 / 同步部门与员工」已按要求删除：
            它们是运维信息，挂在业务指标正上方只会挤占首屏；
            同步异常的结论走侧栏消息中心，定时与手动抓取归系统设置。 */}
        {syncStatus.data && (
          <div className="mt-2 text-xs text-muted-foreground">
            自动同步
            <b className={cn("ml-1", syncStatus.data.auto_enabled ? "text-accent" : "text-foreground")}>
              {syncStatus.data.auto_enabled ? "已开启" : "未开启"}
            </b>
          </div>
        )}
        <ReadOnlyNotice className="mt-3 rounded-control bg-muted/50 px-3.5 py-2.5 text-xs leading-relaxed text-muted-foreground" />
      </header>
      {[syncMsg, analysisMessage].filter(Boolean).map((message, index) => (
        <div key={index} className="mb-4 flex items-center gap-2 rounded-control bg-primary-soft/70 px-3.5 py-2.5 text-xs text-foreground">
          {index === 0 ? <RefreshCw className="h-3.5 w-3.5 text-primary" /> : <Sparkles className="h-3.5 w-3.5 text-primary" />}
          {message}
        </div>
      ))}

      {overview.error && (
        <Card className="mb-4">
          <ErrorState message={overview.error} onRetry={overview.reload} />
        </Card>
      )}

      {/* 第二行：提交率主卡（约 35%）+ 两张紧凑 KPI（约 32.5%）+ AI 周期洞察（约 32.5%）。
          三列同高，避免「一张特别巨大、其他特别空」。 */}
      {overview.loading ? (
        <Card className="mb-4">
          <LoadingBlock rows={3} />
        </Card>
      ) : data ? (
        <div className="mb-4 grid items-stretch gap-4 lg:grid-cols-[minmax(0,7fr)_minmax(0,6.5fr)_minmax(0,6.5fr)]">
          {/* 主卡：保留 88% 作为唯一大数字，但去掉虚高的空白，把人数收进底部 */}
          <HeroMetricCard
            eyebrow="本期提交率"
            value={data.submit_rate}
            unit="%"
            delta={rateDelta}
            ring={data.submit_rate}
            actions={
              <>
                {data.unsubmitted > 0 && (
                  <Button size="sm" variant="onHero" onClick={revealUnsubmitted}>
                    处理未提交名单
                  </Button>
                )}
                <AdminOnly>
                  <Button
                    size="sm"
                    variant="onHero"
                    className="bg-white/15 text-white hover:bg-white/25"
                    onClick={openRemind}
                  >
                    <MessageSquare className="h-3.5 w-3.5" />
                    生成提醒预览
                  </Button>
                </AdminOnly>
              </>
            }
            footer={
              <>
                <span className="num font-medium text-white">{data.submitted}</span>
                /{data.employee_total} 人已提交
                {data.unsubmitted > 0 ? (
                  <span className="num font-medium text-white"> · 还差 {data.unsubmitted} 人</span>
                ) : (
                  <span className="text-white"> · 全部完成</span>
                )}
              </>
            }
          >
            {/* 用真实未提交名单填掉主卡中部的空白：直接回答"哪些人值得关注" */}
            {unsubmittedList.length > 0 && (
              <div className="rounded-control bg-white/10 p-3">
                <p className="text-xs text-accent-foreground/75">待跟进 · 未提交名单</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {unsubmittedList.slice(0, 4).map((emp) => (
                    <button
                      key={emp.id}
                      type="button"
                      onClick={() => router.push(`/reports?employee_id=${emp.id}`)}
                      className="inline-flex items-center gap-1.5 rounded-pill bg-white/90 py-0.5 pl-0.5 pr-2 text-xs font-medium text-foreground transition-colors hover:bg-white"
                      title={`查看 ${emp.name} 的历史周报`}
                    >
                      <Avatar name={emp.name} size={18} />
                      {emp.name}
                    </button>
                  ))}
                  {unsubmittedList.length > 4 && (
                    <span className="self-center text-xs text-accent-foreground/80">
                      还有 {unsubmittedList.length - 4} 人
                    </span>
                  )}
                </div>
              </div>
            )}
          </HeroMetricCard>

          {/* 提交口径：两张指标合成一张卡上下排列（参考图里 Total Income / Expense 那种做法），
              避免两张矮卡各被拉高一半留白。 */}
          <Card className="flex flex-col p-0">
            <CardHeader
              title={<CardHeading icon={FileText} tone="neutral" title="提交口径" desc="只统计在职员工，数字与首页提交率同源" />}
            />
            <div className="flex flex-1 flex-col gap-3 px-5 pb-5">
              {/* 两个指标并排成等宽小格：比上下堆叠更容易横向比对，也避免卡片中缝出现大片空白。
                  小格用 flex-1 吸收卡片多余高度 —— 行高由最高的主卡决定，不留空白只能让内容长高。 */}
              <div className="grid flex-1 grid-cols-2 gap-3">
                {([
                  {
                    label: "应提交周报数",
                    value: data.report_total,
                    unit: "篇",
                    hint: "按周期统计的在职员工周报",
                    tone: "text-foreground",
                    delta: deltaOf(data.report_total, previousPoint?.report_total),
                    positiveIsGood: true,
                  },
                  {
                    label: "未提交人数",
                    value: data.unsubmitted,
                    unit: "人",
                    hint:
                      data.unsubmitted > 0
                        ? `涉及 ${groupedUnsubmitted.length} 个部门`
                        : "本期没有缺口",
                    tone: data.unsubmitted > 0 ? "text-warning" : "text-accent",
                    delta: deltaOf(data.unsubmitted, previousPoint?.unsubmitted),
                    positiveIsGood: false,
                  },
                ]).map((item) => (
                  /* 指标格按「标签 / 数值+环比 / 说明」三段排。
                     标签固定一行、说明固定两行高，中间的数值行才能吃掉剩余空间 ——
                     否则两格的标签或说明相差一行，两个数字就会上下错位。 */
                  <div
                    key={item.label}
                    className="flex flex-col rounded-control bg-muted/40 px-4 py-4"
                  >
                    <span className="truncate text-xs text-muted-foreground">{item.label}</span>
                    <div className="mt-auto flex flex-wrap items-end gap-x-2 gap-y-1 pt-3">
                      <p className={cn("text-[28px] font-semibold leading-none num", item.tone)}>
                        {item.value}
                        <span className="ml-1.5 text-sm font-normal text-muted-foreground">{item.unit}</span>
                      </p>
                      {typeof item.delta === "number" && (
                        <DeltaPill
                          value={item.delta}
                          positiveIsGood={item.positiveIsGood}
                          filled
                          className="mb-0.5"
                        />
                      )}
                    </div>
                    <p className="mt-2 min-h-[2.25rem] text-xs leading-snug text-muted-foreground">
                      {item.hint}
                    </p>
                  </div>
                ))}
              </div>
              {/* 进度条承担「覆盖多少在职员工」这句话，指标格里就不再重复人数 */}
              <div>
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="text-muted-foreground">提交进度</span>
                  <span className="font-medium num text-foreground">
                    {data.submitted}
                    <span className="font-normal text-muted-foreground"> / {data.employee_total} 名在职员工</span>
                  </span>
                </div>
                <ProgressBar
                  className="mt-2"
                  value={data.submitted}
                  total={data.employee_total}
                  tone="accent"
                  showPct={false}
                />
              </div>
            </div>
          </Card>

          {/* AI 周期洞察：优先展示"帮管理者决策"的内容。
              有周期总结时先给结论文本，下面补 2-3 条真实管理信号；
              未生成时给空状态 + 直接生成入口，不伪造洞察。 */}
          <Card className="flex flex-col overflow-hidden p-0">
            <CardHeader
              title={
                <CardHeading
                  icon={Sparkles}
                  tone="accent"
                  title={`AI ${periodLabelShort}洞察`}
                  desc={
                    periodSummary?.summary
                      ? `总结基于 ${periodSummary.processed_reports}/${periodSummary.reports_total} 篇周报`
                      : periodSummary?.status === "pending" || periodSummary?.status === "running"
                        ? "正在读取本期周报…"
                        : analysisPending
                          ? `逐篇分析进行中 · 待分析 ${data.analysis_coverage.reports_total - data.analysis_coverage.analyzed_reports} 篇`
                          : "尚未生成本期总结"
                  }
                />
              }
              action={
                analysisInsights.length > 0 ? (
                  <Badge tone="accent">{analysisInsights.length} 条信号</Badge>
                ) : null
              }
            />
            <div className="flex flex-1 flex-col px-5 pb-4">
              {periodSummary?.summary ? (
                /* 卡片只做两行提要：把 Markdown 标记与引用编号去掉，否则首屏会露出
                   「## 本周重点 - …」这类符号，既占字又难读。完整结论在弹层里看。
                   提要本身可点：这是卡上最大的文本块，读两行被截断后最自然的动作
                   就是「点开看全文」，不该要求用户去找底部的小按钮。 */
                <button
                  type="button"
                  onClick={() => setSummaryModalOpen(true)}
                  title="点击查看完整总结"
                  className="-mx-2 -my-1 block w-full rounded-control px-2 py-1 text-left transition-colors hover:bg-primary-soft/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {/* clamp 元素不能再加 block/flex 等 display 类：Tailwind 产物里
                     .block 排在 .line-clamp-* 之后，display 会覆盖掉 -webkit-box，
                     截断直接失效（曾把整段总结全文铺开、整行卡片被撑到几千像素）。 */}
                  <span className="line-clamp-2 text-sm leading-relaxed text-foreground">
                    {summaryPreview(periodSummary.summary)}
                  </span>
                </button>
              ) : (
                /* 空状态压成一行：这段说明性文字不该和「信号列表」抢同样的垂直空间 */
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {periodSummary?.status === "pending" || periodSummary?.status === "running"
                    ? `正在读取本期 ${periodSummary.reports_total} 篇周报，完成后在这里给出全员总结。`
                    : periodSummary?.status === "stale"
                      ? "周报数据已变化，需要重新生成本期总结。"
                      : periodSummary?.status === "failed"
                        ? `生成失败：${periodSummary.last_error ?? "请检查 AI 设置"}`
                        : periodSummary?.status === "empty"
                          ? "本期还没有已提交周报，暂时无法生成总结。"
                          : "尚未生成本期总结，生成后可直接在这里看结论。"}
                </p>
              )}

              {/* 真实管理信号：来自已有的 insights / action_items，按风险与证据量排序。
                  只展示前 2 条以保持三列等高；其余通过底部链接进入处理中心查看，不省略也不虚报总数。 */}
              {analysisInsights.length > 0 ? (
                <ul className="mt-3.5 space-y-0.5 border-t border-border/60 pt-3">
                  {analysisInsights.slice(0, 2).map((insight, index) => (
                    <li key={insight.key}>
                      <Link
                        href={insight.href}
                        className="group -mx-2 block rounded-control px-2 py-2 transition-colors hover:bg-primary-soft/50"
                      >
                        <div className="flex items-start gap-2.5">
                          <span
                            className={cn(
                              "mt-px inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-control text-[11px] font-semibold num",
                              INSIGHT_TONE_CLASS[insight.tone]                            )}
                            title={insight.riskLabel}
                            aria-hidden
                          >
                            {index + 1}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-baseline gap-2">
                              <span className="shrink-0 text-xs text-muted-foreground">{insight.label}</span>
                              <p className="line-clamp-2 min-w-0 flex-1 text-[13px] font-medium leading-relaxed">
                                {insight.title}
                              </p>
                            </div>
                            {(insight.meta || insight.detailLabel) && (
                              <div className="mt-1 flex items-center justify-between gap-2 text-xs">
                                {insight.meta && <p className="min-w-0 truncate text-muted-foreground">{insight.meta}</p>}
                                {insight.detailLabel && <span className="inline-flex shrink-0 items-center gap-0.5 text-primary">{insight.detailLabel}<ArrowUpRight className="h-3 w-3" /></span>}
                              </div>
                            )}
                          </div>
                        </div>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-3 border-t border-border/60 pt-3 text-xs text-muted-foreground">
                  {analysisPending
                    ? "本期周报的逐篇风险分析还在进行（与上方总结相互独立），完成后这里会列出需要你关注的事项。"
                    : "当前没有识别到需要你介入的管理信号。"}
                </p>
              )}

              <div className="mt-auto flex flex-wrap items-center gap-2 pt-3.5">
                <Button variant="outline" size="sm" onClick={() => setSummaryModalOpen(true)}>
                  {periodSummary?.summary
                    ? "查看完整总结"
                    : isAdmin
                      ? "生成本期总结"
                      : "查看本期总结"}
                </Button>
                {analysisInsights.length > 2 && (
                  <Link
                    href={`/blockers?${periodQuery}`}
                    className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                  >
                    其余 {analysisInsights.length - 2} 条信号 <ArrowUpRight className="h-3 w-3" />
                  </Link>
                )}
              </div>
            </div>
          </Card>
        </div>
      ) : null}

      {/* 第三行：左 65% = 本期变化（四个对照指标 + 趋势图），右 35% = 异常 / 风险 / 待处理。
          趋势图放进首屏，管理者一眼能看到"在收敛还是扩大"。 */}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,13fr)_minmax(0,7fr)]">
        <Card className="overflow-hidden p-0">
          <CardHeader
            title={<CardHeading icon={Activity} tone="neutral" title="本期变化" desc="和上一等长周期对照，先判断问题是在收敛还是扩大" />}
            action={
              !data?.period_comparison ? (
                <span className="text-xs text-muted-foreground">对照基线累计中</span>
              ) : data?.submission_trend?.length ? (
                <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                  <BarChart3 className="h-3.5 w-3.5 text-primary/60" />
                  提交趋势 · 近 8 个周期
                </span>
              ) : null
            }
          />
          <div className="px-5 pb-5">
            {overview.loading ? (
              <LoadingBlock rows={3} />
            ) : data ? (
              <>
                {/* 两个指标 + 一个动作。指标只留「有动作可做」的开放卡点与高风险事项；
                    完成/成果、进行中/计划已移除——它们要等 AI 分析完成才有数，
                    未分析时只能显示「待分析」，占着首屏却答不了任何问题。
                    第三块「周期概览」跟随右上角的本周 / 上周 / 本月筛选，
                    结论在高斯模糊面板里看（故意放在这里而不是工具栏，让「本期情况」集中在一处）。 */}
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  {([
                    { label: "开放卡点", tone: "red" as const, value: data.period_comparison?.current.blockers ?? data.blocker_total, delta: data.period_comparison?.deltas.blockers ?? 0, positiveIsGood: false, href: `/blockers?${periodQuery}`, requiresAnalysis: false },
                    { label: "高风险事项", tone: "red" as const, value: data.period_comparison?.current.high_risk_blockers ?? data.high_risk_blockers, delta: data.period_comparison?.deltas.high_risk_blockers ?? 0, positiveIsGood: false, href: `/blockers?risk_level=高&${periodQuery}`, requiresAnalysis: true },
                  ]).map((metric) => {
                    const metricReady = !metric.requiresAnalysis || analysisReady;
                    const toneText: Record<string, string> = {
                      accent: "text-accent",
                      primary: "text-primary",
                      red: "text-destructive",
                    };
                    return (
                      <Link
                        key={metric.label}
                        href={metric.href}
                        className="group rounded-control bg-muted/40 px-3 py-2.5 transition-colors hover:bg-primary-soft/60"
                        title={metric.requiresAnalysis && analysisPending ? "本期仍有周报未完成 AI 分析" : undefined}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-xs text-muted-foreground">{metric.label}</span>
                          {metricReady && data.period_comparison && (
                            <DeltaPill value={metric.delta} positiveIsGood={metric.positiveIsGood} filled />
                          )}
                        </div>
                        <p
                          className={cn(
                            "mt-2 font-semibold leading-none num",
                            metricReady ? cn("text-2xl", toneText[metric.tone]) : "text-base text-muted-foreground"
                          )}
                        >
                          {metricReady ? metric.value : data.analysis_coverage.reports_total ? "待分析" : "—"}
                        </p>
                      </Link>
                    );
                  })}

                  {/* 动作块：与两个指标同尺寸，但用 accent 底色表明「这块能点」。
                      总览由抓取后自动生成，这里只负责打开看结论；生成中显示进度态。
                      全员总览仅超管可见，部门管理员不渲染该入口。 */}
                  {isAdmin && (
                    <button
                      type="button"
                      onClick={openWeekBrief}
                      disabled={weekBriefRunning}
                      className="group rounded-control bg-accent-soft/70 px-3 py-2.5 text-left transition-colors hover:bg-accent-soft disabled:cursor-wait"
                      title={`查看${briefPeriodName}全部周报的 AI 总览与管理建议`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-xs text-muted-foreground">{briefPeriodName}概览</span>
                        {weekBriefBusy ? (
                          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-accent" />
                        ) : (
                          <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-accent opacity-0 transition-opacity group-hover:opacity-100" />
                        )}
                      </div>
                      <p
                        className={cn(
                          "mt-2 font-semibold leading-none",
                          weekBriefBusy || !weekBriefReady
                            ? "text-base text-muted-foreground"
                            : "text-2xl text-accent"
                        )}
                      >
                        {weekBriefBusy ? "生成中" : weekBriefReady ? "看结论" : "待生成"}
                      </p>
                    </button>
                  )}
                </div>
                {data.submission_trend?.length ? (
                  <div className="mt-4">
                    <p className="mb-1 text-xs text-muted-foreground">
                      已提交 / 未提交人数与内容均分，按自然周
                    </p>
                    <Chart option={trendOption} height={168} />
                  </div>
                ) : (
                  <p className="py-8 text-center text-xs text-muted-foreground">暂无趋势数据</p>
                )}
              </>
            ) : null}
          </div>
        </Card>

        <Card className="overflow-hidden p-0">
          <CardHeader
            title={<CardHeading icon={CircleAlert} tone="red" title="异常 / 风险 / 待处理" desc="一行一事，点标题回到原文" />}
            action={<Badge tone={managementSignalCount ? "red" : analysisPending ? "amber" : "accent"}>{managementSignalCount ? `${managementSignalCount} 条` : analysisPending ? "AI待分析" : "0 条"}</Badge>}
          />
          {managementActions.error ? <div className="px-5 pb-5"><ErrorState message={managementActions.error} onRetry={managementActions.reload} /></div> : overview.loading ? <div className="px-5 pb-5"><LoadingBlock rows={3} /></div> : (
            <div className="px-5 pb-5">
              <div className="space-y-1">
                {signalRows.slice(0, 5).map((row) => (
                  <div key={row.key} className="flex items-center gap-1.5 rounded-control transition-colors hover:bg-muted/50">
                    <Link href={row.href} className="group flex min-w-0 flex-1 items-center gap-2.5 rounded-control px-2.5 py-2">
                      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", SIGNAL_DOT_CLASS[row.tone])} />
                      <p className="min-w-0 flex-1 truncate text-sm">{row.title}</p>
                      {row.stale && <span className="shrink-0 text-xs text-muted-foreground">已更新</span>}
                      {row.tag && <span className="shrink-0 text-xs text-muted-foreground">{row.tag}</span>}
                      <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                    </Link>
                    {row.followUp && (
                      /* 已有跟进的事项进详情页：结构化字段的编辑入口在那里，
                         沟通会话也在那里 —— 首页只负责指出「这件事在跟进中」。 */
                      <Link
                        href={`/actions/${(row.followUp as ManagementAction).id}`}
                        className="shrink-0 px-1.5 text-xs text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
                      >
                        {(row.followUp as ManagementAction).unread_count
                          ? `查看 · ${(row.followUp as ManagementAction).unread_count} 条新回复`
                          : "查看跟进"}
                      </Link>
                    )}
                  </div>
                ))}
              </div>
              {analysisPending && managementSignalCount === 0 && <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-control bg-primary-soft/60 px-3.5 py-4"><div className="flex items-start gap-2 text-sm text-muted-foreground"><Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-accent" /><div><p className="font-medium text-foreground">本期周报还在等待 AI 分析</p><p className="mt-1 text-xs">完成分析后，这里会补充风险和异常信号；已识别的资源需求仍会直接显示。</p></div></div><AdminOnly><Button variant="outline" size="sm" onClick={() => void startPeriodAnalysis()} loading={analysisStarting}>开始分析本期</Button></AdminOnly></div>}
              {!analysisPending && managementSignalCount === 0 && <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><CheckCircle2 className="h-4 w-4 text-accent" />当前没有需要立即处理的管理信号。</div>}
              <Link href={`/blockers?${periodQuery}`} className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">打开处理中心 <ArrowUpRight className="h-3.5 w-3.5" /></Link>
            </div>
          )}
        </Card>
      </div>

      <section className="mb-6" aria-labelledby="needs-heading">
        <SectionHeader
          title={<span id="needs-heading">提交缺口</span>}
          desc="按部门归集未提交名单，处理后可回看证据"
          action={<Link href={`/blockers?${periodQuery}`} className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">打开处理中心 <ArrowUpRight className="h-3.5 w-3.5" /></Link>}
        />
        <Card id="unsubmitted-card" className="scroll-mt-20 overflow-hidden p-0">
            <CardHeader
              title={<CardHeading icon={UsersRound} tone="amber" title="未提交周报" desc="只统计在职员工，名单和动作集中在这里" />}
              action={<Badge tone={data?.unsubmitted ? "amber" : "accent"}>{data?.unsubmitted ?? "-"} 人</Badge>}
            />
            {unsubmitted.loading ? <div className="px-5 py-4"><LoadingBlock rows={2} /></div> : unsubmitted.error ? <div className="px-5 py-4"><ErrorState message={unsubmitted.error} onRetry={unsubmitted.reload} /></div> : unsubmittedList.length === 0 ? (
              <div className="flex items-center gap-2 px-5 py-7 text-sm text-muted-foreground"><CheckCircle2 className="h-4 w-4 text-accent" />本期所有在职员工都已提交。</div>
            ) : (
              <div className="px-5 py-4">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                  <AvatarStack names={unsubmittedList.map((emp) => emp.name)} max={7} />
                  <span className="text-xs text-muted-foreground">
                    本期共 <span className="num font-medium text-foreground">{unsubmitted.data?.total ?? unsubmittedList.length}</span> 人未提交
                  </span>
                </div>
                <div className="mt-3.5 flex flex-wrap gap-2">
                  {unsubmittedList.slice(0, 8).map((emp) => <button key={emp.id} onClick={() => router.push(`/reports?employee_id=${emp.id}`)} className="inline-flex items-center gap-1.5 rounded-full border border-border bg-surface py-1 pl-1 pr-2.5 text-xs font-medium transition-colors hover:border-primary/40 hover:bg-primary-soft/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Avatar name={emp.name} size={20} />{emp.name}</button>)}
                </div>
                {unsubmittedList.length > 8 && <p className="mt-2 text-xs text-muted-foreground">还有 {unsubmittedList.length - 8} 人未列出</p>}
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <button onClick={copyNames} className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 text-xs font-medium transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" title="复制当前已加载名单">
                    {copied ? <><CheckCircle2 className="h-3.5 w-3.5 text-accent" />已复制</> : <><Copy className="h-3.5 w-3.5" />复制名单</>}
                  </button>
                  <a href={`/api/exports/unsubmitted?${periodQuery}`} download className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"><Download className="h-3.5 w-3.5" />导出名单</a>
                  <button type="button" onClick={() => setUnsubmittedOpen((open) => !open)} className="ml-auto text-xs font-medium text-primary hover:underline">{unsubmittedOpen ? "收起完整名单" : `展开完整名单（${unsubmitted.data?.total ?? unsubmittedList.length}）`}</button>
                </div>
                {copyError && <p className="mt-2 text-xs text-destructive">复制失败，请检查浏览器剪贴板权限。</p>}
                {unsubmittedOpen && <div className="mt-4 space-y-4 pt-4">
                  {groupedUnsubmitted.map(([deptName, members]) => <div key={deptName}><div className="mb-2 flex items-center gap-2"><span className="text-sm font-medium">{deptName}</span><span className="text-xs num text-muted-foreground">{members.length} 人未交</span></div><div className="flex flex-wrap gap-2">{members.map((emp) => <button key={emp.id} onClick={() => router.push(`/reports?employee_id=${emp.id}`)} className="inline-flex items-center gap-2 rounded-control border border-input bg-surface px-3 py-2 text-sm transition-colors hover:border-primary/40 hover:bg-primary-soft/50"><span className="font-medium">{emp.name}</span>{emp.position && <span className="text-xs text-muted-foreground">{emp.position}</span>}</button>)}</div></div>)}
                  {unsubmittedList.length < (unsubmitted.data?.total ?? 0) && <p className="text-xs text-muted-foreground">当前仅加载前 {unsubmittedList.length} 人，共 {unsubmitted.data?.total} 人未提交。</p>}
                </div>}
              </div>
            )}
          </Card>
      </section>

      <Collapsible
        title={<span className="text-sm font-semibold">深入分析：部门与趋势</span>}
        badge={<Badge>次级分析</Badge>}
        defaultOpen={false}
        action={<Link href="/reports" className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">打开周报查询 <ArrowUpRight className="h-3.5 w-3.5" /></Link>}
      >
        {/* 次级分析：部门缺口与提交构成。趋势图已提到首屏第三行，这里不再重复。 */}
        <div className="grid gap-6 lg:grid-cols-2">
          <section aria-labelledby="departments-heading" className="lg:col-span-1">
            <p id="departments-heading" className="text-xs font-medium text-muted-foreground">
              部门提交缺口 · 按缺口排序
            </p>
            {departments.loading ? (
              <div className="mt-2"><LoadingBlock rows={3} /></div>
            ) : departments.error ? (
              <ErrorState message={departments.error} onRetry={departments.reload} />
            ) : sortedDepts.length === 0 ? (
              <p className="py-6 text-center text-xs text-muted-foreground">当前周期没有部门数据</p>
            ) : (
              <div className="mt-3 space-y-3">
                {sortedDepts.slice(0, 5).map((dept) => (
                  <button
                    key={dept.id}
                    onClick={() => router.push(`/reports?department_id=${dept.id}`)}
                    className="group block w-full text-left"
                    title={`${dept.name}：${dept.submitted}/${dept.employee_count} 人已提交${dept.average_score == null ? "" : `，内容参考分 ${dept.average_score}`}`}
                  >
                    <span className="flex items-center justify-between gap-3">
                      <span className="truncate text-xs font-medium group-hover:text-primary">{dept.name}</span>
                      <span className="shrink-0 text-xs num text-muted-foreground">{dept.submitted}/{dept.employee_count}</span>
                    </span>
                    <ProgressBar
                      value={dept.submitted}
                      total={dept.employee_count}
                      showPct={false}
                      tone={dept.unsubmitted ? "warning" : "accent"}
                      className="mt-2"
                    />
                  </button>
                ))}
              </div>
            )}
            {sortedDepts.length > 5 && (
              <Link href="/reports" className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline">
                还有 {sortedDepts.length - 5} 个部门 <ArrowUpRight className="h-3 w-3" />
              </Link>
            )}
          </section>
          <section aria-labelledby="split-heading" className="lg:col-span-1">
            <p id="split-heading" className="text-xs font-medium text-muted-foreground">
              本期提交构成
            </p>
            {overview.loading ? (
              <div className="mt-2"><LoadingBlock rows={3} /></div>
            ) : data && data.submitted + data.unsubmitted > 0 ? (
              <>
                <Chart option={deptSplitOption} height={186} className="mt-1" />
                <div className="-mt-2 flex items-center justify-center gap-5">
                  <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span className="h-2 w-2 rounded-full bg-accent" />
                    已提交 <span className="num font-semibold text-foreground">{data.submitted}</span>
                  </span>
                  <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span className="h-2 w-2 rounded-full bg-border" />
                    未提交 <span className="num font-semibold text-foreground">{data.unsubmitted}</span>
                  </span>
                </div>
              </>
            ) : (
              <p className="py-6 text-center text-xs text-muted-foreground">暂无提交数据</p>
            )}
          </section>
        </div>
      </Collapsible>

      {editingSource && (
        <ManagementActionEditor
          open={editorOpen}
          action={editingAction}
          source={{
            sourceType: editingSource.sourceType,
            sourceId: editingSource.sourceId ?? undefined,
            sourceKey: editingSource.sourceKey ?? undefined,
            title: editingSource.title,
          }}
          period={period}
          onClose={() => setEditorOpen(false)}
          onSaved={() => { managementActions.reload(); overview.reload(); }}
        />
      )}
    <Modal
      open={summaryModalOpen}
      title={`AI ${periodLabelShort}总结`}
      size="xl"
      onClose={() => setSummaryModalOpen(false)}
      footer={
        <>
          <Button variant="outline" onClick={() => setSummaryModalOpen(false)}>
            关闭
          </Button>
          {periodSummary && periodSummary.status !== "empty" && (
            <AdminOnly>
              <Button
                onClick={() => void generatePeriodSummary(Boolean(periodSummary.summary))}
                loading={periodSummaryStarting}
                disabled={periodSummary.status === "pending" || periodSummary.status === "running"}
              >
                <Sparkles className="h-3.5 w-3.5" />
                {periodSummary.summary ? "重新生成" : "生成总结"}
              </Button>
            </AdminOnly>
          )}
        </>
      }
    >
      {!periodSummary ? (
        <LoadingBlock rows={4} />
      ) : periodSummary.status === "empty" ? (
        <div className="rounded-control bg-muted/40 px-4 py-4 text-sm text-muted-foreground">
          本期还没有已提交周报，暂时无法生成全员总结。
        </div>
      ) : periodSummary.status === "pending" || periodSummary.status === "running" ? (
        <div className="flex items-start gap-3 rounded-control bg-muted/40 px-4 py-4">
          <RefreshCw className="mt-0.5 h-4 w-4 animate-spin text-primary" />
          <div>
            <p className="text-sm font-medium">正在读取本期周报…</p>
            <p className="mt-1 text-xs text-muted-foreground">
              本次将处理 {periodSummary.reports_total} 篇，完成后自动更新。
            </p>
          </div>
        </div>
      ) : periodSummary.stale ? (
        <div className="rounded-control bg-warning-soft px-4 py-4 text-sm text-warning">
          <p className="font-medium">周报数据已变化，需要重新生成</p>
          <p className="mt-1 text-xs">
            上次总结基于旧数据，本期现在有 {periodSummary.reports_total} 篇已提交周报。
          </p>
        </div>
      ) : periodSummary.status === "failed" ? (
        <div className="rounded-control bg-destructive-soft px-4 py-4 text-sm text-destructive">
          <p className="font-medium">AI 总结生成失败</p>
          <p className="mt-1 text-xs">{periodSummary.last_error ?? "请检查 AI 设置后重新生成。"}</p>
        </div>
      ) : periodSummary.summary ? (
        <div>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Badge tone="blue">
              已读 {periodSummary.processed_reports}/{periodSummary.reports_total} 篇
            </Badge>
            {periodSummary.full_context === false && <Badge tone="amber">分批汇总</Badge>}
            {periodSummary.model && <span className="text-xs text-muted-foreground">{periodSummary.model}</span>}
            {periodSummary.created_at && (
              <span className="text-xs text-muted-foreground">
                生成于 {formatDateTime(periodSummary.created_at)}
              </span>
            )}
          </div>
          {/* 不再套第二层 max-h + overflow：Modal 本身就是滚动容器，
              两层滚动框会在底下留一条永远填不满的空白。 */}
          <AssistantAnswer
            text={periodSummary.summary}
            validCitationIds={periodSummary.source_report_ids}
            quietCitations
            /* 总览是「标题 + 列表」的简报体：标题用同样字号靠留白分区，
               比大字号更安静；行距也略收紧，一屏能多看两条。 */
            className="space-y-3 [&_h2]:text-sm [&_h3]:text-sm [&_h4]:text-sm leading-6"
          />
          <p className="mt-4 text-xs text-muted-foreground">
            {periodSummary.full_context === false
              ? "本周期正文超出单次读取上限，以上结论由分批摘要合并而成，跨篇关联可能不完整。"
              : "仅基于本周期全部已提交周报原文一次读全生成，不作为绩效评价。"}
          </p>
        </div>
      ) : (
        <div className="rounded-control bg-muted/40 px-4 py-4 text-sm text-muted-foreground">
          本期有 {periodSummary.reports_total} 篇已提交周报，可点击下方按钮生成全员总结。
        </div>
      )}
      {periodSummaryMessage && (
        <p className="mt-3 text-xs text-muted-foreground">
          {periodSummaryMessage}
        </p>
      )}
    </Modal>
    {/* 周期概览跟随右上角筛选；截止前默认筛选仍是上周。
        周报抓完由后台自动汇总，这里只读结论。
        面板走 Modal 的 glass-strong（高斯模糊），与需求里之前确认的样式一致。 */}
    <Modal
      open={weekBriefOpen}
      title={`${briefPeriodName}概览`}
      size="xl"
      onClose={() => setWeekBriefOpen(false)}
      footer={
        <>
          <Button variant="outline" onClick={() => setWeekBriefOpen(false)}>
            关闭
          </Button>
          <Button
            loading={weekBriefRunning}
            disabled={weekBriefGenerating}
            onClick={() => runWeekBrief({ sync: true, force: true })}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            重新抓取并生成
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-muted-foreground">
          周期 {periodLabel(briefPeriod.start, briefPeriod.end)} · AI 按这一完整周期汇总全部原文并给出管理建议
        </p>

        {weekBriefGenerating && (
          <div className="flex items-start gap-3 rounded-control bg-muted/40 px-4 py-3">
            <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-primary" />
            <div className="min-w-0">
              <p className="text-sm font-medium">AI 正在汇总{briefPeriodName}全部周报</p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                已入库 {weekBrief.data?.reports_total ?? 0} 篇，后台跑完这里会自动显示结论。
              </p>
            </div>
          </div>
        )}

        {weekBriefStatus === "empty" && (
          <div className="rounded-control bg-muted/40 px-4 py-3.5 text-sm text-muted-foreground">
            {briefPeriodName}还没有已提交的周报。点下方「重新抓取并生成」补抓该周期后，系统会再生成总览。
          </div>
        )}

        {weekBriefStatus === "failed" && (
          <div className="rounded-control bg-destructive-soft px-4 py-3.5 text-sm leading-relaxed text-destructive">
            {weekBrief.data?.last_error ?? "AI 总结生成失败，请检查 AI 设置后重试。"}
          </div>
        )}

        {weekBriefStatus === "stale" && (
          <div className="rounded-control bg-warning-soft px-4 py-3.5 text-sm leading-relaxed text-warning">
            {briefPeriodName}周报在生成后有新增或改动，旧结论已不再对应当前正文，因此不再展示。点「重新抓取并生成」可按最新内容重算。
          </div>
        )}

        {/* stale 时后端不返回旧结论（正文已变，旧结论不再代表当前内容），由上面的琥珀色提示引导重算 */}
        {(weekBriefStatus === "not_generated" ||
          weekBriefStatus === "pending" ||
          weekBriefStatus === "running") && (
          <div className="rounded-control bg-muted/40 px-4 py-3.5 text-sm text-muted-foreground">
            {briefPeriodName}已入库 {weekBrief.data?.reports_total ?? 0} 篇周报，总览会在抓取后自动生成；也可以点「重新抓取并生成」立即触发。
          </div>
        )}

        {weekBriefReady && weekBrief.data?.summary && (
          <div>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Badge tone="accent">
                已读 {weekBrief.data.processed_reports}/{weekBrief.data.reports_total} 篇
              </Badge>
              {weekBrief.data.full_context === false && (
                <Badge tone="amber">分批汇总</Badge>
              )}
              {weekBrief.data.model && (
                <span className="text-xs text-muted-foreground">{weekBrief.data.model}</span>
              )}
              {weekBrief.data.created_at && (
                <span className="text-xs text-muted-foreground">
                  生成于 {formatDateTime(weekBrief.data.created_at)}
                </span>
              )}
            </div>
            {/* 直接交给 Modal 滚动，不在内部再放一个滚动框（否则底部会留大片空白） */}
            <AssistantAnswer
              text={weekBrief.data.summary}
              validCitationIds={weekBrief.data.source_report_ids}
              quietCitations
              className="space-y-3 [&_h2]:text-sm [&_h3]:text-sm [&_h4]:text-sm leading-6"
            />
            <p className="mt-4 text-xs text-muted-foreground">
              {weekBrief.data.full_context === false
                ? "本周期正文超出单次读取上限，以上结论由分批摘要合并而成，跨篇关联可能不完整。"
                : `仅基于${briefPeriodName}全部已提交周报原文一次读全生成，不作为绩效评价。`}
            </p>
          </div>
        )}

        {weekBriefMessage && (
          <p className="text-xs text-muted-foreground">{weekBriefMessage}</p>
        )}
      </div>
    </Modal>
    <Modal
      open={remindOpen}
      title="企微提醒预览"
      onClose={() => setRemindOpen(false)}
      footer={
        remindPreview?.sent ? (
          <Button onClick={() => setRemindOpen(false)}>关闭</Button>
        ) : (
          <>
            <Button variant="outline" onClick={() => setRemindOpen(false)}>
              取消
            </Button>
            <Button
              onClick={confirmSendRemind}
              loading={remindLoading}
              disabled={!remindPreview || remindPreview.target_count === 0}
            >
              {remindPreview?.target_count
                ? `确认发送（${remindPreview.target_count} 人）`
                : "无可提醒人员"}
            </Button>
          </>
        )
      }
    >
      {remindLoading && !remindPreview ? (
        <LoadingBlock rows={3} />
      ) : remindError ? (
        <ErrorState message={remindError} />
      ) : remindPreview ? (
        <div className="space-y-3">
          {!remindPreview.sent && (
            <div className="rounded-control bg-warning-soft px-3.5 py-2.5 text-sm leading-relaxed text-warning">
              将为 <b>{remindPreview.target_count}</b> 位未提交周报的同事发送提醒卡片，
              以上内容为实际推送效果预览。点击「确认发送」后<b>会真实发送企微消息</b>
              {remindPreview.skipped.length > 0 &&
                `；另有 ${remindPreview.skipped.length} 人因无企微账号被跳过`}
              。
            </div>
          )}
          {remindPreview.card ? (
            <div className="wechat-card-preview rounded-card bg-wechat-bg p-4">
              <p className="text-sm font-semibold text-wechat-title">{remindPreview.card.title}</p>
              <div
                className="mt-1.5 text-sm leading-relaxed text-wechat-text"
                dangerouslySetInnerHTML={{ __html: remindPreview.card.description }}
              />
              <div className="mt-3 inline-block rounded-control bg-wechat-green px-3 py-1 text-xs font-medium text-white">
                {remindPreview.card.btn_text}
              </div>
            </div>
          ) : (
            <div className="rounded-control bg-warning-soft px-3.5 py-2.5 text-sm leading-relaxed text-warning">
              提醒卡片内容暂不可用，请关闭后重试。
            </div>
          )}
          {remindPreview.sent && (remindPreview.result?.undelivered?.length ?? 0) > 0 && (
            <div className="rounded-control bg-warning-soft px-3.5 py-2.5 text-sm leading-relaxed text-warning">
              企微实际送达 {remindPreview.result?.delivered_count ?? 0} 人，另有{" "}
              {remindPreview.result?.undelivered?.length} 人未被投递（多为自建应用的可见范围没覆盖到），
              详见下方名单。
            </div>
          )}
          {remindPreview.sent && (remindPreview.result?.undelivered?.length ?? 0) === 0 && (
            <div className="rounded-control bg-success-soft px-3.5 py-2.5 text-sm leading-relaxed text-success">
              ✓ 已向 {remindPreview.result?.delivered_count ?? remindPreview.target_count} 位同事发送企微提醒。
            </div>
          )}
          {remindPreview.error && (
            <div
              className={
                remindPreview.outcome === "failed"
                  ? "rounded-control bg-destructive-soft px-3.5 py-2.5 text-sm leading-relaxed text-destructive"
                  : "rounded-control bg-warning-soft px-3.5 py-2.5 text-sm leading-relaxed text-warning"
              }
            >
              {remindPreview.outcome === "failed" ? "发送失败：" : "部分未送达："}
              {remindPreview.error}
            </div>
          )}
          {remindPreview.skipped.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs font-medium text-muted-foreground">以下人员本次不发送：</p>
              {remindPreview.skipped.map((s) => (
                <p key={s.id} className="text-xs text-muted-foreground">
                  · {s.name}（{s.reason}）
                </p>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </Modal>
    </div>
  );
}
