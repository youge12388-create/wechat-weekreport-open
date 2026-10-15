"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  AlertTriangle,
  CalendarDays,
  Download,
  Gavel,
  Loader2,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Input,
  LoadingBlock,
} from "@/components/ui";
import { apiCacheKey, apiDownload, apiGet, apiPost, apiPostQuery } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDateTime, periodLabel } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import { InlineCitations } from "@/components/assistant-answer";
import { AnalysisScanButton } from "@/components/analysis-scan-button";
import { OperatingOverview } from "./overview";
import {
  MonthlyAnomalies,
  MonthlyChanges,
  MonthlyDepartments,
  MonthlyInterventions,
  MonthlySharedIssues,
  MonthlyMainlines,
  MonthlyStuck,
} from "./monthly-sections";
import type {
  OperatingDashboardPayload,
  OperatingMonthlyNarrative,
  OperatingFeedbackEntry,
  OperatingFeedbackModule,
  OperatingOverviewPayload,
  OperatingSignal,
  OperatingSummaryState,
} from "@/lib/types";

/**
 * 页面上的视图档。**刻意不含 `department`** ——
 * 部门维度的信息已并入总览（组织摩擦与瓶颈都带部门），单独一个视图是重复的，已裁掉。
 * 后端的部门 **AI 总结作用域**（`summary_type=department`）仍然保留，那是独立能力。
 */
type OperatingView = "overview" | "monthly";

/**
 * 每月视图的两个分页。
 *
 * 「情况」回答前两问（公司在推什么、这个月变了什么），「问题」回答后三问
 * （哪里卡住、有什么异常、要谁介入）。分两页是为了让每一页都能读完 ——
 * 七块竖排时实测 8023px（7.4 屏），读到后面已经忘了前面。
 *
 * **只有两页**：早先试过七块各占一个标签，项目负责人反馈「改完很怪」——
 * 那等于把一条叙事线拆成七个互不相干的抽屉。两页的分界是「读情况」与「处理问题」，
 * 各页内部仍按原顺序全部铺开，不折叠。
 */
type MonthlyPane = "outlook" | "issues";

const MONTHLY_PANES: Array<{ key: MonthlyPane; label: string }> = [
  { key: "outlook", label: "经营情况" },
  { key: "issues", label: "问题与介入" },
];
/** 显示语气。与 components/ui 的 Badge tone 完全对齐，避免两处枚举各写一套。 */
type Pill = "neutral" | "green" | "amber" | "red" | "blue" | "accent";

interface PeriodRange {
  start: string;
  end: string;
}

const VIEW_LABELS: Record<OperatingView, string> = {
  overview: "总览",
  monthly: "每月",
};

const VIEW_COPY: Record<OperatingView, { title: string }> = {
  overview: {
    title: "经营看板",
  },
  monthly: {
    title: "每月经营情况",
  },
};


/* --------------------------------------------------------------------------- */
/* 基础工具                                                                     */

function dateIso(value: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
}

function isValidIso(value: string | null): value is string {
  return Boolean(value && /^\d{4}-\d{2}-\d{2}$/.test(value));
}

function summaryWaitMinutes(summary: OperatingSummaryState, now: number): number | null {
  if (!summary.queue_updated_at || !now) return null;
  // 后端时间是 UTC naive，与 formatDateTime 的解析口径保持一致。
  const updatedAt = summary.queue_updated_at;
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(updatedAt);
  const timestamp = Date.parse(hasTimezone ? updatedAt : `${updatedAt}Z`);
  return Number.isNaN(timestamp) ? null : Math.max(0, Math.floor((now - timestamp) / 60000));
}

function lastWeekRange(): PeriodRange {
  const today = new Date();
  const weekday = (today.getDay() + 6) % 7;
  const monday = new Date(today);
  monday.setDate(today.getDate() - weekday - 7);
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  return { start: dateIso(monday), end: dateIso(sunday) };
}

/** 每月视图的快捷月份档。 */
interface MonthPreset {
  key: string;
  label: string;
  range: PeriodRange;
}

/**
 * 每月视图的快捷月份：本月 / 上月 / 近三月 / 近半年。
 *
 * 之前每月页只给两个日期输入框，一进页面默认落在「近三个月（这个月初往前两个月）」，
 * 而简报是按具体周期算的 —— 项目负责人因此反馈「怎么没看到变化」：他打开的那一档
 * 根本没有简报，页面退回计数版。日期范围对「看简报」这个动作是多余的自由度，
 * 给固定档位才能保证每次点进去都是同一份东西。
 *
 * **结束日期一律取自然月末，不用 `today`（2026-09-28 项目负责人 确认）**：
 * 简报缓存按精确起止日期存档，结束日期若是「今天」，每过一天就是一个新区间，
 * 页面就会判定「从未生成过」而自动重排 —— 实测 report_count 一篇没变也照样重算，
 * 一次月度简报约 2.5 万 prompt token。固定到月末后，一个快捷档一个月只算一次；
 * 月内新周报入库时后端会把旧简报标 stale，按既有设计不自动重算、手动点即可。
 */
function monthPresets(): MonthPreset[] {
  const today = new Date();
  const year = today.getFullYear();
  const month = today.getMonth();
  const firstOfMonth = (offset: number) => new Date(year, month + offset, 1);
  const lastOfMonth = (offset: number) => new Date(year, month + offset + 1, 0);
  return [
    {
      key: "this_month",
      label: "本月",
      range: { start: dateIso(firstOfMonth(0)), end: dateIso(lastOfMonth(0)) },
    },
    {
      key: "last_month",
      label: "上月",
      range: { start: dateIso(firstOfMonth(-1)), end: dateIso(lastOfMonth(-1)) },
    },
    {
      key: "last_3_months",
      label: "近三月",
      range: { start: dateIso(firstOfMonth(-2)), end: dateIso(lastOfMonth(0)) },
    },
    {
      key: "last_6_months",
      label: "近半年",
      range: { start: dateIso(firstOfMonth(-5)), end: dateIso(lastOfMonth(0)) },
    },
  ];
}

