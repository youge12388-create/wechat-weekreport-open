"use client";

/** 经营看板 · 综合视图（单页总览）。
 *
 * 版面对齐参考图的总览结构：今天值得关注 / 公司运行概要 / 公司精力投向 /
 * 最大运行瓶颈 / 组织摩擦 / 重点项目进展 / 长期趋势。
 *
 * 三条呈现纪律（后端已保证数据，这里是「不把它读错」的部分）：
 * 1. **数字比文字重**：所有计数走 `withNumbers`，一屏里能先看到量再看结论。
 * 2. **环比不可比时不显示数字**：`delta.available=false` 只写文字（如上期无分析数据），
 *    绝不显示「+0」或「—0」那种看起来像结论的东西。
 * 3. **没有数据的周期画「—」不画 0**：项目活跃度里 `has_data=false` 的点，
 *    0 会被读成「这周没人做」。
 */

import React, { useCallback, useMemo, useRef, useState } from "react";
import Link from "next/link";
import type { ECharts } from "echarts/core";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  Building2,
  CalendarDays,
  CircleAlert,
  Compass,
  Layers,
  Repeat2,
  Scale,
  ShieldAlert,
  TrendingUp,
  UsersRound,
} from "lucide-react";
import Chart from "@/components/chart";
import { AnalysisScanButton } from "@/components/analysis-scan-button";
import { FeedbackButtons, signalKeyOf } from "@/components/feedback-buttons";
import {
  Badge,
  Button,
  Card,
  CardHeading,
  EmptyState,
  Modal,
  ProgressBar,
  Select,
  StatusDot,
} from "@/components/ui";
import { chartCategorical, chartColorsForTheme } from "@/lib/chart-theme";
import { cn } from "@/lib/cn";
import { formatDate, formatDateTime, periodLabel } from "@/lib/format";
import { useTheme } from "@/components/theme-provider";
import { useSession } from "@/components/session-provider";
import { Head, withNumbers } from "./head";
import type {
  OperatingBottleneck,
  OperatingConflict,
  OperatingDuplicate,
  OperatingFeedbackEntry,
  OperatingFeedbackModule,
  OperatingFeedbackRate,
  OperatingOverviewPayload,
  OperatingBottleneckSource,
  OperatingProject,
  OperatingSignal,
} from "@/lib/types";

/** Head 与 withNumbers 在本目录的 head.tsx（两个视图共用一份卡头语言）。 */
type Tone = "neutral" | "green" | "amber" | "red" | "blue" | "accent";

/**
 * 从已返回的判定索引里现算某个模块的准确率。
 *
 * **刻意不额外请求 `/feedback/summary`**：这份数据已经在同一个 payload 的
 * `feedback` 字段里（每条判定的 verdict 都在），再发一个请求只是把同一份事实
 * 传两遍。样本不足时 accuracy 为 null，调用方据此显示「还在攒样本」而不是百分比。
 */
function rateFor(
  payload: OperatingOverviewPayload,
  module: OperatingFeedbackModule,
  minSample: number
): OperatingFeedbackRate | null {
  const index = payload.feedback;
  if (!index) return null;
  const counts = { correct: 0, wrong: 0, unclear: 0 };
  for (const [key, byReviewer] of Object.entries(index)) {
    if (!key.startsWith(`${module}|`)) continue;
    for (const entry of Object.values(byReviewer)) {
      if (entry.verdict === "correct") counts.correct += 1;
      else if (entry.verdict === "wrong") counts.wrong += 1;
      else counts.unclear += 1;
    }
  }
  // 「说不清」不进分母：它是「证据不足以判断」，既不是对也不是错。
  const decided = counts.correct + counts.wrong;
  const total = decided + counts.unclear;
  if (total === 0) return null;
  return {
    ...counts,
    decided,
    total,
    accuracy: decided ? Math.round((counts.correct / decided) * 1000) / 10 : null,
    enough_sample: decided >= minSample,
    min_sample: minSample,
  };
}

/** 判定样本门槛，必须与后端 `MIN_SAMPLE_FOR_RATE` 一致（契约测试守护）。 */
const MIN_SAMPLE_FOR_RATE = 5;

/**
 * 取某条结论的已有判定。
 *
 * 指纹由「模块 + 周期 + 业务键」拼成，与后端 `build_signal_key` 一致；
 * 这里不复用后端的字符串，而是各自拼一次 —— 前后端本就分属两个进程，
 * 让前端依赖后端代码反而会糊掉接口边界。一致性由契约测试守护。
 */
function feedbackFor(
  payload: OperatingOverviewPayload,
  module: OperatingFeedbackModule,
  start: string,
  end: string,
  key: string,
  reviewer: string | null
): OperatingFeedbackEntry | null {
  const index = payload.feedback?.[signalKeyOf(module, start, end, key)];
  if (!index) return null;
  if (reviewer === null) return null;
  return index[reviewer] ?? null;
}

interface Range {
  start: string;
  end: string;
}

function reportHref(reportId: number, range: Range): string {
  return `/reports/${reportId}?start=${range.start}&end=${range.end}`;
}

/** 来源周报链接组：每个结论都要能回到原文。 */
function SourceLinks({
  ids,
  range,
  max = 6,
  label = "来源",
}: {
  ids: number[];
  range: Range;
  max?: number;
  label?: string;
}) {
  if (!ids?.length) {
    return <span className="text-xs text-muted-foreground">暂无来源</span>;
  }
  const shown = ids.slice(0, max);
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>{label}</span>
      {shown.map((id) => (
        <Link
          key={id}
          href={reportHref(id, range)}
          className="num rounded-pill bg-muted/70 px-1.5 py-0.5 font-medium text-foreground/75 transition-colors hover:bg-accent-soft hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          #{id}
        </Link>
      ))}
      {ids.length > shown.length ? <span className="num">+{ids.length - shown.length}</span> : null}
    </span>
  );
}

function evidenceDateLabel(
  source: Pick<OperatingBottleneckSource, "period_start" | "period_end" | "occurred_on">
): string {
  if (source.period_start && source.period_end) {
    return `周报周期 ${formatDate(source.period_start)} ~ ${formatDate(source.period_end)}`;
  }
  const date = source.occurred_on || source.period_end || source.period_start;
  return date ? `发生日期 ${formatDate(date)}` : "时间未知";
}

/** 数据依据折叠区：四类弹层（信号/瓶颈/重复/冲突/项目）通用。
 *
 * 弹层默认只留结论（负责人 2026-09-30 确认的验收标准）：指标行、来源周报、
 * 证据事项、口径说明这类「数据依据」全部收进这里，默认不可见，点开才展开；
 * 展开后数据完整可见，一条不丢。`summary` 是折叠条上的概要，让不看数据的
 * 人也能扫一眼范围。
 */
function EvidenceDisclosure({
  summary,
  children,
}: {
  summary?: string;
  children: React.ReactNode;
}) {
  return (
    <details className="group rounded-control bg-muted/30 open:bg-muted/40">
      <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
        数据依据（点开核对）
        {summary ? <span className="font-normal opacity-80">{summary}</span> : null}
        <ArrowRight className="ml-auto h-3 w-3 shrink-0 transition-transform group-open:rotate-90" />
      </summary>
      <div className="space-y-4 px-3 pb-3 pt-1">{children}</div>
    </details>
  );
}

