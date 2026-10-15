"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft,
  Bot,
  FileText,
  Languages,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import {
  Avatar,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  StatusDot,
} from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { AdminOnly } from "@/components/admin-only";
import { formatDate, formatDateTime } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import { TASK_POLL_MAX_MS, usePolling } from "@/lib/use-polling";
import type { ReportDetail } from "@/lib/types";

const TYPE_LABELS: Record<string, string> = {
  weekly: "周报",
  daily: "日报",
  monthly: "月报",
  other: "其他",
};

const ANALYSIS_SECTIONS: Array<{
  key:
    | "completed_items"
    | "ongoing_items"
    | "results"
    | "blockers"
    | "problems"
    | "dependencies"
    | "next_week_plans";
  label: string;
}> = [
  { key: "completed_items", label: "本周完成事项" },
  { key: "ongoing_items", label: "进行中事项" },
  { key: "results", label: "工作成果" },
  { key: "blockers", label: "卡点" },
  { key: "problems", label: "存在问题" },
  { key: "dependencies", label: "外部依赖" },
  { key: "next_week_plans", label: "下周计划" },
];

function FormattedReportText({ text }: { text: string }) {
  const blocks = text.trim().split(/\n{2,}/).filter((block) => block.trim());
  return (
    <div className="space-y-3 text-sm leading-7">
      {blocks.map((block, blockIndex) => {
        const lines = block
          .split("\n")
          .map((line) => line.trimEnd())
          .filter((line) => line.trim());
        const unordered = lines.every((line) => /^\s*[-*•]\s+/.test(line));
        const ordered = lines.every((line) => /^\s*\d+[.、)]\s+/.test(line));
        if (unordered && lines.length > 0) {
          return (
            <ul key={blockIndex} className="space-y-1.5 pl-5">
              {lines.map((line, lineIndex) => (
                <li key={lineIndex} className="list-disc break-words pl-0.5">
                  {line.replace(/^\s*[-*•]\s+/, "")}
                </li>
              ))}
            </ul>
          );
        }
        if (ordered && lines.length > 0) {
          const start = Number(lines[0].match(/^\s*(\d+)/)?.[1] ?? 1);
          return (
            <ol
              key={blockIndex}
              start={start}
              className="space-y-1.5 pl-5"
            >
              {lines.map((line, lineIndex) => (
                <li key={lineIndex} className="list-decimal break-words pl-0.5">
                  {line.replace(/^\s*\d+[.、)]\s+/, "")}
                </li>
              ))}
            </ol>
          );
        }
        return (
          <p key={blockIndex} className="whitespace-pre-wrap break-words">
            {block}
          </p>
        );
      })}
    </div>
  );
}