/** 当前区间命中了哪个快捷档（没有命中返回 null，说明是手填的自定义区间）。 */
function activeMonthPreset(range: PeriodRange): string | null {
  const hit = monthPresets().find(
    (preset) => preset.range.start === range.start && preset.range.end === range.end
  );
  return hit ? hit.key : null;
}

function defaultRange(view: OperatingView): PeriodRange {
  // 每月视图默认落「本月」这一档（与快捷档一致），不再用一个对不上的自造区间。
  return view === "monthly" ? monthPresets()[0].range : lastWeekRange();
}

/**
 * 只有「总览」走综合视图的数据形状。每月视图使用明细形状，所以渲染前必须区分。
 */
function isOverview(view: OperatingView): boolean {
  return view === "overview";
}

function reportListHref(departmentId?: number): string {
  // 周报查询页已改为「应交周报人员名单」视图，不再按周期铺报告列表；
  // 部门过滤仍然有效，周期参数不再携带。
  const params = new URLSearchParams();
  if (departmentId !== undefined) params.set("department_id", String(departmentId));
  return `/reports?${params.toString()}`;
}

/**
 * 把文案里的阿拉伯数字抬成前景色 + 等宽，其余留中灰。
 * 实现移到本目录 head.tsx，与总览视图共用一份（2026-09-25 视觉审查收敛）。
 */

/* --------------------------------------------------------------------------- */
/* 版式零件                                                                     */

const CHIP_TONES: Record<Pill, string> = {
  accent: "bg-accent-soft text-accent",
  green: "bg-success-soft text-success",
  amber: "bg-warning-soft text-warning",
  red: "bg-destructive-soft text-destructive",
  blue: "bg-primary-soft text-primary",
  neutral: "bg-muted text-muted-foreground",
};

/* --------------------------------------------------------------------------- */
/* 首屏状态条：数据可信度 + 问题硬指标                                            */

/**
 * 页面最上面的一条：左边是「这份数据可不可信」，中间是「公司现在有哪些问题」。
 *
 * 把问题计数提到这里，是「一眼看到问题」最直接的实现 —— 管理层扫一眼就知道
 * 本期有没有高风险、有几件事待处理，不用往下翻。数字全部来自既有字段，
 * 不新增口径；覆盖不足时**必须**同时显示，否则结论会被当成全覆盖的结论。
 */
function CoverageBar({ summary, period }: {
  summary: OperatingSummaryState;
  period: PeriodRange;
}) {
  const total = summary.analysis_total;
  const completed = summary.analysis_completed;
  const coverageKnown = typeof total === "number" && typeof completed === "number";
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-card border border-border bg-surface px-4 py-2.5 text-xs text-muted-foreground shadow-card">
      <span className="font-medium text-foreground">
        {periodLabel(period.start, period.end)}
      </span>
      <span>
        {coverageKnown
          ? `已完成 AI 分析 ${completed}/${total} 篇`
          : "逐篇 AI 分析状态待核实"}
        {(summary.analysis_failed ?? 0) > 0
          ? ` · ${summary.analysis_failed} 篇分析失败`
          : ""}
      </span>
      <Link
        href={reportListHref()}
        className="ml-auto font-medium text-primary hover:underline"
      >
        查看本期周报
      </Link>
    </div>
  );
}

/* 本月经营简报：先给结论，再给少量有证据的重点。 */
const HIGHLIGHT_META: Record<
  OperatingMonthlyNarrative["highlights"][number]["kind"],
  { label: string; tone: Pill; icon: React.ComponentType<{ className?: string }> }
> = {
  progress: { label: "进展", tone: "accent", icon: TrendingUp },
  risk: { label: "风险", tone: "amber", icon: AlertTriangle },
  decision: { label: "待拍板", tone: "red", icon: Gavel },
};

function AiProgressBar({ label, className }: { label: string; className?: string }) {
  return (
    <div
      role="progressbar"
      aria-label="AI 分析进度"
      aria-valuetext={label}
      className={cn("relative h-1.5 overflow-hidden rounded-full bg-muted", className)}
    >
      <span className="api-loading-progress absolute inset-y-0 left-0 w-[38%] rounded-full bg-accent" />
    </div>
  );
}