/** 依据事项列表：数据依据折叠区里通用的一段「原文核对」，只在弹层内出现。 */
function SourceEvidenceList({
  sources,
  range,
}: {
  sources: OperatingBottleneckSource[];
  range: Range;
}) {
  if (!sources?.length) return null;
  return (
    <div className="space-y-2">
      <p className="text-xs font-medium text-muted-foreground">依据事项</p>
      {sources.map((source) => (
        <div key={source.id ?? source.report_id} className="rounded-control bg-muted/40 p-3">
          <p className="text-[13px] font-medium">{source.title}</p>
          {source.description ? (
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              {source.description}
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>{evidenceDateLabel(source)}</span>
            <span>{source.department_name || "部门未知"}</span>
            <span>{source.employee_name || "提交人未知"}</span>
            {source.project ? <span>{source.project}</span> : null}
            <Link
              href={reportHref(source.report_id, range)}
              className="num font-medium text-accent hover:underline"
            >
              查看 #{source.report_id}
            </Link>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * 卡脚口径行：数据来源、跳过说明这类「注释性内容」。
 * 比正文再淡一档（11px + 更低对比），明确它是注释不是内容 ——
 * 此前各卡脚注与正文同为 text-xs 等权，整屏读下来注释占了三成视觉重量。
 */
function CardFootnote({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone?: "warning";
}) {
  return (
    <p
      className={cn(
        "text-[11px] leading-5",
        tone === "warning" ? "text-warning/80" : "text-muted-foreground/75"
      )}
    >
      {children}
    </p>
  );
}

/** 总览中的无数据说明保持紧凑，避免通用空状态把整张看板撑成大片留白。 */
function CompactEmptyState({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="mx-4 mb-4 mt-1 rounded-control bg-muted/35 px-3 py-2.5 text-center">
      <p className="text-xs font-medium text-muted-foreground">{title}</p>
      <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground/80">{desc}</p>
    </div>
  );
}

/**
 * 弹窗主指标行：**一个主数字做视觉锚点**，次级指标缩小陪衬。
 *
 * 旧版三格等权 `text-base`，扫一眼抓不到「这个结论为什么排第一」；
 * 现在主指标 24px（tone=destructive 时用警示色），次级 15px。
 */
function ModalMetricRow({
  primary,
  secondary,
}: {
  primary: { label: string; value: React.ReactNode; unit?: string; tone?: "destructive" };
  secondary: Array<{ label: string; value: React.ReactNode }>;
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-control bg-muted/40 px-4 py-3">
      <div>
        <p className="text-xs text-muted-foreground">{primary.label}</p>
        <p className="num mt-0.5 flex items-baseline gap-1 leading-none">
          <span
            className={cn(
              "text-2xl font-semibold tracking-tight",
              primary.tone === "destructive" && "text-destructive"
            )}
          >
            {primary.value}
          </span>
          {primary.unit ? (
            <span className="text-xs text-muted-foreground">{primary.unit}</span>
          ) : null}
        </p>
      </div>
      <div className="hidden h-9 w-px bg-border sm:block" />
      {secondary.map((item) => (
        <div key={item.label}>
          <p className="text-xs text-muted-foreground">{item.label}</p>
          <p className="num mt-0.5 text-[15px] font-semibold leading-5">{item.value}</p>
        </div>
      ))}
    </div>
  );
}

/**
 * 口径/免责说明：**默认折叠**。这些内容是诚实的、必要的，但平铺在正文流里
 * 等于把核对成本转嫁给每个读者 —— 收进 details，需要核对口径的人自己展开。
 */
function MethodNote({
  children,
  label = "口径与判定说明",
}: {
  children: React.ReactNode;
  label?: string;
}) {
  return (
    <details className="group rounded-control bg-muted/40 px-3 py-2">
      <summary className="cursor-pointer select-none text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
        {label}
      </summary>
      <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{children}</p>
    </details>
  );
}

/**
 * 弹窗固底判定区：左侧「是否准确？」+ 判定按钮，右侧主操作（可空）。
 * Modal.footer 默认右对齐，这里用 w-full 撑开左右分布 —— 判定是这套看板
 * 最核心的人工机制，固底后它是明确的动作而不是漂浮的小字。
 */
function ModalVerdictFooter({
  question,
  buttons,
  action,
}: {
  question: string;
  buttons: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex w-full flex-wrap items-center justify-between gap-x-3 gap-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">{question}</span>
        {buttons}
      </div>
      {action ?? null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 1. 今天值得关注                                                              */
/* -------------------------------------------------------------------------- */

function AttentionSection({
  payload,
  range,
  onOpen,
  onVerdict,
  feedbackBusy,
  feedbackRate,
}: {
  payload: OperatingOverviewPayload;
  range: Range;
  onOpen: (signal: OperatingSignal) => void;
  onVerdict: (
    module: OperatingFeedbackModule,
    itemKey: string,
    verdict: "correct" | "wrong" | "unclear",
    reason?: string
  ) => void;
  feedbackBusy?: boolean;
  /** 本周期的人工判定准确率。样本不足时为 null，不显示百分比。 */
  feedbackRate?: OperatingFeedbackRate | null;
}) {
  const items = payload.attention.items;
  const { start, end } = payload.period;
  const [showAll, setShowAll] = useState(false);
  return (
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={ShieldAlert}
        tone="red"
        title="今天值得你关注的 5 件事"
        // 准确率放在卡头：它是「这张卡该信几分」的前提信息，不能藏在别处。
        // 样本不足时**不给百分比**（一条判对的结论算出 100% 会误导人信任它），
        // 改为显示已判定条数，如实告诉人「还在攒样本」。
        action={
          feedbackRate && feedbackRate.decided > 0 ? (
            feedbackRate.enough_sample && feedbackRate.accuracy !== null ? (
              <Badge tone={feedbackRate.accuracy >= 90 ? "green" : "amber"}>
                人工核对准确率 {feedbackRate.accuracy}%
              </Badge>
            ) : (
              <Badge tone="neutral">
                已判定 {feedbackRate.decided} 条 · 满 {feedbackRate.min_sample} 条出准确率
              </Badge>
            )
          ) : null
        }
      />
      {items.length ? (
        <ol className="space-y-0.5 px-3 pb-3">
          {items.map((item, index) => (
            <li
              key={`${item.signal_type}-${item.title}`}
              className={cn(index >= 3 && !showAll && "hidden sm:block")}
            >
              <button
                type="button"
                onClick={() => onOpen(item)}
                className="group flex w-full items-start gap-2 rounded-control px-1.5 py-1.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span
                  className={cn(
                    "num mt-0.5 inline-flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                    index === 0
                      ? "bg-destructive text-white"
                      : item.tone === "red"
                        ? "bg-destructive-soft text-destructive"
                        : item.tone === "amber"
                          ? "bg-warning-soft text-warning"
                          : "bg-muted text-muted-foreground"
                  )}
                >
                  {index + 1}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[13px] font-medium leading-5">
                    {withNumbers(item.title)}
                  </span>
                  <span className="block text-[12px] leading-5 text-muted-foreground">
                    {withNumbers(item.conclusion)}
                  </span>
                </span>
                <span className="mt-0.5 flex shrink-0 flex-col items-end gap-1">
                  <Badge tone={item.tone as Tone}>{item.label}</Badge>
                  <span className="text-[12px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
                    查看详情
                  </span>
                </span>
              </button>
            </li>
          ))}
          {items.length > 3 ? (
            <li className="px-1.5 pt-1 sm:hidden">
              <button
                type="button"
                onClick={() => setShowAll((value) => !value)}
                className="w-full rounded-control py-1.5 text-center text-xs font-medium text-primary transition-colors hover:bg-primary-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {showAll ? "收起" : `查看其余 ${items.length - 3} 条`}
              </button>
            </li>
          ) : null}
        </ol>
      ) : (
        <CompactEmptyState
          title="本期没有需要特别关注的问题"
          desc="规则没有从本期周报里算出值得单独提示的经营信号。这不代表没有问题，只代表本期证据不足以判定。"
        />
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 2. 公司运行概要                                                              */
/* -------------------------------------------------------------------------- */

function OverviewSection({
  payload,
  range,
  onOpenSignal,
  onDataChanged,
}: {
  payload: OperatingOverviewPayload;
  range: Range;
  onOpenSignal: (signal: OperatingSignal) => void;
  onDataChanged?: () => void;
}) {
  const colors = chartColorsForTheme(useTheme().theme);
  const points = payload.activity.points;
  const [metricDetail, setMetricDetail] = useState<"workstreams" | "signals" | null>(null);

  const option = useMemo(
    () => ({
      animationDuration: 2600,
      animationDurationUpdate: 450,
      animationEasing: "cubicOut" as const,
      animationEasingUpdate: "cubicOut" as const,
      grid: { left: 4, right: 16, top: 20, bottom: 0, containLabel: true },
      tooltip: { trigger: "axis" as const },
      // 双线恢复（项目负责人点名要回）：「已分析」线直观表达每周期实际进入
      // AI 分析的量，缺口一眼可见，Badge 只给总数。图例还给主题位（top:20
      // 给图例留高度）；两线不用面积填充，重叠段才不会糊。
      legend: {},
      xAxis: {
        type: "category" as const,
        data: points.map((point) => point.label),
        boundaryGap: false,
      },
      yAxis: { type: "value" as const, minInterval: 1 },
      series: [
        {
          name: "周报篇数",
          type: "line" as const,
          smooth: true,
          showSymbol: true,
          symbolSize: 6,
          animationDuration: 2600,
          animationDurationUpdate: 450,
          animationEasing: "linear" as const,
          animationEasingUpdate: "cubicOut" as const,
          animationDelay: 0,
          data: points.map((point) => point.reports),
          itemStyle: { color: colors.accent, borderColor: "#fff", borderWidth: 2 },
          lineStyle: { color: colors.accent, width: 2.4 },
        },
        {
          name: "已分析",
          type: "line" as const,
          smooth: true,
          showSymbol: true,
          symbolSize: 6,
          animationDuration: 2600,
          animationDurationUpdate: 450,
          animationEasing: "linear" as const,
          animationEasingUpdate: "cubicOut" as const,
          animationDelay: 240,
          data: points.map((point) => point.analyzed),
          itemStyle: { color: colors.primary, borderColor: "#fff", borderWidth: 2 },
          lineStyle: { color: colors.primary, width: 2 },
        },
      ],
    }),
    [points, colors.accent, colors.primary]
  );

  return (
    <>
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={Activity}
        tone="accent"
        title="公司运行概要"
        action={
          <div className="flex flex-col items-end gap-1">
            <Badge
              tone={
                payload.coverage.reports_total === 0
                  ? "neutral"
                  : payload.coverage.partial
                    ? "amber"
                    : "green"
              }
            >
              {payload.coverage.reports_total > 0
                ? `已分析 ${payload.coverage.analyzed_reports}/${payload.coverage.reports_total} 篇`
                : "本期无周报"}
            </Badge>
            {payload.coverage.partial ? (
              <AnalysisScanButton
                start={payload.period.start}
                end={payload.period.end}
                onChanged={onDataChanged}
              />
            ) : null}
          </div>
        }
      />

      <div className="grid grid-cols-2 gap-x-4 gap-y-3 px-4 sm:grid-cols-4">
        {payload.metrics.map((metric) => {
          const detailKind =
            metric.key === "themes"
              ? "workstreams"
              : metric.key === "signals"
                ? "signals"
                : null;
          const content = (
            <>
              <p className="text-xs text-muted-foreground">{metric.label}</p>
              <p className="mt-0.5 flex items-baseline gap-1">
                <span className="num text-xl font-semibold leading-none tracking-tight">
                  {metric.value}
                </span>
                <span className="text-xs text-muted-foreground">{metric.unit}</span>
              </p>
            </>
          );

          return detailKind ? (
            <button
              key={metric.key}
              type="button"
              onClick={() => setMetricDetail(detailKind)}
              aria-label={`${metric.label} ${metric.value}${metric.unit}，查看明细`}
              className="group -m-1.5 w-full min-w-0 rounded-control p-1.5 text-left transition-colors hover:bg-muted/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {content}
              <span className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-primary">
                查看明细 <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
              </span>
            </button>
          ) : (
            <div key={metric.key} className="min-w-0">
              {content}
            </div>
          );
        })}
      </div>

      {/* 图表区 flex-1 撑满卡内剩余高度（网格会把两张卡拉到等高，本卡内容
          偏矮时多出的是这里），脚注钉在卡底——不留悬空的卡底空白。 */}
      <div className="mt-3 flex flex-1 flex-col border-t border-border/70 px-4 pt-2.5">
        <p className="text-xs text-muted-foreground">
          整体工作活跃度（近 {points.length} 周，按周报篇数）
        </p>
        {points.length ? (
          <Chart option={option} height={168} respectReducedMotion={false} />
        ) : (
          <p className="py-6 text-center text-xs text-muted-foreground">暂无活跃度数据</p>
        )}
        <div className="mt-auto pt-2">
          <CardFootnote>{payload.activity.basis}</CardFootnote>
        </div>
      </div>
    </Card>
    <Modal
      open={metricDetail !== null}
      title={
        metricDetail === "workstreams"
          ? `工作主线 · ${payload.workstreams.items?.length ?? 0} 条`
          : `需要关注的问题 · ${payload.attention.total} 项`
      }
      onClose={() => setMetricDetail(null)}
      size="lg"
    >
      {metricDetail === "workstreams" ? (
        payload.workstreams.items?.length ? (
          <div className="max-h-[62vh] divide-y divide-border/70 overflow-y-auto pr-1">
            {payload.workstreams.items.map((item) => (
              <div key={item.name} className="space-y-2 py-3 first:pt-0">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <p className="text-[13px] font-medium">{item.name}</p>
                  <span className="num text-xs text-muted-foreground">{item.item_count} 条事项</span>
                </div>
                <SourceLinks
                  ids={item.source_report_ids}
                  range={range}
                  max={12}
                  label="来源周报"
                />
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="本期没有可展示的工作主线" desc="工作主线按至少 2 条事项的业务对象统计。" />
        )
      ) : metricDetail === "signals" ? (
        payload.attention.all_items?.length ? (
          <div className="max-h-[62vh] space-y-2 overflow-y-auto pr-1">
            {payload.attention.all_items.map((item, index) => (
              <div key={`${item.signal_type}-${item.title}`} className="rounded-control bg-muted/35 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setMetricDetail(null);
                      onOpenSignal(item);
                    }}
                    className="text-left text-[13px] font-medium text-foreground hover:text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {index + 1}. {withNumbers(item.title)}
                  </button>
                  <Badge tone={item.tone as Tone}>{item.label}</Badge>
                </div>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  {withNumbers(item.conclusion)}
                </p>
                <div className="mt-2">
                  <SourceLinks ids={item.source_report_ids} range={range} max={12} />
                </div>
              </div>
            ))}
            {payload.attention.all_items.length < payload.attention.total ? (
              <p className="text-xs text-muted-foreground">
                当前显示前 {payload.attention.all_items.length} 项；总数 {payload.attention.total} 项。
              </p>
            ) : null}
          </div>
        ) : (
          <EmptyState title="本期没有需要特别关注的问题" desc="规则没有从本期周报中算出值得单独提示的经营信号。" />
        )
      ) : null}
    </Modal>
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* 3. 公司精力投向                                                              */
/* -------------------------------------------------------------------------- */

function FocusSection({ payload }: { payload: OperatingOverviewPayload }) {
  const colors = chartColorsForTheme(useTheme().theme);
  const { categories, total_entries, basis } = payload.focus;
  const palette = useMemo(
    () => chartCategorical(colors, categories.length),
    [colors, categories.length]
  );
  const chartRef = useRef<ECharts | null>(null);
  /** 当前高亮的类别下标；null = 没有高亮，环心回到总人次。 */
  const [active, setActive] = useState<number | null>(null);

  /* 图例与扇区双向联动：八类颜色再克制，也还是「读图例找颜色」这一步，
     联动把这一步消掉 —— 鼠标落在哪一行，哪一片就亮起来、其余压暗。 */
  const focusSlice = useCallback((index: number | null) => {
    setActive(index);
    const chart = chartRef.current;
    if (!chart) return;
    chart.dispatchAction(
      index === null
        ? { type: "downplay", seriesIndex: 0 }
        : { type: "highlight", seriesIndex: 0, dataIndex: index }
    );
  }, []);

  const option = useMemo(
    () => ({
      // 关掉 ECharts 自带的图例：右侧已经有一份 HTML 图例（带占比），
      // 两边同时渲染会把同一批类别名写两遍。
      legend: { show: false },
      tooltip: {
        trigger: "item" as const,
        formatter: (params: unknown) => {
          // ECharts 的回调入参是联合类型，只有饼图这一路会带 name/value/percent。
          // 在这里收窄一次，避免把 any 扩散到整个 option。
          const item = params as { name?: string; value?: number; percent?: number };
          return `${item.name ?? ""}<br/>${item.value ?? 0} 人次 · ${item.percent ?? 0}%`;
        },
      },
      series: [
        {
          type: "pie" as const,
          // 动效只留一次短促的展开：原来的 2.4 秒扫盘 + 逐个延迟，翻到这一屏时
          // 数字还在转，读起来是「特效」而不是「读数」。
          animationType: "expansion" as const,
          animationDuration: 900,
          animationEasing: "cubicOut" as const,
          animationDelay: (index: number) => index * 45,
          // 环带厚度：中空区要放环心两行字，太薄整块会显得轻飘、也太小
          radius: ["54%", "80%"],
          center: ["50%", "50%"],
          /* 高亮时本片微胀、其余压暗：比只描个边更有指向性 */
          emphasis: { focus: "self" as const, scale: true, scaleSize: 5 },
          blur: { itemStyle: { opacity: 0.28 } },
          /* 环心文字改由 DOM 渲染（见下方绝对定位层）：canvas 里的字跟着
             画布缩放会发虚，且主题色得在 JS 里再写一份 hex。 */
          label: { show: false },
          labelLine: { show: false },
          /* 相邻两档色相接近时靠卡片底色切出细缝；端部微圆角与全站圆角语言一致 */
          itemStyle: { borderColor: colors.surface, borderWidth: 2, borderRadius: 3 },
          data: categories.map((category, index) => ({
            name: category.label,
            value: category.people_entries,
            itemStyle: { color: palette[index] },
          })),
        },
      ],
    }),
    [categories, colors, palette]
  );

  // 无篇级结构化分析时收起此模块；分析覆盖率在公司运行概要中统一展示。
  if (!categories.length) return null;

  return (
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={Compass}
        tone="accent"
        title="公司精力投向"
        /* 不能写「过去一周」：页头是自由起止日期，选本月时那句话就是错的 */
        desc="本周期精力投到了哪些方向"
        action={
          /* 口径不再单独占一行小字（2026-09-24 项目负责人要求）：说明收进这枚
             标签的悬停提示，版面只留结论。文字仍由后端给定，前端不另写一套。 */
          <span className="cursor-help" title={basis}>
            <Badge tone="neutral">人员参与度</Badge>
          </span>
        }
      />
      {categories.length ? (
        /* 移动端上下堆叠、桌面并排：环形图在 390px 里并排会被压成一条窄带，
           图例也会被挤到看不见。 */
        <div className="grid flex-1 grid-cols-[minmax(0,1fr)] items-center gap-2 px-3 pb-3 sm:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div
            className="relative min-w-0"
            /* 鼠标离开整块图（包括离开扇区与环心）时取消高亮。挂在容器上而不是
               扇区上：扇区之间的白缝也是这块的子元素，逐片绑定会在缝里闪。 */
            onMouseLeave={() => focusSlice(null)}
          >
            <Chart
              option={option}
              height={196}
              respectReducedMotion={false}
              onReady={(chart) => {
                chartRef.current = chart;
                /* 反向联动：鼠标直接落在扇区上时，右侧图例那一行同步高亮 */
                chart.on("mouseover", (params) => {
                  const dataIndex = (params as { dataIndex?: number }).dataIndex;
                  if (typeof dataIndex === "number") setActive(dataIndex);
                });
                chart.on("mouseout", () => setActive(null));
              }}
            />
            {/* 环心文字走 DOM 而不是 ECharts 的 label：canvas 放大会发虚，
                且字号/颜色可以直接用全站 token，不必在 JS 里再写一份 hex。 */}
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span className="num text-[26px] font-semibold leading-none tracking-tight text-foreground">
                {total_entries}
              </span>
              <span className="mt-1 text-xs text-muted-foreground">参与人次</span>
            </div>
          </div>
          {/* 每行只给一个数（占比）：环心已经给出总人次，再并列一列绝对人数
              等于把同一个数写两遍，八个类别就是十六个数字。人次与提及篇数进
              悬停提示 —— 行内只留一个提示，两个 title 会互相盖住。 */}
          <ul className="min-w-0 space-y-1">
            {categories.map((category, index) => (
              <li
                key={category.label}
                className={cn(
                  "-mx-1 flex items-center gap-2 rounded-control px-1 py-1 text-xs transition-colors",
                  active === index ? "bg-muted/60" : "bg-transparent",
                  active !== null && active !== index && "opacity-45"
                )}
                title={`${category.label} · ${category.people_entries} 人次参与 · ${category.report_count} 篇周报提及`}
                onMouseEnter={() => focusSlice(index)}
                onMouseLeave={() => focusSlice(null)}
              >
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: palette[index] }}
                />
                <span className="min-w-0 flex-1 truncate">{category.label}</span>
                <span className="num shrink-0 font-semibold">{category.share}%</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 4. 最大运行瓶颈                                                              */
/* -------------------------------------------------------------------------- */

function BottleneckSection({
  payload,
  onOpen,
}: {
  payload: OperatingOverviewPayload;
  onOpen: (item: OperatingBottleneck) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const items = payload.bottlenecks.items;
  const visible = showAll ? items : items.slice(0, 4);
  return (
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={AlertTriangle}
        tone="red"
        title="影响面最大的瓶颈"
        desc="按被依赖工作数聚合，适合核对根因"
      />
      {items.length ? (
        <>
          <ol className="space-y-0.5 px-3 pb-2.5">
            {visible.map((item, index) => (
              <li key={item.target}>
                <button
                  type="button"
                  onClick={() => onOpen(item)}
                  className="group flex w-full items-start gap-2 rounded-control px-1.5 py-1.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span
                    className={cn(
                      "num mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                      (item.rank ?? index + 1) === 1
                        ? "bg-destructive text-white"
                        : "bg-muted text-muted-foreground"
                    )}
                  >
                    {item.rank ?? index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="block truncate text-[13px] font-medium" title={item.target}>
                        {item.target}
                      </span>
                      {item.conclusion_source === "ai" ? (
                        <span className="shrink-0 rounded px-1 py-px text-[11px] font-medium bg-accent/10 text-accent">
                          AI
                        </span>
                      ) : null}
                    </span>
                    {/* 正文由模型逐条分析（谁被挡、在等什么、先核对谁）。
                        计数依据行不回到卡上 —— 数据依据按负责人要求折叠隐藏。 */}
                    <span
                      className={cn(
                        "mt-0.5 line-clamp-2 text-[12px] leading-5",
                        item.conclusion ? "text-foreground/85" : "text-muted-foreground"
                      )}
                      title={item.conclusion}
                    >
                      {withNumbers(
                        item.conclusion ??
                          `影响 ${item.workstream_count} 项工作 · ${item.department_count} 个部门`
                      )}
                    </span>
                  </span>
                  <span className="mt-0.5 shrink-0">
                    <Badge tone={item.severity_tone as Tone}>{item.severity}</Badge>
                  </span>
                </button>
              </li>
            ))}
          </ol>
          {/* 「另有 N 条依赖未归属」的口径说明移进瓶颈弹窗的归一依据区：
              它是审计口径，不是给管理层的结论，挂在卡片上只会占一行噪音。 */}
          {items.length > 4 ? (
            <div className="mt-auto border-t border-border/60 px-4 py-2">
              <button
                type="button"
                onClick={() => setShowAll((value) => !value)}
                className="inline-flex items-center gap-1 text-[12px] font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {showAll ? "收起" : `查看全部 ${items.length} 条瓶颈`}
                <ArrowRight className={cn("h-3.5 w-3.5 transition-transform", showAll && "rotate-90")} />
              </button>
            </div>
          ) : null}
        </>
      ) : (
        <CompactEmptyState
          title="本期没有重复出现的瓶颈"
          desc="瓶颈要求至少 2 篇周报提到、或已挡住 2 项以上工作，单次提及不计入。"
        />
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 5. 组织摩擦                                                                  */
/* -------------------------------------------------------------------------- */

function FrictionSection({
  payload,
  onOpenDuplicate,
  onOpenConflict,
}: {
  payload: OperatingOverviewPayload;
  onOpenDuplicate: (item: OperatingDuplicate) => void;
  onOpenConflict: (item: OperatingConflict) => void;
}) {
  const { counts, duplicates, conflicts } = payload.frictions;
  // 有 AI 结论时行副标题换成模型写的分析（待核对措辞由提示词保证）；
  // 计数为 0 时写「本期没有发现」，不渲染暗示存在问题的模板句。
  // 典型案例块保持删除：它就是 duplicates[0]，正文与来源收进该条的弹层。
  const rows: Array<{
    key: string;
    count: number;
    label: string;
    desc: string;
    tone: Tone;
    onClick?: () => void;
  }> = [
    {
      key: "duplicate",
      count: counts.duplicate_effort,
      label: "个疑似重复推进",
      desc:
        duplicates[0]?.conclusion ??
        (counts.duplicate_effort > 0
          ? "多个部门在做同一件事，可能存在资源浪费"
          : "本期没有发现"),
      tone: "amber",
      onClick: duplicates[0] ? () => onOpenDuplicate(duplicates[0]) : undefined,
    },
    {
      key: "conflict",
      count: counts.status_conflict,
      label: "个状态不一致",
      desc:
        conflicts[0]?.conclusion ??
        (counts.status_conflict > 0
          ? "同一件事被不同的人写成了不同状态"
          : "本期没有发现"),
      tone: "amber",
      onClick: conflicts[0] ? () => onOpenConflict(conflicts[0]) : undefined,
    },
  ];

  return (
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={Scale}
        tone="amber"
        title="组织摩擦"
        desc="哪里存在重复、冲突或信息不一致"
      />
      <ul className="space-y-0.5 px-3 pb-2.5">
        {rows.map((row, index) => {
          const content = (
            <>
              <span
                className={cn(
                  "num mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-semibold",
                  row.count === 0
                    ? "bg-muted text-muted-foreground"
                    : row.tone === "amber"
                      ? "bg-warning-soft text-warning"
                      : "bg-muted text-muted-foreground"
                )}
              >
                {row.count}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <span className="block text-[13px] font-medium leading-6">{row.label}</span>
                  {row.key === "duplicate" && duplicates[0]?.conclusion_source === "ai" ? (
                    <span className="shrink-0 rounded px-1 py-px text-[11px] font-medium bg-accent/10 text-accent">
                      AI
                    </span>
                  ) : null}
                  {row.key === "conflict" && conflicts[0]?.conclusion_source === "ai" ? (
                    <span className="shrink-0 rounded px-1 py-px text-[11px] font-medium bg-accent/10 text-accent">
                      AI
                    </span>
                  ) : null}
                </span>
                <span
                  className="mt-0.5 line-clamp-2 text-xs leading-relaxed text-muted-foreground"
                  title={row.desc}
                >
                  {withNumbers(row.desc)}
                </span>
              </span>
            </>
          );
          return (
            <li key={row.key}>
              {row.onClick && row.count > 0 ? (
                <button
                  type="button"
                  onClick={row.onClick}
                  className="group flex w-full items-start gap-2 rounded-control px-1.5 py-1.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {content}
                  <ArrowUpRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                </button>
              ) : (
                <div className="flex items-start gap-2 px-1.5 py-1.5">{content}</div>
              )}
            </li>
          );
        })}
      </ul>

      {/* 典型案例块已删：它展示的就是 duplicates[0]，正文与来源都收进该条的
          弹层（点击行打开）。卡片数据一律折叠，不在卡上重复弹层内容。 */}
      {counts.duplicate_effort === 0 && counts.status_conflict === 0 ? (
        <p className="px-5 pb-4 text-xs text-muted-foreground">
          本期没有检测到跨部门的重复推进或状态冲突。
        </p>
      ) : null}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 6. 重点项目进展                                                              */
/* -------------------------------------------------------------------------- */

function ProjectSection({
  payload,
  range,
  onOpen,
}: {
  payload: OperatingOverviewPayload;
  range: Range;
  onOpen: (project: OperatingProject) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const items = payload.projects.items;
  const visible = showAll ? items : items.slice(0, 4);
  const colors = chartColorsForTheme(useTheme().theme);

  // 无可追踪事项时收起模块，避免用整张空卡重复表达顶部的分析覆盖率。
  if (!items.length) return null;

  return (
    <Card className="flex min-w-0 flex-col">
      <Head
        icon={Layers}
        tone="primary"
        title="重点项目进展"
        desc="关键事项到底有没有往前走 · 生成 AI 总结后每条结论由模型分析"
      />
      {items.length ? (
        <>
          <ul className="space-y-0.5 px-3 pb-1.5">
            {visible.map((project) => (
              <li key={project.name}>
                <button
                  type="button"
                  onClick={() => onOpen(project)}
                  className="flex w-full items-center gap-2 rounded-control px-1.5 py-1.5 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="block truncate text-[13px] font-medium" title={project.name}>
                        {project.name}
                      </span>
                      {project.conclusion_source === "ai" ? (
                        <span className="shrink-0 rounded px-1 py-px text-[11px] font-medium bg-accent/10 text-accent">
                          AI
                        </span>
                      ) : null}
                    </span>
                    {/* 副标题用后端拼好的结论（说人话）；卡上不渲染计数依据，
                        数据依据收进弹层折叠区。 */}
                    <span
                      className="mt-0.5 line-clamp-2 text-xs leading-5 text-muted-foreground"
                      title={project.conclusion}
                    >
                      {withNumbers(
                        project.conclusion ?? `${project.report_count} 篇周报提到`
                      )}
                    </span>
                  </span>
                  {/* 逐周活跃度：一根发丝线 + 圆点。没有分析数据的周期留空，
                      不画成 0（0 会被读成「这周没人做」）。 */}
                  <span className="flex shrink-0 items-center gap-[5px]" aria-hidden>
                    {project.activity.map((point) => (
                      <span
                        key={point.period_end}
                        title={
                          point.has_data
                            ? `${point.period_end}：${point.report_count} 篇`
                            : `${point.period_end}：无分析数据`
                        }
                        className={cn(
                          "h-1.5 w-1.5 rounded-full",
                          !point.has_data
                            ? "bg-border"
                            : (point.report_count ?? 0) >= 2
                              ? "bg-accent"
                              : "bg-accent/40"
                        )}
                      />
                    ))}
                  </span>
                  <Badge tone={project.tone as Tone} className="shrink-0">
                    {project.status_label}
                  </Badge>
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-auto flex items-center justify-between gap-2 px-4 pb-3 pt-1">
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: colors.accent }} />
              每个点代表一个周期的活跃度
            </span>
            {items.length > 4 ? (
              <button
                type="button"
                onClick={() => setShowAll((value) => !value)}
                className="inline-flex items-center gap-1 text-xs font-medium text-accent transition-colors hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {showAll ? "收起" : `查看全部 ${items.length} 个项目`}
                <ArrowRight className={cn("h-3 w-3 transition-transform", showAll && "rotate-90")} />
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 7. 长期趋势                                                                  */
/* -------------------------------------------------------------------------- */

function TrendSection({ payload }: { payload: OperatingOverviewPayload }) {
  const { series, periods, basis, skipped_periods: skipped } = payload.trends;
  const skippedCount = skipped?.length ?? 0;
  const colors = chartColorsForTheme(useTheme().theme);
  /** 有 AI 结论的行（图下解读区用）。 */
  const concluded = series.filter((row) => row.conclusion);
  const denominators = series[0]?.denominators;

  /**
   * 趋势的主体是「形状」不是「查数」：6 行 × 6 列的小数字表格铺不满卡宽，
   * 格子之间全是空气。折线把整个画布占满，主题间的消长一眼可比；
   * 精确数字由线尾常驻标签（最新值）与悬浮提示（全量 + 分母）兜底。
   */
  const option = useMemo(() => {
    if (!series.length) return null;
    const palette = chartCategorical(colors, series.length);
    return {
      animationDuration: 900,
      animationDurationUpdate: 450,
      grid: { left: 8, right: 42, top: 34, bottom: 4, containLabel: true },
      legend: { type: "scroll" as const },
      tooltip: {
        trigger: "axis" as const,
        formatter: (params: unknown) => {
          const list = params as Array<{
            dataIndex: number;
            seriesName: string;
            marker?: string;
            value: number | null;
          }>;
          if (!list.length) return "";
          const idx = list[0].dataIndex;
          const den = denominators?.[idx];
          const head = `<div style="font-weight:600;margin-bottom:4px">${periods[idx] ?? ""}</div>`;
          const lines = list
            .map(
              (item) =>
                `<div>${item.marker ?? ""} ${item.seriesName}：<b>${item.value ?? "—"}</b> 篇</div>`
            )
            .join("");
          const foot = den
            ? `<div style="margin-top:4px;opacity:.65">该周期已分析 ${den} 篇</div>`
            : "";
          return head + lines + foot;
        },
      },
      xAxis: {
        type: "category" as const,
        boundaryGap: false,
        data: periods.map((period) => period.slice(5)),
      },
      yAxis: { type: "value" as const, minInterval: 1 },
      series: series.map((row, index) => ({
        name: row.title,
        type: "line" as const,
        smooth: true,
        showSymbol: true,
        symbolSize: 5,
        emphasis: { focus: "series" as const },
        // 0 沿用表格时代的口径画成断点（不把 0 画成「这周期没人提」——
        // 覆盖不足的周期里 0 不可信），悬浮里显示「—」。
        data: row.counts.map((count) => (count === 0 ? null : count)),
        connectNulls: false,
        lineStyle: { width: 2, color: palette[index] },
        itemStyle: { color: palette[index] },
        endLabel: {
          show: true,
          formatter: "{c}",
          distance: 6,
          fontSize: 11,
          fontWeight: 600 as const,
          color: palette[index],
        },
      })),
    };
  }, [series, periods, denominators, colors]);

  // 没有达到趋势判定条件时收起图表卡，不在主看板留下大块空状态。
  if (!series.length || !option) return null;

  return (
    <Card className="flex min-w-0 flex-col">
      <Head icon={TrendingUp} tone="primary" title="长期趋势" desc="哪些问题正在变多" />
      {series.length && option ? (
        <>
          <div className="px-2 pt-1">
            <Chart option={option} height={300} respectReducedMotion={false} />
          </div>
          <div className="border-t border-border/60 px-4 pb-3 pt-2.5">
            {concluded.length ? (
              <>
                <p className="mb-1.5 text-[12px] font-semibold text-foreground/85">AI 解读</p>
                {/* 解读是 1~3 行的短段落，两列栅格把宽度用掉，与每月视图同一套语言。 */}
                <ul className="grid gap-x-8 gap-y-1.5 xl:grid-cols-2">
                  {concluded.map((row) => (
                    <li key={`note-${row.title}`} className="min-w-0 text-xs leading-relaxed">
                      <span className="font-medium text-foreground/90">{row.title}</span>
                      <span className="text-muted-foreground">
                        {" —— "}
                        {withNumbers(row.conclusion ?? "")}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            <div className={cn("space-y-1.5", concluded.length && "mt-3")}>
              <CardFootnote>{basis}</CardFootnote>
              {skipped?.length ? (
                <CardFootnote tone="warning">
                  使用已完成分析的 {periods.length} 个周期；另有 {skippedCount} 个周期覆盖不足，
                  已跳过，不把未分析周报当成 0。
                </CardFootnote>
              ) : null}
            </div>
          </div>
        </>
      ) : null}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export function OperatingOverview({
  payload,
  range,
  onCreateTrack,
  onVerdict,
  feedbackBusy,
  feedbackError,
  onDataChanged,
}: {
  payload: OperatingOverviewPayload;
  range: Range;
  onCreateTrack: (track: NonNullable<OperatingSignal["track"]>) => Promise<number>;
  /**
   * 提交一条人工判定。参数是结论的**业务键**（不是完整指纹）——
   * 指纹由后端拼装，前端不重复拼，避免出现双重周期前缀。
   */
  onVerdict: (
    module: OperatingFeedbackModule,
    itemKey: string,
    verdict: "correct" | "wrong" | "unclear",
    reason?: string
  ) => void;
  feedbackBusy?: boolean;
  feedbackError?: string | null;
  /** 补分析完成后刷新整页数据（分析结果变化会让本页结论失效）。 */
  onDataChanged?: () => void;
}) {
  const { session } = useSession();
  // 判定索引包含多个管理员的记录。会话还没加载完时不展示任何人的判定，
  // 避免把别人的结论误显示成“我已判定”。
  const currentReviewer = session
    ? session.employee_id != null
      ? `employee:${session.employee_id}`
      : "admin"
    : null;
  const [signal, setSignal] = useState<OperatingSignal | null>(null);
  const [tracking, setTracking] = useState(false);
  const [trackError, setTrackError] = useState<string | null>(null);
  const [trackedActionId, setTrackedActionId] = useState<number | null>(null);
  const [bottleneck, setBottleneck] = useState<OperatingBottleneck | null>(null);
  const [duplicate, setDuplicate] = useState<OperatingDuplicate | null>(null);
  const [conflict, setConflict] = useState<OperatingConflict | null>(null);
  const [project, setProject] = useState<OperatingProject | null>(null);

  async function createSignalTrack() {
    const track = signal?.track;
    if (!track || tracking) return;
    setTracking(true);
    setTrackError(null);
    try {
      const actionId = await onCreateTrack(track);
      setTrackedActionId(actionId);
    } catch (error) {
      setTrackError(error instanceof Error ? error.message : "纳入跟进失败，请稍后重试。");
    } finally {
      setTracking(false);
    }
  }

  return (
    <div className="space-y-3">
      {feedbackError ? (
        <p className="rounded-control border border-destructive/30 bg-destructive-soft px-3 py-2 text-xs text-destructive">
          人工判定未保存：{feedbackError}
        </p>
      ) : null}
      {/* 两列各自向下排列，避免短卡被长卡撑开后留下整行空白。 */}
      <section className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
        <div className="min-w-0 space-y-3">
          <AttentionSection
            payload={payload}
            range={range}
            onOpen={(item) => {
              setSignal(item);
              setTrackError(null);
              setTrackedActionId(null);
            }}
            onVerdict={onVerdict}
            feedbackBusy={feedbackBusy}
            feedbackRate={rateFor(payload, "attention", MIN_SAMPLE_FOR_RATE)}
          />
          <BottleneckSection payload={payload} onOpen={setBottleneck} />
          <FrictionSection
            payload={payload}
            onOpenDuplicate={setDuplicate}
            onOpenConflict={setConflict}
          />
        </div>
        <div className="min-w-0 space-y-3">
          <OverviewSection
            payload={payload}
            range={range}
            onDataChanged={onDataChanged}
            onOpenSignal={(item) => {
              setSignal(item);
              setTrackError(null);
              setTrackedActionId(null);
            }}
          />
          <FocusSection payload={payload} />
          <ProjectSection payload={payload} range={range} onOpen={setProject} />
        </div>
      </section>

      <section className="min-w-0">
        <TrendSection payload={payload} />
      </section>

      {/* 下钻：每条结论都能展开看到依据与原文。 */}
      <Modal
        open={Boolean(signal)}
        title={signal?.title || ""}
        onClose={() => setSignal(null)}
        size="lg"
        footer={
          signal ? (
            <div className="w-full space-y-2">
              {trackError ? (
                <p role="alert" className="text-xs text-destructive">
                  {trackError}
                </p>
              ) : null}
              <ModalVerdictFooter
                question="这条信号是否准确？"
                buttons={
                  <FeedbackButtons
                    module="attention"
                    itemKey={signal.title}
                    current={feedbackFor(
                      payload,
                      "attention",
                      range.start,
                      range.end,
                      signal.title,
                      currentReviewer
                    )}
                    onVerdict={onVerdict}
                    busy={feedbackBusy}
                  />
                }
                action={
                  signal.track ? (
                    trackedActionId ? (
                      <Link
                        href={`/actions/${trackedActionId}`}
                        className="text-xs font-medium text-primary hover:underline"
                      >
                        已纳入管理跟进 · 查看事项
                      </Link>
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={createSignalTrack}
                        loading={tracking}
                      >
                        纳入管理跟进
                      </Button>
                    )
                  ) : (
                    <span className="text-xs text-muted-foreground">暂无匹配的跟进来源</span>
                  )
                }
              />
            </div>
          ) : null
        }
      >
        {signal ? (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={signal.tone as Tone}>{signal.label}</Badge>
              {/* 结论来源标出来：AI 分析与规则计数句读法不同 —— 前者是判断，
                  后者只是事实压缩。旧周期没重算总结时这里是「规则计算」，不是 bug。 */}
              <Badge tone={signal.conclusion_source === "ai" ? "accent" : "neutral"}>
                {signal.conclusion_source === "ai" ? "AI 分析" : "规则计算"}
              </Badge>
            </div>
            <p className="text-[14px] leading-relaxed text-foreground">
              {withNumbers(signal.conclusion)}
            </p>
            {/* 评分、指标、来源与依据事项全部是数据依据，收进折叠区。 */}
            <EvidenceDisclosure summary={`值得关注度 ${signal.attention_score}/100`}>
              {/* 趋势信号的 impact_workstreams 是「最新周期提到它的周报篇数」，
                  不是工作项 —— 按工作项标会让人拿着 8 去问「哪 8 项」。 */}
              <ModalMetricRow
                primary={{
                  label:
                    signal.signal_type === "growing_issue" ? "最新周期提及" : "影响工作",
                  value: signal.impact_workstreams,
                  unit: signal.signal_type === "growing_issue" ? "篇" : "项",
                  tone: "destructive",
                }}
                secondary={[
                  { label: "涉及部门", value: signal.impact_departments },
                  { label: "依据篇数", value: signal.evidence_count },
                ]}
              />
              <SourceEvidenceList sources={signal.sources} range={range} />
              {/* SourceEvidenceList 每张卡已带「查看 #id」；只补列证据卡没覆盖到的周报，不重复列。 */}
              {(() => {
                const covered = new Set(signal.sources.map((source) => source.report_id));
                const extra = signal.source_report_ids.filter((id) => !covered.has(id));
                return extra.length ? (
                  <SourceLinks ids={extra} range={range} max={12} />
                ) : null;
              })()}
            </EvidenceDisclosure>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(bottleneck)}
        title={bottleneck ? `瓶颈：${bottleneck.target}` : ""}
        onClose={() => setBottleneck(null)}
        size="lg"
        footer={
          bottleneck ? (
            <ModalVerdictFooter
              question="这条瓶颈是否准确？"
              buttons={
                <FeedbackButtons
                  module="bottleneck"
                  itemKey={bottleneck.target}
                  current={feedbackFor(
                    payload,
                    "bottleneck",
                    range.start,
                    range.end,
                    bottleneck.target,
                    currentReviewer
                  )}
                  onVerdict={onVerdict}
                  busy={feedbackBusy}
                />
              }
            />
          ) : null
        }
      >
        {bottleneck ? (
          <div className="space-y-4 text-sm">
            {/* 结论先行：AI 人话（与卡上同源）；无 AI 时退回事实串，页面不空。 */}
            <p className="text-[14px] leading-relaxed text-foreground">
              {withNumbers(
                bottleneck.conclusion ??
                  `影响 ${bottleneck.workstream_count} 项工作 · ${bottleneck.department_count} 个部门`
              )}
            </p>
            {/* 指标、部门清单、依据事项与口径说明都是数据依据，收进折叠区。 */}
            <EvidenceDisclosure
              summary={`影响 ${bottleneck.workstream_count} 项工作 · ${bottleneck.department_count} 个部门`}
            >
              <ModalMetricRow
                primary={{
                  label: "影响工作",
                  value: bottleneck.workstream_count,
                  unit: "项",
                  tone: "destructive",
                }}
                secondary={[
                  { label: "涉及部门", value: bottleneck.department_count },
                  { label: "涉及人数", value: bottleneck.employee_count },
                ]}
              />
              {bottleneck.departments.length ? (
                <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1.5">
                  <span className="text-xs text-muted-foreground">
                    涉及 {bottleneck.department_count} 个部门：
                  </span>
                  {bottleneck.departments.map((department) => (
                    <span
                      key={department}
                      className="rounded-pill bg-muted px-2 py-0.5 text-xs text-foreground/80"
                    >
                      {department}
                    </span>
                  ))}
                </div>
              ) : null}
              <SourceEvidenceList sources={bottleneck.sources} range={range} />
              <MethodNote label="归一依据（口径说明）">
                关键词：{bottleneck.matched_keywords.join("、") || "—"}
                。同一被依赖对象的不同说法归到一组；命中的关键词可核对，判断口径不用猜。
              </MethodNote>
              {payload.bottlenecks.unattributed_count ? (
                <MethodNote label="未计入的依赖">
                  另有 {payload.bottlenecks.unattributed_count}{" "}
                  条依赖在周报里没有可归属的业务对象，未计入影响面统计。
                </MethodNote>
              ) : null}
            </EvidenceDisclosure>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(duplicate)}
        title={duplicate ? `疑似重复推进：${duplicate.project}` : ""}
        onClose={() => setDuplicate(null)}
        size="lg"
        footer={
          duplicate ? (
            <ModalVerdictFooter
              question="这个提示是否准确？"
              buttons={
                <FeedbackButtons
                  module="duplicate"
                  itemKey={duplicate.project}
                  current={feedbackFor(
                    payload,
                    "duplicate",
                    range.start,
                    range.end,
                    duplicate.project,
                    currentReviewer
                  )}
                  onVerdict={onVerdict}
                  busy={feedbackBusy}
                />
              }
            />
          ) : null
        }
      >
        {duplicate ? (
          <div className="space-y-4 text-sm">
            {/* 结论先行（AI 分析或规则串），证据与口径说明收进数据依据折叠区。 */}
            <p className="text-[14px] leading-relaxed text-foreground">
              {withNumbers(
                duplicate.conclusion ??
                  `${duplicate.departments.join("、")} 在同一周期各自推进，共 ${duplicate.evidence_count} 条事项未看到协作分工描述`
              )}
            </p>
            <EvidenceDisclosure
              summary={`涉及 ${duplicate.department_count} 个部门 · ${duplicate.evidence_count} 条事项`}
            >
              {duplicate.sources.map((source) => (
                <div key={source.id} className="rounded-control bg-muted/40 p-3">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="text-muted-foreground">{evidenceDateLabel(source)}</span>
                    <Badge tone="neutral">{source.department_name || "部门未知"}</Badge>
                    <span className="text-muted-foreground">{source.employee_name || "提交人未知"}</span>
                  </div>
                  <p className="mt-1.5 text-[13px] font-medium">{source.title}</p>
                  {source.source_text || source.description ? (
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {source.source_text || source.description}
                    </p>
                  ) : null}
                  <Link
                    href={reportHref(source.report_id, range)}
                    className="num mt-2 inline-block text-xs font-medium text-accent hover:underline"
                  >
                    查看 #{source.report_id}
                  </Link>
                </div>
              ))}
              <MethodNote>
                以下内容来自不同部门的周报，指向同一业务对象且都在推进。系统只提示「建议核对分工」，
                不判定谁做了什么、也不认为存在重复劳动 —— 两边可能有我们看不到的沟通。
              </MethodNote>
            </EvidenceDisclosure>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(conflict)}
        title={conflict ? `状态不一致：${conflict.title}` : ""}
        onClose={() => setConflict(null)}
        size="lg"
        footer={
          conflict ? (
            <ModalVerdictFooter
              question="这个提示是否准确？"
              buttons={
                <FeedbackButtons
                  module="conflict"
                  itemKey={conflict.title}
                  current={feedbackFor(
                    payload,
                    "conflict",
                    range.start,
                    range.end,
                    conflict.title,
                    currentReviewer
                  )}
                  onVerdict={onVerdict}
                  busy={feedbackBusy}
                />
              }
            />
          ) : null
        }
      >
        {conflict ? (
          <div className="space-y-4 text-sm">
            {/* 结论先行（AI 分析或规则串），双方记录与判定依据收进数据依据折叠区。 */}
            <p className="text-[14px] leading-relaxed text-foreground">
              {withNumbers(
                conflict.conclusion ??
                  `${conflict.finished_by.join("、")} 记录为已完成，${conflict.open_by.join("、")} 仍记录为待确认或未解决`
              )}
            </p>
            <EvidenceDisclosure
              summary={`已完成 ${conflict.finished_by.length} 方 · 未解决 ${conflict.open_by.length} 方`}
            >
              <div className="grid grid-cols-[minmax(0,1fr)] gap-3 sm:grid-cols-2">
                <div className="rounded-control bg-success-soft/50 p-3">
                  <p className="text-xs font-medium text-success">记录为已完成</p>
                  <ul className="mt-1.5 space-y-1 text-[13px]">
                    {conflict.finished_by.map((name) => (
                      <li key={name}>{name}</li>
                    ))}
                  </ul>
                </div>
                <div className="rounded-control bg-warning-soft/60 p-3">
                  <p className="text-xs font-medium text-warning">仍记录为未解决</p>
                  <ul className="mt-1.5 space-y-1 text-[13px]">
                    {conflict.open_by.map((name) => (
                      <li key={name}>{name}</li>
                    ))}
                  </ul>
                </div>
              </div>
              {/* 判定依据必须能核对：系统说「这两条是同一件事」，得让人看到它凭哪两句
                  原文如此判断 —— 拿共用业务对象名当同一件事正是这里出过误报。 */}
              {conflict.evidence?.length ? (
                <div>
                  <p className="mb-2 text-xs font-medium text-muted-foreground">
                    判定依据（系统据此认为这是同一件事）
                  </p>
                  <ul className="space-y-1.5">
                    {conflict.evidence.map((entry, index) => (
                      <li
                        key={`${entry.side}-${entry.report_id}-${index}`}
                        className="flex items-start gap-2 rounded-control bg-muted/40 px-3 py-2 text-[13px]"
                      >
                        <Badge tone={entry.side === "finished" ? "green" : "amber"}>
                          {entry.side === "finished" ? "已完成" : "未解决"}
                        </Badge>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {evidenceDateLabel(entry)}
                        </span>
                        <span className="min-w-0 flex-1 leading-relaxed">
                          <span className="text-muted-foreground">{entry.employee_name}：</span>
                          {entry.title}
                        </span>
                        <Link
                          href={reportHref(entry.report_id, range)}
                          className="num shrink-0 text-xs font-medium text-accent hover:underline"
                        >
                          #{entry.report_id}
                        </Link>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              <SourceLinks ids={conflict.source_report_ids} range={range} max={10} />
              <MethodNote>
                同一件事被不同的人描述成了不同状态。系统只提示「请核对」，不判断哪一方
                准确 —— 也可能只是两人说的不是同一个阶段。
              </MethodNote>
            </EvidenceDisclosure>
          </div>
        ) : null}
      </Modal>

      <Modal
        open={Boolean(project)}
        title={project ? project.name : ""}
        onClose={() => setProject(null)}
        size="lg"
        footer={
          project ? (
            <ModalVerdictFooter
              question="这个项目分析是否准确？"
              buttons={
                <FeedbackButtons
                  module="project"
                  itemKey={project.name}
                  current={feedbackFor(
                    payload,
                    "project",
                    range.start,
                    range.end,
                    project.name,
                    currentReviewer
                  )}
                  onVerdict={onVerdict}
                  busy={feedbackBusy}
                />
              }
            />
          ) : null
        }
      >
        {project ? (
          <div className="space-y-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={project.tone as Tone}>{project.status_label}</Badge>
            </div>
            {/* 先给结论再说数：负责人点开应该先读到一句能直接转述的话，
                与「今天值得关注」的弹窗同一纪律。旧后端没这个字段时整段不渲染。 */}
            {project.conclusion ? (
              <p className="text-[14px] leading-relaxed text-foreground">
                {withNumbers(project.conclusion)}
              </p>
            ) : null}
            {/* 指标、逐周活跃度、依据事项与口径说明都是数据依据，收进折叠区。 */}
            <EvidenceDisclosure
              summary={
                project.duration_weeks
                  ? `已持续 ${project.duration_weeks} 周 · ${project.report_count} 篇周报提到`
                  : `${project.report_count} 篇周报提到`
              }
            >
              <ModalMetricRow
                primary={{ label: "提到篇数", value: project.report_count, unit: "篇" }}
                secondary={[
                  { label: "参与人数", value: project.employee_count },
                  { label: "涉及部门", value: project.department_count },
                ]}
              />
              <div>
                <p className="mb-1.5 text-xs font-medium text-muted-foreground">逐周活跃度</p>
                <div className="flex flex-wrap gap-2 text-xs">
                  {project.activity.map((point) => (
                    <span
                      key={point.period_end}
                      className={cn(
                        "rounded-pill px-2 py-0.5",
                        point.has_data ? "bg-muted text-foreground" : "bg-muted/40 text-muted-foreground"
                      )}
                    >
                      {formatDate(point.period_end)}{" "}
                      <span className="num font-semibold">
                        {point.has_data ? point.report_count : "—"}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
              <div className="space-y-2">
                <p className="text-xs font-medium text-muted-foreground">依据事项</p>
                {project.evidence.map((item, index) => (
                  <div key={`${item.report_id}-${index}`} className="rounded-control bg-muted/40 p-3">
                    <p className="text-[13px] font-medium">{item.title}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span>{item.department_name || "部门未知"}</span>
                      <span>{item.employee_name || "提交人未知"}</span>
                      {item.status ? <span>{item.status}</span> : null}
                      <Link
                        href={reportHref(item.report_id, range)}
                        className="num font-medium text-accent hover:underline"
                      >
                        查看 #{item.report_id}
                      </Link>
                    </div>
                  </div>
                ))}
              </div>
              <MethodNote label="口径说明">
                {project.conclusion_source === "ai"
                  ? "结论由 AI 根据本期周报分析生成。"
                  : "结论由系统按规则归纳，生成 AI 总结后自动替换为模型分析。"}
                「—」表示该周期没有完成结构化分析的数据，不等于该周期没有推进。
              </MethodNote>
            </EvidenceDisclosure>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