export default function ReportDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);

  const detail = useAsyncData<ReportDetail>(
    () => apiGet(`/api/reports/${id}`),
    [id]
  );

  const [starting, setStarting] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [translating, setTranslating] = useState(false);
  const [translationError, setTranslationError] = useState<string | null>(null);
  const [translationView, setTranslationView] = useState<
    "translation" | "original"
  >("translation");
  const queueStatus = detail.data?.analysis_queue?.status;
  // 队列长时间停在 pending/running（Worker 挂掉等）时置位，用于提示而不是无限转圈。
  const [analysisStale, setAnalysisStale] = useState(false);

  // 队列离开 pending/running（新任务入队、任务完成或失败）后清除卡死提示。
  useEffect(() => {
    if (queueStatus !== "pending" && queueStatus !== "running") setAnalysisStale(false);
  }, [queueStatus]);

  async function startAnalysis() {
    setStarting(true);
    setActionErr(null);
    try {
      const force = Boolean(detail.data?.analysis);
      await apiPost(`/api/ai/reports/${id}/analyze?force=${force}`);
      await detail.reload();
    } catch (e: unknown) {
      setActionErr(e instanceof Error && e.message ? e.message : "提交分析失败，请稍后重试");
    } finally {
      setStarting(false);
    }
  }

  async function translateToChinese() {
    setTranslating(true);
    setTranslationError(null);
    try {
      const force = Boolean(detail.data?.translation?.available);
      await apiPost(`/api/reports/${id}/translation?force=${force}`);
      await detail.reload();
    } catch (e: unknown) {
      setTranslationError(e instanceof Error ? e.message : "翻译失败，请稍后重试");
    } finally {
      setTranslating(false);
    }
  }

  // 队列处理中自动刷新，页面只读库、不重复触发模型。
  // 超过 5 分钟未完成则停止轮询并提示，避免永远「分析中」+ 无限请求。
  usePolling(() => detail.reload(), 4000, {
    enabled: queueStatus === "pending" || queueStatus === "running",
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: () => setAnalysisStale(true),
  });

  // 队列离开 pending/running（完成或失败）后撤销「任务卡死」提示；
  // 换到另一篇周报时也要复位，避免上一篇的提示串到这一篇。
  useEffect(() => {
    if (queueStatus !== "pending" && queueStatus !== "running") setAnalysisStale(false);
  }, [queueStatus]);

  useEffect(() => {
    setTranslationView("translation");
    setTranslationError(null);
  }, [id]);

  if (detail.error) {
    return (
      <Card>
        <ErrorState message={detail.error} onRetry={detail.reload} />
      </Card>
    );
  }

  const report = detail.data;
  const scoreBreakdown = report?.analysis?.result.score_breakdown;
  const contentLength = report?.content?.length ?? 0;
  const queueRunning = queueStatus === "pending" || queueStatus === "running";
  const queueFailed = queueStatus === "failed";
  const queueLastError = detail.data?.analysis_queue?.last_error ?? null;
  const isEnglishReport = report?.content_language === "english";
  const translationReady = Boolean(
    isEnglishReport &&
      report?.translation.available &&
      report.translation.content
  );
  const showingTranslation = Boolean(
    translationReady && translationView === "translation"
  );
  const translationNeedsRetry =
    report?.translation.status === "failed" ||
    report?.translation.status === "stale";
  // 批量体检结论与结构化分析结论可能只有一个存在，展示时合并成一个信号。
  const duplicateFlag = report?.analysis
    ? report.analysis.result.is_duplicate
    : Boolean(report?.duplicate?.is_duplicate);
  const duplicateKnown = Boolean(report?.analysis ?? report?.duplicate);

  return (
    <div>
      <button
        type="button"
        onClick={() => window.history.back()}
        className="mb-3 inline-flex items-center gap-1.5 rounded-control px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> 返回列表
      </button>

      {detail.loading || !report ? (
        <Card><LoadingBlock rows={6} /></Card>
      ) : (
        <div className="space-y-4">
          {/* 第一层：这是谁、哪一期、交得怎么样。结论细节留给下面的正文与分析。 */}
          <Card className="overflow-hidden p-0">
            <div className="flex flex-wrap items-start gap-3 px-5 py-4">
              <Avatar name={report.employee?.name ?? report.employee_name ?? "未知员工"} size={40} />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                  <h1 className="text-lg font-semibold tracking-tight">{report.title ?? "(无标题)"}</h1>
                  <Badge tone="neutral">{TYPE_LABELS[report.report_type] ?? report.report_type}</Badge>
                </div>
                <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
                  {report.employee ? (
                    <Link
                      href={`/reports?employee_id=${report.employee_id}`}
                      className="font-medium text-foreground hover:text-primary hover:underline"
                    >
                      {report.employee.name}
                    </Link>
                  ) : (
                    <span>未知员工</span>
                  )}
                  {report.employee?.department_name && <span>· {report.employee.department_name}</span>}
                  {report.employee?.position && <span>· {report.employee.position}</span>}
                </p>
                {(report.content_language === "english" || duplicateKnown) && (
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    {report.content_language === "english" && <StatusDot tone="blue">英文周报</StatusDot>}
                    {duplicateKnown && (
                      <StatusDot tone={duplicateFlag ? "red" : "green"}>
                        {duplicateFlag ? "疑似重复" : "重复检测正常"}
                      </StatusDot>
                    )}
                  </div>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-x-4 gap-y-3 border-t border-border px-5 py-4 sm:grid-cols-4">
              <div>
                <p className="text-xs text-muted-foreground">汇报周期</p>
                <p className="mt-1 text-sm num">{formatDate(report.period_start)} ~ {formatDate(report.period_end)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">提交时间</p>
                <p className="mt-1 text-sm num">{formatDateTime(report.submitted_at)}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">正文字数</p>
                <p className="mt-1 text-sm num">{contentLength > 0 ? `${contentLength} 字` : "—"}</p>
              </div>
              <div>
                <p className="text-xs text-muted-foreground">AI 质量分</p>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                  {report.analysis ? (
                    <>
                      <span
                        className={
                          report.analysis.result.score >= 60
                            ? "text-sm font-semibold num text-success"
                            : "text-sm font-semibold num text-warning"
                        }
                      >
                        {report.analysis.result.score}
                      </span>
                      <StatusDot tone={report.analysis.result.is_thin ? "amber" : "green"}>
                        {report.analysis.result.is_thin ? "内容较空" : "内容充实"}
                      </StatusDot>
                    </>
                  ) : (
                    <StatusDot
                      tone={
                        queueRunning ? "amber" : queueFailed ? "red" : "neutral"
                      }
                    >
                      {queueRunning ? "分析中" : queueFailed ? "分析失败" : "未分析"}
                    </StatusDot>
                  )}
                </div>
              </div>
            </div>
          </Card>

          {/* 英文周报默认看中文译文，需要核对时切回原文。 */}
          <Card className="overflow-hidden p-0">
            <div className="card-head card-head-line">
              <div>
                <h2 className="flex items-center gap-1.5 text-sm font-semibold">
                  {showingTranslation ? (
                    <Languages className="h-4 w-4 text-primary" />
                  ) : (
                    <Sparkles className="h-4 w-4 text-primary" />
                  )}
                  {showingTranslation
                    ? "中文译文"
                    : isEnglishReport
                      ? "英文原文"
                      : "正文"}
                </h2>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {showingTranslation
                    ? "默认展示中文译文，可随时切换回英文原文"
                    : isEnglishReport
                      ? "当前查看英文原文，译文缓存不会覆盖原文"
                      : "用于展示与 AI 分析的清洗后正文"}
                </p>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {isEnglishReport && translationReady && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setTranslationView(
                        showingTranslation ? "original" : "translation"
                      )
                    }
                  >
                    <FileText className="h-3.5 w-3.5" />
                    {showingTranslation ? "查看原文" : "查看译文"}
                  </Button>
                )}
                {isEnglishReport && (
                  <AdminOnly>
                    <Button
                      variant={translationReady ? "ghost" : "primary"}
                      size="sm"
                      onClick={translateToChinese}
                      loading={translating}
                      disabled={!report.content}
                    >
                      <Languages className="h-3.5 w-3.5" />
                      {translationReady || translationNeedsRetry
                        ? "重新翻译"
                        : "翻译为中文"}
                    </Button>
                  </AdminOnly>
                )}
                <span className="text-xs text-muted-foreground num">
                  {contentLength} 字
                </span>
              </div>
            </div>
            <div className="max-h-[420px] overflow-y-auto px-5 py-4">
              {translationError && (
                <p className="mb-3 rounded-control bg-destructive-soft px-3 py-2 text-sm text-destructive">
                  {translationError}
                </p>
              )}
              {isEnglishReport && !showingTranslation && !translationReady && (
                <div
                  className={
                    report.translation.status === "failed"
                      ? "mb-4 rounded-control bg-destructive-soft px-4 py-3 text-sm text-destructive"
                      : report.translation.status === "stale"
                        ? "mb-4 rounded-control bg-warning-soft px-4 py-3 text-sm text-warning"
                        : "mb-4 rounded-control border border-dashed border-border px-4 py-3 text-sm text-muted-foreground"
                  }
                >
                  {report.translation.status === "failed"
                    ? report.translation.error ??
                      "上次翻译失败，请检查系统设置后重试。"
                    : report.translation.status === "stale"
                      ? "原文已更新，当前译文已过期。"
                      : "尚未生成中文译文。"}
                  <span className="ml-1">当前显示英文原文。</span>
                </div>
              )}
              {showingTranslation && report.translation.content ? (
                <>
                  <FormattedReportText
                    text={report.translation.content}
                  />
                  <p className="mt-4 border-t border-border/70 pt-3 text-xs text-muted-foreground">
                    {report.translation.source_language
                      ? `源语言：${report.translation.source_language} · `
                      : ""}
                    {report.translation.updated_at
                      ? `翻译时间：${formatDateTime(report.translation.updated_at)}`
                      : "已缓存"}
                  </p>
                </>
              ) : report.content ? (
                <FormattedReportText text={report.content} />
              ) : (
                <EmptyState title="正文为空" desc="汇报中未提取到文本内容" />
              )}
            </div>
          </Card>

          {/* AI 结构化分析：结果一律来自已入库的分析，不重复调模型 */}
          <Card className="overflow-hidden p-0">
            <div className="card-head card-head-line">
              <div>
                <h2 className="flex items-center gap-1.5 text-sm font-semibold">
                  <Bot className="h-4 w-4 text-primary" /> AI 结构化分析
                </h2>
                <p className="mt-0.5 text-xs text-muted-foreground">摘要、事项、卡点、风险与评分来自已入库的分析结果，不在页面重复调用模型</p>
              </div>
              <AdminOnly>
                <Button
                  variant={report.analysis ? "outline" : "primary"}
                  size="sm"
                  onClick={startAnalysis}
                  loading={starting}
                  disabled={queueRunning}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  {report.analysis
                    ? "重新分析"
                    : queueRunning
                      ? "已在队列"
                      : queueFailed
                        ? "分析失败，重试"
                        : "开始分析"}
                </Button>
              </AdminOnly>
            </div>
            <div className="space-y-4 px-5 py-4">
              {actionErr && (
                <p className="rounded-control bg-destructive-soft px-3 py-2 text-sm text-destructive">{actionErr}</p>
              )}
              {!report.analysis ? (
                <div className="rounded-control border border-dashed border-border px-4 py-5 text-sm text-muted-foreground">
                  {queueRunning ? (
                    <>
                      分析队列中，完成后会自动刷新。
                      {analysisStale && (
                        <p className="mt-2 text-warning">
                          已超过 5 分钟未完成，可能后台服务异常；可点击右上角重新提交，或联系管理员检查 Worker。
                        </p>
                      )}
                    </>
                  ) : queueFailed ? (
                    <>
                      <p className="text-destructive">
                        上次分析失败（已重试 {detail.data?.analysis_queue?.attempts ?? 0} 次），点击右上角按钮重新分析。
                      </p>
                      {queueLastError && (
                        <p className="mt-2 break-all text-xs opacity-80">{queueLastError}</p>
                      )}
                    </>
                  ) : (
                    "尚未生成结构化分析。"
                  )}
                </div>
              ) : (
                <>
                  {/* 一行里只让质量分带颜色，其余状态交给圆点，避免一屏五个彩色胶囊 */}
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
                    <Badge tone={report.analysis.result.score >= 60 ? "green" : "amber"}>
                      质量分 {report.analysis.result.score}
                    </Badge>
                    <StatusDot tone={report.analysis.result.is_thin ? "amber" : "green"}>
                      {report.analysis.result.is_thin ? "内容较空" : "内容充实"}
                    </StatusDot>
                    <StatusDot tone={report.analysis.result.is_duplicate ? "red" : "green"}>
                      {report.analysis.result.is_duplicate ? "重复度较高" : "重复正常"}
                    </StatusDot>
                    {report.analysis.result.risks.length > 0 && (
                      <StatusDot tone="red">风险 {report.analysis.result.risks.length} 项</StatusDot>
                    )}
                    {report.analysis.model_name && (
                      <span className="text-xs text-muted-foreground sm:ml-auto">
                        {report.analysis.model_name} · {formatDateTime(report.analysis.created_at)}
                      </span>
                    )}
                  </div>

                  <div>
                    <p className="text-xs font-medium text-muted-foreground">摘要</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm leading-relaxed">{report.analysis.result.summary}</p>
                  </div>

                  {scoreBreakdown && (
                    <div className="rounded-control bg-muted/60 p-3">
                      <p className="text-xs font-medium text-muted-foreground">质量评分明细</p>
                      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground sm:grid-cols-4">
                        {[
                          ["完整度", scoreBreakdown.completeness, 15],
                          ["具体程度", scoreBreakdown.specificity, 20],
                          ["成果", scoreBreakdown.results, 20],
                          ["推进", scoreBreakdown.advancement, 20],
                          ["计划", scoreBreakdown.planning, 10],
                          ["问题描述", scoreBreakdown.problem_quality, 5],
                          ["重复度", scoreBreakdown.duplicate, 10],
                        ].map(([label, value, total]) => (
                          <span key={String(label)}>{label}：<b className="text-foreground">{value ?? "-"}</b> / {total}</span>
                        ))}
                      </div>
                      {scoreBreakdown.reason && <p className="mt-2 text-xs text-muted-foreground">评分理由：{scoreBreakdown.reason}</p>}
                    </div>
                  )}

                  {report.analysis.result.is_thin && report.analysis.result.thin_reason && (
                    <p className="rounded-control bg-warning-soft px-3 py-2 text-xs text-warning">
                      质量提示：{report.analysis.result.thin_reason}
                    </p>
                  )}
                  {report.analysis.result.is_duplicate && report.analysis.result.duplicate_reason && (
                    <p className="rounded-control bg-destructive-soft px-3 py-2 text-xs text-destructive">
                      重复提示：{report.analysis.result.duplicate_reason}
                    </p>
                  )}

                  <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                    {ANALYSIS_SECTIONS.map((section) => {
                      const items = report.analysis?.result[section.key] ?? [];
                      if (items.length === 0) return null;
                      return (
                        <div key={section.key} className="rounded-control bg-muted/40 p-3">
                          <p className="mb-2 text-xs font-medium text-muted-foreground">
                            {section.label} <span className="text-foreground/60">({items.length})</span>
                          </p>
                          <div className="space-y-2">
                            {items.map((item, index) => (
                              <div key={index} className="border-b border-border/60 pb-2 last:border-0 last:pb-0">
                                <div className="flex items-start justify-between gap-2">
                                  <p className="text-sm font-medium">{item.title || "（未命名事项）"}</p>
                                  {item.status && (
                                    <span className="shrink-0 text-xs text-muted-foreground">{item.status}</span>
                                  )}
                                </div>
                                {item.description && (
                                  <p className="mt-0.5 text-xs text-muted-foreground">{item.description}</p>
                                )}
                                {item.source_text && (
                                  <p className="mt-1 rounded-control bg-background/70 px-2 py-1 text-xs text-muted-foreground">
                                    {item.source_text}
                                  </p>
                                )}
                                {(item.related_project || item.mentioned_people.length > 0) && (
                                  <p className="mt-1 text-xs text-muted-foreground">
                                    {item.related_project && `项目：${item.related_project}`}
                                    {item.related_project && item.mentioned_people.length > 0 ? " · " : ""}
                                    {item.mentioned_people.length > 0 && `涉及：${item.mentioned_people.join("、")}`}
                                  </p>
                                )}
                              </div>
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {report.analysis.result.risks.length > 0 && (
                    <div className="overflow-hidden rounded-control border border-destructive/20">
                      <div className="flex items-center justify-between gap-2 bg-destructive-soft/60 px-3 py-2">
                        <p className="text-xs font-medium text-destructive">风险 {report.analysis.result.risks.length} 项</p>
                        <span className="text-xs text-muted-foreground">每条都带原文依据</span>
                      </div>
                      <div className="space-y-2.5">
                        {report.analysis.result.risks.map((risk, index) => (
                          <div key={index} className="px-3 py-2.5">
                            <div className="flex items-start justify-between gap-2">
                              <p className="text-sm font-medium">{risk.title}</p>
                              <Badge tone={risk.level === "高" ? "red" : risk.level === "中" ? "amber" : "neutral"}>
                                {risk.level}风险
                              </Badge>
                            </div>
                            {risk.description && (
                              <p className="mt-0.5 text-xs text-muted-foreground">{risk.description}</p>
                            )}
                            {risk.source_text && (
                              <p className="mt-1 rounded-control bg-muted/60 px-2 py-1 text-xs text-muted-foreground">
                                {risk.source_text}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}

              {report.ai_analyses.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">分析历史</p>
                  <div className="space-y-2">
                    {report.ai_analyses.map((a, i) => (
                      <div key={i} className="rounded-control border border-border px-3 py-2 text-xs text-muted-foreground">
                        {a.analysis_type} · {a.status} · {a.model_name ?? a.model_provider ?? "-"} ·{" "}
                        {formatDateTime(a.created_at)}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </Card>

          {/* 数据来源 */}
          <Card className="px-5 py-3">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-muted-foreground">
              <span>
                数据来源：<span className="text-foreground">{report.source === "wecom" ? "企业微信汇报" : report.source}</span>
              </span>
              <span>
                汇报模板：<span className="text-foreground">{report.template_name ?? "-"}</span>
              </span>
              <span>
                原始记录 ID：<span className="tabular-nums text-foreground">{report.raw_record_id ?? "-"}</span>
              </span>
            </div>
          </Card>
        </div>
      )}
    </div>
  );
}