function MonthlyWaitingState({
  summary,
  generating,
  error,
  waitMinutes,
  onGenerate,
  range,
  onAnalysisChanged,
}: {
  summary: OperatingSummaryState;
  generating: boolean;
  error: string | null;
  waitMinutes: number | null;
  onGenerate: () => void;
  range: PeriodRange;
  onAnalysisChanged: () => void;
}) {
  if (error) {
    return (
      <Card>
        <ErrorState message={`提交月度简报失败：${error}`} onRetry={onGenerate} />
      </Card>
    );
  }
  if (summary.status === "empty") {
    return (
      <Card>
        <EmptyState title="本期暂无周报" desc="有周报入库后，再生成本期经营简报。" />
      </Card>
    );
  }
  if (summary.analysis_ready !== true) {
    const total = summary.analysis_total;
    if (typeof total !== "number" || total <= 0) {
      return (
        <Card>
          <div className="flex flex-col items-center gap-2 px-6 py-5 text-center">
            <p className="text-sm font-medium text-foreground">逐篇 AI 分析状态无法核实</p>
            <p className="text-xs text-muted-foreground">校验完成前暂不生成公司整体判断；下方部门状态可单独查看。</p>
            <Button size="sm" variant="outline" onClick={onAnalysisChanged}>刷新状态</Button>
          </div>
        </Card>
      );
    }
    const remaining = (summary.analysis_total ?? 0) - (summary.analysis_completed ?? 0);
    const failed = summary.analysis_failed ?? 0;
    const processing = summary.analysis_processing ?? 0;
    return (
      <Card>
        <div className="flex flex-col items-center gap-2 px-6 py-5 text-center">
          <p className="text-sm font-medium text-foreground">
            {failed > 0 ? "本期有周报 AI 分析失败" : processing > 0 ? "AI 正在分析本期周报" : "本期周报尚未完成 AI 分析"}
          </p>
          <p className="text-xs text-muted-foreground">
            已完成 {summary.analysis_completed ?? 0}/{total} 篇
            {failed > 0 ? `，${failed} 篇失败` : ""}；公司整体结论等待全部完成，已就绪部门可在下方查看。
          </p>
          <AnalysisScanButton
            start={range.start}
            end={range.end}
            remainingOverride={remaining}
            totalOverride={total}
            onChanged={onAnalysisChanged}
          />
        </div>
      </Card>
    );
  }
  if (summary.status === "failed" && !generating) {
    return (
      <Card>
        <ErrorState
          message={`月度简报生成失败${summary.last_error ? `：${summary.last_error}` : "。请检查 AI 设置后重试。"}`}
          onRetry={onGenerate}
        />
      </Card>
    );
  }
  if (
    (summary.status === "stale" || summary.stale) &&
    summary.status !== "pending" && summary.status !== "running" && !generating
  ) {
    return (
      <Card>
        <div className="flex flex-col items-center gap-2 px-6 py-5 text-center">
          <p className="text-sm font-medium text-foreground">本期公司结论需要更新</p>
          <p className="text-xs text-muted-foreground">旧结论作为历史快照保留；部门卡片可单独更新。</p>
          <Button size="sm" variant="outline" onClick={onGenerate} loading={generating}>
            <Sparkles className="h-3.5 w-3.5" />重新生成简报
          </Button>
        </div>
      </Card>
    );
  }
  const pending = summary.status === "pending";
  const running = summary.status === "running";
  if (generating || pending || running) {
    const longWait = waitMinutes !== null && waitMinutes >= (pending ? 2 : 5);
    return (
      <Card>
        <div className="flex min-h-64 flex-col items-center justify-center gap-3 px-6 py-14 text-center">
          <Sparkles className="h-6 w-6 text-accent-deep" />
          <p className="text-sm font-medium text-foreground">
            {generating && !pending && !running
              ? "正在提交月度简报任务"
              : pending
                ? "月度简报等待后台处理"
                : "AI 正在生成月度简报"}
          </p>
          <AiProgressBar
            label={pending ? "等待后台处理" : running ? "AI 正在生成" : "正在提交任务"}
            className="w-64 max-w-full"
          />
          <p className="max-w-md text-xs leading-5 text-muted-foreground">
            {longWait
              ? pending
                ? `已等待约 ${waitMinutes} 分钟，后台任务仍未开始。可检查 Worker 运行和队列积压；页面会继续自动更新。`
                : `已处理约 ${waitMinutes} 分钟，可能正在等待模型响应或重试；页面会继续自动更新。`
              : "生成完成后会自动显示经营情况和需要处理的事。"}
          </p>
        </div>
      </Card>
    );
  }
  if (summary.status === "success") {
    return (
      <Card>
        <div className="px-6 py-8 text-center text-sm text-muted-foreground">
          本期分析已完成，暂无有证据支持的经营判断。
        </div>
      </Card>
    );
  }
  return (
    <Card>
      <EmptyState
        title={
          summary.status === "not_generated" ? "本期月度简报尚未生成" : "本期暂无可展示的月度简报"
        }
        desc={
          summary.status === "not_generated"
            ? "正在准备生成任务；如未自动开始，可手动生成。"
            : "请重新生成后查看经营情况和问题明细。"
        }
        action={
          <Button size="sm" variant="outline" onClick={onGenerate}>
            <Sparkles className="h-3.5 w-3.5" />
            生成月度简报
          </Button>
        }
      />
    </Card>
  );
}

/**
 * 本月经营简报。
 *
 * **它必须排在最前面，而且必须与事实明细分开。** 项目负责人对这一页的原话是
 * 「很多东西，其实从周边获取的东西不多……看完这些我头脑晕晕的，但是信息都没得到」。
 * 根因不是数字错了，是页面通篇都是代码拼的计数串（「在推 18 条主线（8 条只有本部门），
 * 其中 32 项未解决」）—— 那是统计口径，读十遍也拼不出「公司这个月怎么了」。
 * 现在由模型把这些事实读成话放在最前，五块清单退到后面当明细供核对。
 *
 * 三条纪律：
 *   * **没有简报就不假装有**：`narrative` 缺席或正在重算时只显示等待/失败空态，
 *     不铺事实明细，也不拼一段像总结的话。
 *   * **不覆盖事实**：简报里的数字都来自下面那份明细，明细保持原样展示。
 *   * 引用走全站同一套 `[#周报ID]`，保证每句话能点回原文。
 */
function MonthlyNarrativeBlock({
  narrative,
  range,
}: {
  narrative?: OperatingMonthlyNarrative;
  range: PeriodRange;
}) {
  if (!narrative) return null;
  const highlights = narrative.highlights ?? [];

  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 pb-3 pt-5">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-px inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-accent-soft text-accent-deep">
            <Sparkles className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h2 className="text-base font-semibold leading-6 text-foreground">本月经营简报</h2>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {narrative.model ? (
            <span className="text-[13px] text-muted-foreground">{narrative.model}</span>
          ) : null}
          {narrative.generated_at ? (
            <span className="text-[13px] text-muted-foreground">
              生成于 {formatDateTime(narrative.generated_at)}
            </span>
          ) : null}
        </div>
      </div>

      {/* 结论段：这是唯一一段「把话说全」的文字，字号比明细大一档，行距放开 */}
      <div className="px-5">
        <p className="whitespace-pre-line text-[15px] leading-8 text-foreground">
          <InlineCitations
            text={narrative.headline}
            validCitationIds={highlights.flatMap((item) => item.source_report_ids)}
          />
        </p>
      </div>

      {highlights.length ? (
        <ul className="mt-4 grid gap-2 px-5 pb-4 lg:grid-cols-2">
          {highlights.map((item, index) => {
            const meta = HIGHLIGHT_META[item.kind] ?? HIGHLIGHT_META.progress;
            const Icon = meta.icon;
            return (
              <li
                key={`${item.kind}-${item.title}-${index}`}
                className="min-w-0 rounded-control border border-border/70 px-3.5 py-3"
              >
                <div className="flex items-start gap-2">
                  <span
                    className={cn(
                      "mt-px inline-flex shrink-0 items-center gap-1 rounded-pill px-2 py-0.5 text-[11px] font-medium leading-4",
                      CHIP_TONES[meta.tone]
                    )}
                  >
                    <Icon className="h-3 w-3" />
                    {meta.label}
                  </span>
                  <span className="min-w-0 flex-1 text-[15px] font-semibold leading-6 text-foreground">
                    {item.title}
                  </span>
                </div>
                <p className="mt-1.5 text-sm leading-6 text-foreground/80">
                  <InlineCitations
                    text={item.detail}
                    validCitationIds={item.source_report_ids}
                  />
                </p>
              </li>
            );
          })}
        </ul>
      ) : null}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/70 px-5 py-3 text-[13px] leading-5 text-muted-foreground">
        {narrative.coverage_note ? <span>{narrative.coverage_note}</span> : null}
        <span className="inline-flex items-center gap-1">
          <SourceLinks
            ids={highlights.flatMap((item) => item.source_report_ids)}
            range={range}
            limit={3}
          />
        </span>
      </div>
    </section>
  );
}

/* --------------------------------------------------------------------------- */
/* 每月视图                                                                     */

/**
 * 每月视图 = 管理层的五个问题，页面顺序与问题顺序一致：
 *   ① 公司现在主要推进什么   → MainlinesBlock
 *   ② 最近一个月发生了什么变化 → ChangesBlock
 *   ③ 哪些事情卡住了          → StuckBlock
 *   ④ 有哪些平时没发现的异常   → AnomaliesBlock
 *   ⑤ 哪些需要管理层介入       → InterventionsBlock（右侧窄栏，常驻可见）
 *
 * 三条纪律（与后端一致，前端不加戏）：
 *   * 所有数字与判断都来自后端字段，页面**不重算**、不按数组下标配对；
 *   * 事实（计数）与判断（停滞 / 恶化）在视觉上分列，不混成一句话；
 *   * 不可比就如实说不比 —— 需要基线的类型在 `needs_baseline` 且不可比时
 *     根本不会出现，页面负责说明「为什么没有」。
 */

/** 来源周报引用：一条结论要能点回原文核对（全站纪律）。 */
function SourceLinks({
  ids,
  range,
  limit = 3,
}: {
  ids: number[];
  range: PeriodRange;
  limit?: number;
}) {
  if (!ids.length) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
      <span>来源</span>
      {ids.slice(0, limit).map((id) => (
        <Link
          key={id}
          href={`/reports/${id}?start=${range.start}&end=${range.end}`}
          className="num rounded-pill bg-muted px-1.5 py-0.5 font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          #{id}
        </Link>
      ))}
      {ids.length > limit ? <span className="num">等 {ids.length} 篇</span> : null}
    </span>
  );
}

/** 口径脚注。后端算的原因/口径在前端原样展示，避免两处各写一句。 */
export default function OperatingDashboardPage() {
  const searchParams = useSearchParams();
  const initialView = ((): OperatingView => {
    const value = searchParams.get("view");
    // 已移除视图的旧链接（例如 `?view=weekly`）统一落到总览。
    if (value === "monthly") return value;
    // 默认「总览」：它一屏回答「哪里值得关注」，每月视图用于下钻。
    return "overview";
  })();
  const [view, setView] = useState<OperatingView>(initialView);
  // 每月视图的分页（「情况」/「问题与介入」）。放 URL 里，刷新与分享都停在同一页；
  // 切到总览时它不写进 URL，但保留在内存里，切回来还在原来那页。
  const [pane, setPane] = useState<MonthlyPane>(() =>
    searchParams.get("pane") === "issues" ? "issues" : "outlook"
  );
  const [range, setRange] = useState<PeriodRange>(() => {
    const start = searchParams.get("start");
    const end = searchParams.get("end");
    if (isValidIso(start) && isValidIso(end) && start <= end) return { start, end };
    return defaultRange(initialView);
  });
  const [generating, setGenerating] = useState(false);
  // AI 周期总结在本页已不再展示（那块与问答面板一并移除），但它仍有一个作用：
  // 它按事实写出的「建议动作」会由后端并进介入清单（``merge_summary_actions``）。
  // 所以保留生成入口与它的错误提示 —— 失败不静默。
  const [summaryError, setSummaryError] = useState<string | null>(null);
  // 导出失败单独一条通道：summaryError 的重试动作是「再生成一次」，
  // 导出挂在那上面会让重试语义错位。
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  // 一个入口服务两个视图。总览的响应字段与每月视图不同，所以按响应里的 view 分流。
  const overviewView = isOverview(view);
  const dashboard = useAsyncData<OperatingDashboardPayload | OperatingOverviewPayload>(
    () =>
      apiGet("/api/operating-dashboard", {
        view,
        start: range.start,
        end: range.end,
      }),
    [view, range.start, range.end],
    {
      cacheKey: apiCacheKey("/api/operating-dashboard", {
        view,
        start: range.start,
        end: range.end,
      }),
    }
  );

  // **按响应自证的 view 分流，而不是按 state.view 分流**。
  //
  // 切视图时 `useAsyncData` 在 deps 变化的那一帧仍持有上一份数据（它刻意保留旧数据
  // 以避免骨架屏闪烁），此时若按 state.view 去 cast，就会拿总览的 payload 去喂明细
  // 分支 —— 实测这不是理论风险：点视图标签时页面整体崩成
  // `Uncaught TypeError: attention.map is not a function`（总览的 attention 是对象，
  // 明细的 attention 是数组，同名不同形）。
  //
  // 响应里的 `view` 是后端自己写的，不会说谎：只有它与当前 state.view 一致才渲染，
  // 不一致就继续显示骨架屏。代价是切视图时多一帧 loading，换来的是不可能再拿错数据。
  const dashboardView = dashboard.data?.view ?? null;
  const dashboardPeriod = dashboard.data?.period;
  const settled =
    dashboardView === view &&
    dashboardPeriod?.start === range.start &&
    dashboardPeriod?.end === range.end;
  const overviewPayload =
    settled && dashboardView === "overview"
      ? (dashboard.data as OperatingOverviewPayload)
      : null;
  const detailPayload =
    settled && dashboardView === "monthly"
      ? (dashboard.data as OperatingDashboardPayload)
      : null;

  /**
   * 提交一条人工效度判定。
   *
   * 用的是普通 `apiPost` 而不是 `apiPostQuery`：后者会走 GET 缓存键，
   * 而判定是写操作 —— 它必须真的打到后端，不能被任何缓存吞掉。
   * 提交成功后**本地合并**响应里的 `entry`（与回显索引同形，由契约测试守护）：
   * 判定只影响回显索引、不改变视图数据，全量 reload 会把「点一个按钮」
   * 放大成整页重算（overview 单次约 2 秒）。不做乐观更新 —— 权威状态在
   * 服务端，合并的是**服务端确认返回**的那一条，不是本地先猜的值。
   */
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const submitVerdict = useCallback(
    async (
      module: OperatingFeedbackModule,
      itemKey: string,
      verdict: "correct" | "wrong" | "unclear",
      reason?: string
    ) => {
      setFeedbackBusy(true);
      try {
        // 只传业务键：完整指纹（模块 + 周期 + 键）由后端 `build_signal_key` 拼装。
        // 前端若把自己拼好的指纹再当键传过去，后端会再拼一次周期，
        // 得到 `attention|周期|attention|周期|标题` 这种双重指纹，
        // 回显索引永远对不上（实测踩过）。
        const result = await apiPost<{ signal_key: string; entry: OperatingFeedbackEntry }>(
          "/api/operating-dashboard/feedback",
          {
            module,
            key: itemKey,
            verdict,
            reason: reason ?? null,
            start: range.start,
            end: range.end,
          }
        );
        dashboard.mutate((current) => {
          if (!current) return current;
          const feedback: OperatingDashboardPayload["feedback"] = {
            ...(current.feedback ?? {}),
          };
          feedback[result.signal_key] = {
            ...(feedback[result.signal_key] ?? {}),
            [result.entry.reviewer]: result.entry,
          };
          return { ...current, feedback };
        });
      } catch (error) {
        setFeedbackError(error instanceof Error ? error.message : "判定提交失败");
      } finally {
        setFeedbackBusy(false);
      }
    },
    [dashboard, range.start, range.end]
  );
  const [feedbackError, setFeedbackError] = useState<string | null>(null);

  // URL 保存「视图 + 周期 + 每月分页」，刷新、分享、前进后退都停在原处。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams();
    params.set("view", view);
    params.set("start", range.start);
    params.set("end", range.end);
    // 只在每月视图写 pane：总览没有分页，带上它会得到一个无意义的参数。
    if (!isOverview(view)) params.set("pane", pane);
    window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
  }, [view, pane, range.start, range.end]);

  // 只允许当前视图、当前周期的数据驱动总结轮询；切换周期时不能拿旧队列继续刷。
  const summary = settled ? dashboard.data?.summary : undefined;
  const waitingForAnalysis = view === "monthly" && summary?.analysis_ready === false &&
    (summary.analysis_total ?? 0) > 0;
  const departmentCardsRunning = view === "monthly" &&
    (summary?.department_cards?.status === "pending" || summary?.department_cards?.status === "running");
  const summaryRunning = summary?.status === "pending" || summary?.status === "running" || waitingForAnalysis || departmentCardsRunning;
  const [waitClock, setWaitClock] = useState(0);
  useEffect(() => {
    if (!summaryRunning) return;
    setWaitClock(Date.now());
    const timer = window.setInterval(() => setWaitClock(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, [summaryRunning]);
  const waitMinutes = summary ? summaryWaitMinutes(summary, waitClock) : null;

  // 总结生成中才轮询，且走**只读** GET（不会把后台任务打回待处理）；页面隐藏时停。
  // 总览也在轮询范围：重点项目的 AI 结论挂在周度总结上（2026-09-24），总览必须
  // 能看到「生成中 → 已生成」的翻转，否则用户不知道 AI 分析什么时候到。
  //
  // 轮询打的是轻量的 GET /summary（只读状态，不重算整页），而不是整页接口：
  // 4 秒一次的整页 reload 会让后端每过一次缓存 TTL（15s）就全量重算一遍，
  // 而我们关心的只有 status 一个字段。status 翻转（done/failed/…）才做一次
  // 全量 reload，把 narrative 合并结果拿回来。
  // bypassCache 必须开：30s 的 GET 缓存会让轮询在 TTL 内永远读到同一个状态，
  // 「正在生成」也就永远翻不了页。
  const reloadRef = React.useRef(dashboard.reload);
  reloadRef.current = dashboard.reload;
  const mutateRef = React.useRef(dashboard.mutate);
  mutateRef.current = dashboard.mutate;
  useEffect(() => {
    if (!summaryRunning) return;
    let busy = false;
    let cancelled = false;
    // 全量 reload 是 fire-and-forget（useAsyncData.reload 不返回 Promise），
    // 无法知道它是否成功：若那次 reload 失败，旧数据里 summary 仍是
    // pending/running，本 effect 的依赖不变、不会重建 —— 早期版本用一次性
    // finished 标记把轮询永久关掉，结果就是「明明生成完了，顶栏永远转圈」。
    // 现在改为触发 reload 后冷却一段时间：冷却结束若 summaryRunning 仍为真
    // （reload 失败或状态没翻转）就继续检查，直到本 effect 因状态翻转被清理。
    let nextReloadAllowedAt = 0;
    const timer = window.setInterval(async () => {
      if (document.hidden || busy) return;
      busy = true;
      try {
        if (departmentCardsRunning) {
          const cards = await apiGet<{ status: string }>(
            "/api/operating-dashboard/summary",
            { view: "monthly_departments", start: range.start, end: range.end, poll: "1" },
            { bypassCache: true }
          );
          if (cancelled) return;
          if (cards.status !== "pending" && cards.status !== "running" && Date.now() >= nextReloadAllowedAt) {
            nextReloadAllowedAt = Date.now() + 8000;
            reloadRef.current();
          }
        }
        if (summary?.status !== "pending" && summary?.status !== "running" && !waitingForAnalysis) return;
        const state = await apiGet<OperatingSummaryState>(
          "/api/operating-dashboard/summary",
          { view, start: range.start, end: range.end, poll: "1" },
          { bypassCache: true }
        );
        if (cancelled) return;
        if (waitingForAnalysis && state.analysis_ready === false) {
          mutateRef.current((current) => {
            if (!current || current.view !== "monthly" ||
              current.period?.start !== range.start || current.period?.end !== range.end) return current;
            return {
              ...current,
              summary: {
                ...current.summary,
                analysis_ready: false,
                analysis_total: state.analysis_total,
                analysis_completed: state.analysis_completed,
                analysis_failed: state.analysis_failed,
                analysis_processing: state.analysis_processing,
                analysis_waiting: state.analysis_waiting,
              },
            };
          });
          return;
        }
        if (waitingForAnalysis && state.analysis_ready) {
          reloadRef.current();
          return;
        }
        if (state.status === "pending" || state.status === "running") {
          mutateRef.current((current) => {
            if (
              !current || current.view !== view ||
              current.period?.start !== range.start || current.period?.end !== range.end ||
              (current.summary.status !== "pending" && current.summary.status !== "running") ||
              (
                current.summary.status === state.status &&
                current.summary.queue_updated_at === state.queue_updated_at &&
                current.summary.last_error === state.last_error
              )
            ) return current;
            return {
              ...current,
              summary: {
                ...current.summary,
                status: state.status,
                queue_updated_at: state.queue_updated_at,
                last_error: state.last_error,
              },
            };
          });
        }
        if (
          state.status !== "pending" &&
          state.status !== "running" &&
          Date.now() >= nextReloadAllowedAt
        ) {
          nextReloadAllowedAt = Date.now() + 8000;
          reloadRef.current();
        }
      } catch {
        // 单次轮询失败不放大：下一次 tick 会重试，全量数据保持不动。
      } finally {
        busy = false;
      }
    }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [overviewView, summaryRunning, departmentCardsRunning, waitingForAnalysis, summary?.status, view, range.start, range.end]);

  /**
   * 当前周期**从未生成过**总结时，自动排队生成一次（每月视图的月度简报、
   * 总览视图的周度总结都适用 —— 总览的重点项目 AI 结论挂在周度总结上，
   * 不自动排一次的话用户在总览页永远看不到 AI 分析，只能看见规则兜底）。
   *
   * 这是「怎么没看到变化」的直接修复：简报按具体周期生成，用户切到某个没算过的
   * 月份时页面只能显示代码算的事实明细 —— 看着就像改动没生效。自动排队之后，
   * 切月份 = 稍等一下就有那份月份的简报（顶栏有「正在生成」提示）。
   *
   * 四条护栏，缺一个都会出问题：
   * ① **只对当前区间排一次**（`autoQueuedRef`），否则每次轮询都 POST；
   *    key 用区间本身，同一区间永不自排第二次。
   * ② **只处理 `not_generated`，不碰 `stale`**。`stale` 是「事实变了、旧结论基于
   *    上一版事实」—— 本月每周都有新周报入库，若过期就自动重算，一个月会静默调用
   *    模型四五次（每次约 1.7 万 prompt token）。**过期交给人工**：页面隐藏旧结论并显示
   *    过期状态，由人决定什么时候重新生成。
   * ③ **区间内没有周报就不排**（`reports_total === 0`）：没有东西可总结，
   *    排队只会得到一个必然失败的任务。
   * ④ **失败不自动重试**：上游故障时自动重排会反复计费，且掩盖真正的错误原因。
   *
   * 只 POST 排队、不指望 GET 顺带入队 —— 后端那条纪律（GET 必须只读，否则轮询会把
   * 后台任务打回待处理）不能因为前端图省事而破。
   */
  const autoQueuedRef = React.useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!settled || generating) return;
    const state = dashboard.data?.summary;
    const total = dashboard.data?.coverage?.reports_total ?? 0;
    if (!state || total <= 0 || (view === "monthly" && !state.analysis_ready)) return;
    if (state.status !== "not_generated") return;
    const key = `${range.start}|${range.end}`;
    if (autoQueuedRef.current.has(key)) return;
    autoQueuedRef.current.add(key);
    void generateSummary(false);
    // generateSummary 是 useCallback；依赖里带上它会让 effect 每次渲染都重跑。
    // 这里只需要「区间变了就试一次」，用区间做依赖即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overviewView, settled, range.start, range.end, dashboard.data?.summary?.status,
    dashboard.data?.summary?.analysis_ready]);

  function changeView(next: OperatingView) {
    setView(next);
    setRange(defaultRange(next));
    setSummaryError(null);
  }

  /** 导出本周分析报告：fetch 取回内容再触发下载，失败把原因说清楚而不是跳错误页。 */
  const exportReport = useCallback(async () => {
    if (exporting) return;
    setExporting(true);
    setExportError(null);
    try {
      await apiDownload("/api/operating-dashboard/export", {
        start: range.start,
        end: range.end,
      });
    } catch (error) {
      setExportError(
        error instanceof Error ? error.message : "导出失败，请稍后重试。"
      );
    } finally {
      setExporting(false);
    }
  }, [exporting, range.start, range.end]);

  const generateSummary = useCallback(
    async (force = false) => {
      if (generating) return;
      setGenerating(true);
      setSummaryError(null);
      try {
        await apiPostQuery(
          "/api/operating-dashboard/summary",
          {
            view,
            start: range.start,
            end: range.end,
            force: force ? true : null,
          },
          undefined,
          { timeoutMs: 30000 }
        );
        dashboard.reload();
      } catch (error) {
        setSummaryError(
          error instanceof Error ? error.message : "提交生成任务失败，请稍后重试。"
        );
      } finally {
        setGenerating(false);
      }
    },
    [generating, view, range.start, range.end, dashboard]
  );

  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [departmentGenerating, setDepartmentGenerating] = useState(false);
  const generateDepartments = useCallback(async () => {
    if (departmentGenerating) return;
    setDepartmentGenerating(true);
    setDepartmentError(null);
    try {
      await apiPostQuery("/api/operating-dashboard/summary", {
        view: "monthly_departments", start: range.start, end: range.end,
      });
      dashboard.reload();
    } catch (error) {
      setDepartmentError(error instanceof Error ? error.message : "提交部门简报任务失败");
    } finally {
      setDepartmentGenerating(false);
    }
  }, [departmentGenerating, range.start, range.end, dashboard]);

  const createManagementTrack = useCallback(
    async (track: NonNullable<OperatingSignal["track"]>) => {
      const action = await apiPost<{ id: number }>("/api/management-actions", {
        source_type: track.source_type,
        source_id: track.source_id ?? undefined,
        source_key: track.source_key,
        start: range.start,
        end: range.end,
      });
      return action.id;
    },
    [range.start, range.end]
  );


  const pageCopy = VIEW_COPY[view];
  const payload = detailPayload;
  const monthlySummaryCurrent = Boolean(
    payload?.summary.analysis_ready === true &&
    payload.summary.status === "success" &&
    !payload.summary.stale
  );

  return (
    <div className="mx-auto max-w-[1520px] space-y-3 pb-7">
      <header className="flex flex-col gap-2 xl:flex-row xl:items-center xl:justify-between">
        {/* 看板是高频扫描的工具页：标题压到 18px，省出的首屏高度
            让给「今天值得关注」；28px 大标题是给落地页的排场。 */}
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2.5 gap-y-0">
          <h1 className="text-[18px] font-semibold tracking-tight text-foreground">
            {pageCopy.title}
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* 快捷月份：只在每月视图出现（总览看的是「上周」这一档，没有月份可选）。
              固定档位而不是让用户自由填 —— 简报按周期生成，档位固定才能保证
              「点同一个按钮永远是同一份简报」。手填日期仍然保留在后面。 */}
          {!overviewView ? (
            <div
              className="flex items-center gap-0.5 rounded-control border border-input bg-surface p-0.5"
              role="group"
              aria-label="快捷月份"
            >
              {monthPresets().map((preset) => {
                const active = activeMonthPreset(range) === preset.key;
                return (
                  <button
                    key={preset.key}
                    type="button"
                    onClick={() => {
                      setSummaryError(null);
                      setRange(preset.range);
                    }}
                    aria-current={active}
                    className={cn(
                      "rounded-[7px] px-2.5 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      active
                        ? "bg-primary text-white"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    )}
                  >
                    {preset.label}
                  </button>
                );
              })}
            </div>
          ) : null}
          <div className="flex items-center gap-1.5 rounded-control border border-input bg-surface px-2.5 py-1">
            <CalendarDays className="h-4 w-4 shrink-0 text-primary" />
            <Input
              aria-label="开始日期"
              type="date"
              value={range.start}
              onChange={(event) => {
                const value = event.target.value;
                if (isValidIso(value)) setRange((current) => ({ ...current, start: value }));
              }}
              className="h-6 w-[118px] border-0 bg-transparent px-0 text-xs focus-visible:ring-0"
            />
            <span className="text-xs text-muted-foreground">–</span>
            <Input
              aria-label="结束日期"
              type="date"
              value={range.end}
              onChange={(event) => {
                const value = event.target.value;
                if (isValidIso(value)) setRange((current) => ({ ...current, end: value }));
              }}
              className="h-6 w-[118px] border-0 bg-transparent px-0 text-xs focus-visible:ring-0"
            />
          </div>
          {/* 总览的 AI 入口：重点项目的结论由周度总结的 AI 分析写出（挂
              project_notes）。总览没有 CoverageBar 那条状态栏，入口只能放顶栏。
              五种状态各有一句话，别让用户猜「点了之后会发生什么」。 */}
          {overviewView && overviewPayload ? (
            (() => {
              const summaryState = overviewPayload.summary;
              const busy =
                generating ||
                summaryState.status === "pending" ||
                summaryState.status === "running";
              const failed = summaryState.status === "failed";
              const stale = summaryState.stale && !busy;
              const label = busy
                ? summaryState.status === "pending"
                  ? waitMinutes !== null && waitMinutes >= 2
                    ? `周期总结排队约 ${waitMinutes} 分钟`
                    : "周期总结排队中…"
                  : summaryState.status === "running" && waitMinutes !== null && waitMinutes >= 5
                    ? `周期总结生成约 ${waitMinutes} 分钟`
                    : "正在生成周期总结…"
                : failed
                  ? "周期总结生成失败，重试"
                  : stale
                    ? "事实已变化，重新生成周期总结"
                    : summaryState.status === "success"
                      ? "周期总结已生成 · 重新生成"
                      : "生成 AI 周期总结";
              return (
                <div className="inline-flex max-w-full flex-col gap-1">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      // 已生成过结论的「重新生成」必须带 force：否则后端按幂等
                      // 语义直接跳过，按钮文案承诺了重算、实际什么都不会发生。
                      // 未生成/已失败时 force 无副作用（没有可跳过的成功结论）。
                      generateSummary(true)
                    }
                    title="模型会读取本期周报，生成周期总结并补充重点项目结论；篇级结构化分析覆盖率见公司运行概要"
                  >
                    {busy ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Sparkles className="h-3.5 w-3.5" />
                    )}
                    {label}
                  </Button>
                  {busy ? (
                    <AiProgressBar
                      label={
                        summaryState.status === "pending"
                          ? "周期总结等待后台处理"
                          : "周期总结正在生成"
                      }
                      className="mx-1"
                    />
                  ) : null}
                </div>
              );
            })()
          ) : null}
          <Button
            size="sm"
            variant="outline"
            onClick={exportReport}
            disabled={exporting}
            aria-busy={exporting}
            title="导出本周分析报告（CSV），含来源周报 ID"
          >
            {exporting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="h-3.5 w-3.5" />
            )}
            {exporting ? "导出中…" : "导出报告"}
          </Button>
        </div>
      </header>

      <nav className="flex items-end gap-6 border-b border-border" aria-label="经营看板视图">
        {(["overview", "monthly"] as OperatingView[]).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => changeView(key)}
            aria-current={view === key}
            className={cn(
              "relative -mb-px px-0.5 pb-2 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              view === key ? "text-foreground" : "text-muted-foreground hover:text-foreground"
            )}
          >
            {VIEW_LABELS[key]}
            {view === key ? (
              <span className="absolute inset-x-0 bottom-0 h-[2px] rounded-pill bg-primary" />
            ) : null}
          </button>
        ))}
      </nav>

      {/* 总览的生成失败反馈。summaryError 在每月视图由简报卡/明细提示消费，
          总览两个分支都不读它 —— 不补这条横幅的话，点「用 AI 分析重点项目」
          遇到网络/后端错误时按钮恢复原状、页面毫无动静，用户只能靠猜。 */}
      {overviewView && summaryError ? (
        <Card>
          <ErrorState
            message={summaryError}
            onRetry={() => {
              const state = dashboard.data?.summary;
              void generateSummary(!!(state?.stale || state?.status === "failed"));
            }}
          />
        </Card>
      ) : null}

      {dashboard.error && !settled ? (
        <Card>
          <ErrorState message={dashboard.error} onRetry={dashboard.reload} />
        </Card>
      ) : !settled ? (
        /* 数据还没换成当前视图的那一份。这里刻意不按 state.view 抢先渲染：
           上一份数据的字段与当前视图不同形，拿错就是整页崩（见上方 settled 的注释）。 */
        <Card>
          <LoadingBlock rows={10} />
        </Card>
      ) : overviewView && overviewPayload ? (
        <OperatingOverview
          payload={overviewPayload}
          range={range}
          onCreateTrack={createManagementTrack}
          onVerdict={submitVerdict}
          feedbackBusy={feedbackBusy}
          feedbackError={feedbackError}
          onDataChanged={dashboard.reload}
        />
      ) : payload ? (
        <>
          <CoverageBar summary={payload.summary} period={range} />

          {!monthlySummaryCurrent || !payload.narrative ? (
            <MonthlyWaitingState
              summary={payload.summary}
              generating={generating}
              error={monthlySummaryCurrent ? null : summaryError}
              waitMinutes={waitMinutes}
              onGenerate={() => generateSummary(true)}
              range={range}
              onAnalysisChanged={dashboard.reload}
            />
          ) : null}

          {!monthlySummaryCurrent && payload.narrative && (
            <p className="rounded-control border border-warning/30 bg-warning-soft/50 px-4 py-2.5 text-xs text-warning">
              下方公司结论生成于 {payload.narrative.generated_at ? formatDateTime(payload.narrative.generated_at) : "较早前"}，属于历史快照；当前材料变化或分析尚未完成，请按来源核对。各部门卡片按本部门状态独立更新。
            </p>
          )}

          {/* 每月视图的分页切换。两页共用上面的状态栏与周期，只把明细分开 ——
              七块竖排时实测 8023px（7.4 屏），读到「异常」时已经想不起
              「在推进什么」写了什么。分页的分界是「读情况」与「处理问题」。 */}
          <nav
            className="flex items-end gap-6 border-b border-border px-1"
            aria-label="每月视图分页"
          >
            {MONTHLY_PANES.map((item) => {
              const active = pane === item.key;
              return (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => setPane(item.key)}
                  aria-current={active}
                  className={cn(
                    "relative -mb-px min-w-0 border-b-2 px-0.5 pb-2 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    active
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  )}
                >
                  <span className="text-[13px] font-semibold">{item.label}</span>
                </button>
              );
            })}
          </nav>

          {pane === "outlook" ? (
            <>
              {/* 版面：**先读结论，再核事实。**
                  1) 本月经营简报（模型把全部事实读成话，无结论时整块不渲染）；
                  2) 经营明细（在推进什么 / 变了什么 / 各部门怎么样）。

                  顺序不能反：先铺清单正是项目负责人反馈「头晕、什么信息都没得到」
                  的原因 —— 明细是给核对用的，不是用来读懂这个月的。 */}
              {payload.narrative ? (
                <MonthlyNarrativeBlock
                  narrative={payload.narrative}
                  range={range}
                />
              ) : null}

              <MonthlyMainlines payload={payload} range={range} />
              <MonthlyChanges payload={payload} range={range} />
              <MonthlyDepartments payload={payload} range={range} onGenerate={() => void generateDepartments()}
                generating={departmentGenerating} error={departmentError} />
            </>
          ) : (
            <>
              <MonthlyInterventions payload={payload} range={range} />
              <MonthlySharedIssues payload={payload} range={range} />
              <MonthlyStuck payload={payload} range={range} />
              <MonthlyAnomalies payload={payload} range={range} />
            </>
          )}
        </>
      ) : null}

      {dashboard.error && settled ? (
        <p className="rounded-control border border-warning/30 bg-warning-soft/50 px-3 py-2 text-xs text-warning">
          本次刷新未完成，当前显示的是上一次成功加载的数据。{dashboard.error}
        </p>
      ) : null}

    </div>
  );
}
