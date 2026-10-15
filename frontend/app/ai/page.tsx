"use client";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  Bot,
  Brain,
  Check,
  ChevronDown,
  Clock3,
  FileText,
  History,
  Loader2,
  MessageSquarePlus,
  PanelLeftClose,
  PanelLeftOpen,
  Quote,
  Search,
  Send,
  Sparkles,
  Trash2,
  Undo2,
  X,
  Zap,
} from "lucide-react";
import { AdminOnly } from "@/components/admin-only";
import { AssistantAnswer } from "@/components/assistant-answer";
import { Badge, Button, Card, Modal, PageHeader } from "@/components/ui";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost, apiPostStream } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  defaultPeriodKey,
  formatDate,
  formatDateTime,
  periodLabel,
  periodOptions,
} from "@/lib/format";
import { PeriodPicker } from "@/lib/use-data";
import type { PeriodPickerOption } from "@/lib/use-data";
import type {
  AgentEscalation,
  AgentClarification,
  AgentClarificationOption,
  AgentEvent,
  AgentPayload,
  AgentStep,
  AgentMemoryItem,
  AiHistoryItem,
  DepartmentItem,
  Period,
  SearchSource,
} from "@/lib/types";

const MEMORY_CATEGORIES = [
  { value: "company_rule", label: "公司规则" },
  { value: "glossary", label: "术语解释" },
  { value: "preference", label: "回答偏好" },
  { value: "project", label: "项目背景" },
] as const;

/** 归一错误信息：请求层抛出的都是 Error 子类（ApiError 带 message），取不到就退回兜底文案。
 *  替代散落各处的 `catch (e: any) { e?.message ?? ... }`——那个写法吞掉了类型检查。 */
function apiErrorMessage(e: unknown, fallback: string): string {
  if (e instanceof Error && e.message) return e.message;
  return fallback;
}

function MemoryManager({
  open,
  seed,
  onClose,
}: {
  open: boolean;
  seed: string;
  onClose: () => void;
}) {
  const [items, setItems] = useState<AgentMemoryItem[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [key, setKey] = useState("");
  const [category, setCategory] = useState<AgentMemoryItem["category"]>("company_rule");
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 正在执行启停/删除的记忆 id：按钮禁用防连点，出错能定位到具体条目。
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = async () => setItems(await apiGet<AgentMemoryItem[]>("/api/ai/memories"));

  async function toggleMemory(item: AgentMemoryItem) {
    if (busyId !== null) return;
    setBusyId(item.id);
    setError(null);
    try {
      await apiPatch(`/api/ai/memories/${item.id}`, { enabled: !item.enabled });
      await load();
    } catch (e) {
      setError(apiErrorMessage(e, `${item.enabled ? "停用" : "启用"}「${item.key}」失败`));
    } finally {
      setBusyId(null);
    }
  }

  async function removeMemory(item: AgentMemoryItem) {
    if (busyId !== null || !window.confirm(`确认删除记忆“${item.key}”吗？`)) return;
    setBusyId(item.id);
    setError(null);
    try {
      await apiDelete(`/api/ai/memories/${item.id}`);
      await load();
    } catch (e) {
      setError(apiErrorMessage(e, `删除「${item.key}」失败`));
    } finally {
      setBusyId(null);
    }
  }

  useEffect(() => {
    if (!open) return;
    void load().catch((e) => setError(e?.message ?? "加载长期记忆失败"));
    if (!editingId && seed.trim()) setContent(seed.trim());
  }, [open, seed]);

  function resetForm() {
    setEditingId(null);
    setKey("");
    setCategory("company_rule");
    setContent("");
    setError(null);
  }

  function close() {
    resetForm();
    onClose();
  }

  async function save() {
    if (!key.trim() || !content.trim()) {
      setError("请填写记忆标识和内容");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = {
        key: key.trim(),
        category,
        content: content.trim(),
        source: seed.trim() ? "conversation" : "manual",
      };
      if (editingId) {
        await apiPatch(`/api/ai/memories/${editingId}`, payload);
      } else {
        await apiPost("/api/ai/memories", payload);
      }
      resetForm();
      await load();
    } catch (e: unknown) {
      setError(apiErrorMessage(e, "保存长期记忆失败"));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={open} title="Agent 长期记忆" onClose={close} size="lg">
      <div className="space-y-4">
        <p className="text-xs leading-relaxed text-muted-foreground">
          只保存稳定规则、术语和回答偏好；周报事实仍以实时查询结果为准。保存前请人工确认，禁止填写密码或密钥。
        </p>
        <div className="rounded-card bg-muted/50 p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs font-medium text-foreground">
              记忆标识
              <input
                value={key}
                onChange={(event) => setKey(event.target.value)}
                placeholder="例如 report.period.rule"
                className="mt-1.5 h-9 w-full rounded-control border border-border bg-surface px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
              />
            </label>
            <label className="text-xs font-medium text-foreground">
              分类
              <select
                value={category}
                onChange={(event) => setCategory(event.target.value as AgentMemoryItem["category"])}
                className="mt-1.5 h-9 w-full rounded-control border border-border bg-surface px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
              >
                {MEMORY_CATEGORIES.map((item) => (
                  <option key={item.value} value={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
          </div>
          <label className="mt-3 block text-xs font-medium text-foreground">
            记忆内容
            <textarea
              value={content}
              onChange={(event) => setContent(event.target.value)}
              rows={3}
              maxLength={2000}
              placeholder="写成一句明确、稳定、可复核的事实或偏好"
              className="mt-1.5 w-full resize-y rounded-control border border-border bg-surface px-3 py-2 text-sm leading-relaxed outline-none focus:ring-2 focus:ring-ring"
            />
          </label>
          {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
          <div className="mt-3 flex justify-end gap-2">
            {editingId && <Button variant="outline" size="sm" onClick={resetForm}>取消编辑</Button>}
            <Button size="sm" loading={saving} onClick={() => void save()}>
              {editingId ? "保存修改" : "确认记住"}
            </Button>
          </div>
        </div>

        <div className="space-y-2">
          {items.length === 0 ? (
            <p className="rounded-control bg-muted/40 px-3 py-6 text-center text-xs text-muted-foreground">还没有长期记忆</p>
          ) : items.map((item) => (
            <div key={item.id} className="rounded-control border border-border bg-surface px-3 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold">{item.key}</span>
                    <Badge tone={item.enabled ? "green" : "neutral"}>
                      {MEMORY_CATEGORIES.find((entry) => entry.value === item.category)?.label}
                    </Badge>
                    {!item.enabled && <span className="text-xs text-muted-foreground">已停用</span>}
                  </div>
                  <p className="mt-1.5 whitespace-pre-wrap text-sm leading-relaxed text-foreground/85">{item.content}</p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button
                    type="button"
                    disabled={busyId !== null}
                    onClick={() => void toggleMemory(item)}
                    className="rounded-control px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                  >{busyId === item.id ? "处理中…" : item.enabled ? "停用" : "启用"}</button>
                  <button
                    type="button"
                    disabled={busyId !== null}
                    onClick={() => { setEditingId(item.id); setKey(item.key); setCategory(item.category); setContent(item.content); setError(null); }}
                    className="rounded-control px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                  >编辑</button>
                  <button
                    type="button"
                    disabled={busyId !== null}
                    onClick={() => void removeMemory(item)}
                    className="rounded-control px-2 py-1 text-xs text-destructive hover:bg-destructive-soft disabled:cursor-not-allowed disabled:opacity-50"
                  >删除</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
}

/** AI 能力自检结果。字段与后端 GET /api/ai/status 对齐。 */
interface AiStatusInfo {
  provider: string;
  model: string;
  configured: boolean;
  hint: string | null;
  last_call_at: string | null;
  last_status: string | null;
  last_error: string | null;
}

/** AI 用量账本。刻意不做金额换算：单价会变，这里只给可核对的 token 数。 */
interface AiUsageInfo {
  total_calls: number;
  success: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  by_type: {
    analysis_type: string;
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
  }[];
}

interface ScanResult {
  queued: number;
  running: number;
  already_done: number;
  failed: number;
  total: number;
  force: boolean;
  /** 因单次排队上限未排上、还需下一轮再排的篇数。 */
  remaining?: number;
  limited?: boolean;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  sources?: SearchSource[];
  validCitationIds?: number[];
  /** Agent 这一轮实际调用了哪些检索工具 */
  steps?: AgentStep[];
  /** 原范围没命中、被自动放宽的记录 */
  escalations?: AgentEscalation[];
  /** 本轮真正读了正文的篇数 */
  readCount?: number;
  /** 小范围问题的全量上下文：已把范围内全部原文交给模型 */
  fullContext?: AgentPayload["full_context"];
  batchRead?: AgentPayload["batch_read"];
  progressText?: string;
  /** 读了但被字符预算截断的篇数：>0 说明没读到全文 */
  truncatedCount?: number;
  /** 回答里是否出现有效引用；false 时来源仅代表「读过」 */
  sourcesCited?: boolean;
  analysisId?: number;
  resolvedScope?: AgentPayload["resolved_scope"];
  period?: Period;
  keywordCoverage?: AgentPayload["keyword_coverage"];
  coverageStatus?: AgentPayload["coverage_status"];
  coverageGaps?: string[];
  evidenceType?: AgentPayload["evidence_type"];
  clarification?: AgentClarification;
  feedback?: AiHistoryItem["feedback"];
  scope?: "period" | "all";
  model?: string | null;
  streaming?: boolean;
  error?: string | null;
}

/** 工具名 -> 人话。模型看到的是函数名，管理层看到的是动作。 */
const TOOL_LABELS: Record<string, string> = {
  read_batch: "分批阅读周报原文",
  search_reports: "检索周报",
  get_reports: "阅读原文",
  list_submissions: "查询提交名单",
  list_report_quality: "读取质量评分",
  list_blockers: "统计卡点",
  list_tasks: "统计未推进任务",
  list_periods: "查看周期",
  list_employees: "查询员工",
};

const SCOPE_LABELS: Record<string, string> = {
  period: "当前周期",
  recent: "近 4 周",
  all: "全部周期",
};

function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

function scopeLabel(scope: string): string {
  return SCOPE_LABELS[scope] ?? scope;
}

/**
 * 模型状态不能只看「有没有 Key」。
 *
 * 真实踩到的坑：DeepSeek 余额为 0 时 Key 依然存在，配置判定为「已连接」，
 * 但每次调用都返回 402，页面却是满绿的——用户会以为「改了密钥没生效」。
 * 所以把「最近一次调用是否失败」也算进状态。
 */
function aiStateOf(status: AiStatusInfo): "ok" | "failed" | "unconfigured" {
  if (!status.configured) return "unconfigured";
  if (status.last_status === "failed") return "failed";
  return "ok";
}

const AI_STATE_STYLE: Record<
  "ok" | "failed" | "unconfigured",
  { chip: string; dot: string; label: string }
> = {
  ok: {
    chip: "bg-success-soft text-success",
    dot: "bg-success",
    label: "模型已连接",
  },
  failed: {
    chip: "bg-destructive-soft text-destructive",
    dot: "bg-destructive",
    label: "上次调用失败",
  },
  unconfigured: {
    chip: "bg-warning-soft text-warning",
    dot: "bg-warning",
    label: "未配置模型",
  },
};

/** 「全部周期」不是时间段，而是放开周期过滤；与后端 scope=all 对应。 */
const ALL_SCOPE = "all";

/**
 * 来源面板默认展示条数，与后端 MAX_SOURCES 对齐；超过时折叠并提供「展开全部」。
 * 后端对「真正引用」的来源已不再截断，展示层的折叠只是防长列表刷屏。
 */
const SOURCES_PREVIEW_COUNT = 5;

function createConversationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `conversation-${Date.now()}`;
}

function SourceCard({ source }: { source: SearchSource }) {
  return (
    <Link
      href={`/reports/${source.report_id}`}
      className="block rounded-control bg-muted/40 px-3.5 py-3 transition-colors hover:bg-primary-soft/60"
    >
      <div className="flex items-start gap-2">
        <Quote className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
            <p className="truncate text-xs font-medium text-foreground">
              {source.title ?? "无标题周报"}
              {source.employee_name ? ` · ${source.employee_name}` : ""}
            </p>
            {source.period_start && source.period_end && (
              <span className="shrink-0 text-xs text-muted-foreground">
                {formatDate(source.period_start)} ~ {formatDate(source.period_end)}
              </span>
            )}
          </div>
          {source.reason && (
            <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
              {source.reason}
            </p>
          )}
        </div>
      </div>
    </Link>
  );
}

function StepList({
  steps,
  escalations,
}: {
  steps: AgentStep[];
  escalations?: AgentEscalation[];
}) {
  if (steps.length === 0 && !escalations?.length) return null;
  return (
    <div className="space-y-1.5">
      {steps.map((step, index) => (
        <div key={`${step.step}-${step.tool}-${index}`} className="flex items-start gap-2 text-xs">
          <span className="mt-px w-3 shrink-0 text-right tabular-nums text-muted-foreground/60">
            {index + 1}
          </span>
          <span className="shrink-0 font-medium text-foreground/80">{toolLabel(step.tool)}</span>
          <span className="min-w-0 flex-1 leading-relaxed text-muted-foreground">
            {step.summary}
          </span>
          <span className="shrink-0 tabular-nums text-muted-foreground/60">
            {step.elapsed_ms}ms
          </span>
        </div>
      ))}
      {escalations && escalations.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          <Badge tone="amber">
            范围已自动放宽 {scopeLabel(escalations[0].from)} → {scopeLabel(escalations[escalations.length - 1].to)}
          </Badge>
          <span className="text-xs text-muted-foreground">
            当前范围内没有命中，已往更宽的范围继续检索
          </span>
        </div>
      )}
    </div>
  );
}

function ChatBubble({
  message,
  onClarify,
  onFeedback,
}: {
  message: ChatMessage;
  onClarify: (question: string, option: AgentClarificationOption, messageId: string) => void;
  onFeedback: (analysisId: number, feedback: NonNullable<AiHistoryItem["feedback"]>) => void;
}) {
  const isUser = message.role === "user";
  const [openPanel, setOpenPanel] = useState<"steps" | "sources" | null>(null);
  const [sourcesExpanded, setSourcesExpanded] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [feedbackCategory, setFeedbackCategory] = useState<"scope" | "fact" | "evidence" | "omission" | "other">("fact");
  const [feedbackReason, setFeedbackReason] = useState("");
  const [feedback, setFeedback] = useState(message.feedback ?? null);
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  useEffect(() => setFeedback(message.feedback ?? null), [message.feedback]);
  const hasSteps = Boolean(message.steps?.length || message.escalations?.length);
  const hasSources = Boolean(message.sources?.length);
  const feedbackLabels = {
    scope: "范围不对",
    fact: "事实不对",
    evidence: "证据不对",
    omission: "有遗漏",
    other: "其他",
  };

  async function submitFeedback() {
    if (!message.analysisId) return;
    setFeedbackBusy(true);
    setFeedbackError(null);
    try {
      const saved = await apiPost<NonNullable<AiHistoryItem["feedback"]> & { analysis_id: number }>(
        `/api/ai/history/${message.analysisId}/feedback`,
        { category: feedbackCategory, reason: feedbackReason.trim() || undefined }
      );
      setFeedback(saved);
      setFeedbackOpen(false);
      onFeedback(message.analysisId, saved);
    } catch (error: unknown) {
      setFeedbackError(apiErrorMessage(error, "反馈提交失败，请稍后重试"));
    } finally {
      setFeedbackBusy(false);
    }
  }
  return (
    <div className={`flex gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
      {!isUser && (
        <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-primary text-primary-foreground">
          <Bot className="h-4 w-4" />
        </div>
      )}
      <div className={`max-w-[min(760px,88%)] ${isUser ? "items-end" : "items-start"}`}>
        <div
          className={
            isUser
              ? "rounded-card rounded-tr-control bg-primary px-4 py-3 text-sm leading-relaxed text-primary-foreground"
              : "rounded-card rounded-tl-control border border-border bg-surface px-4 py-3 text-sm leading-relaxed"
          }
        >
          {message.streaming && !message.text ? (
            <span className="inline-flex items-center gap-2 text-muted-foreground">
              <span className="flex gap-1">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:120ms]" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:240ms]" />
              </span>
              {message.progressText ?? (message.steps && message.steps.length > 0 ? "正在继续检索…" : "正在回复…")}
            </span>
          ) : isUser ? (
            <p className="whitespace-pre-wrap">{message.text}</p>
          ) : (
            <AssistantAnswer
              text={message.text}
              sources={message.sources}
              validCitationIds={message.validCitationIds}
            />
          )}
          {!isUser && message.error && (
            <p className="mt-2 rounded-control bg-destructive-soft px-2.5 py-1.5 text-xs text-destructive">
              {message.error}
            </p>
          )}
          {!isUser && message.clarification && (
            <div className="mt-3 rounded-control border border-warning/30 bg-warning-soft/50 p-3">
              <p className="text-sm">{message.clarification.message}</p>
              {message.clarification.options.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {message.clarification.options.map((option, index) => (
                    <Button
                      key={`${option.employee_id ?? option.department_id ?? index}`}
                      size="sm"
                      variant="outline"
                      onClick={() => onClarify(message.clarification!.question ?? "", option, message.id)}
                    >
                      {option.label}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          )}
          {!isUser && message.streaming && message.steps && (
            <div className="mt-3">
              <StepList steps={message.steps} escalations={message.escalations} />
            </div>
          )}
          {!isUser &&
            !message.streaming &&
            (hasSteps || hasSources || message.readCount !== undefined || message.model) && (
              <div className="mt-3 border-t border-border pt-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    {message.steps && message.steps.length > 0 && (
                      <span>处理 {message.steps.length} 步</span>
                    )}
                    {!message.fullContext?.enabled &&
                      message.readCount !== undefined &&
                      message.evidenceType !== "structured_data" &&
                      message.evidenceType !== "conversation" && (
                      <span>精读 {message.readCount} 篇全文</span>
                    )}
                    {message.fullContext?.enabled && (
                      <span className="text-accent">
                        已载入全量 {message.fullContext.report_count} 篇原文
                      </span>
                    )}
                    {message.batchRead?.enabled && (
                      <span className="text-accent">
                        已分批读完 {message.batchRead.report_count} 篇原文（{message.batchRead.batch_count} 批）
                      </span>
                    )}
                    {Boolean(message.truncatedCount) && (
                      <span className="text-warning">
                        {message.truncatedCount} 篇正文被截断
                      </span>
                    )}
                    {message.model && <span>{message.model}</span>}
                  </div>
                  <div className="flex flex-wrap items-center justify-end gap-1">
                    {hasSteps && (
                      <button
                        type="button"
                        onClick={() => setOpenPanel(openPanel === "steps" ? null : "steps")}
                        className={`inline-flex h-7 items-center gap-1 rounded-control px-2 text-xs font-medium transition-colors ${
                          openPanel === "steps"
                            ? "bg-primary-soft text-primary"
                            : "text-muted-foreground hover:bg-muted hover:text-foreground"
                        }`}
                      >
                        <Search className="h-3 w-3" />
                        检索过程
                        <ChevronDown
                          className={`h-3 w-3 transition-transform ${
                            openPanel === "steps" ? "rotate-180" : ""
                          }`}
                        />
                      </button>
                    )}
                    {hasSources && (
                      <button
                        type="button"
                        onClick={() => setOpenPanel(openPanel === "sources" ? null : "sources")}
                        className={`inline-flex h-7 items-center gap-1 rounded-control px-2 text-xs font-medium transition-colors ${
                          openPanel === "sources"
                            ? "bg-primary-soft text-primary"
                            : "text-muted-foreground hover:bg-muted hover:text-foreground"
                        }`}
                      >
                        <FileText className="h-3 w-3" />
                        {message.sourcesCited === false ? "读过" : "引用来源"}{" "}
                        {message.sources?.length}
                        <ChevronDown
                          className={`h-3 w-3 transition-transform ${
                            openPanel === "sources" ? "rotate-180" : ""
                          }`}
                        />
                      </button>
                    )}
                  </div>
                </div>
                {openPanel === "steps" && hasSteps && (
                  <div className="mt-2 rounded-control bg-muted/60 px-3 py-2.5">
                    <StepList steps={message.steps ?? []} escalations={message.escalations} />
                  </div>
                )}
                {openPanel === "sources" && hasSources && message.sources && (
                  <div className="mt-2 space-y-2">
                    {(sourcesExpanded
                      ? message.sources
                      : message.sources.slice(0, SOURCES_PREVIEW_COUNT)
                    ).map((source) => (
                      <SourceCard key={`${message.id}-${source.report_id}`} source={source} />
                    ))}
                    {message.sources.length > SOURCES_PREVIEW_COUNT && (
                      <button
                        type="button"
                        onClick={() => setSourcesExpanded((value) => !value)}
                        className="w-full rounded-control border border-border px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                      >
                        {sourcesExpanded
                          ? "收起来源"
                          : `展开全部 ${message.sources.length} 条来源`}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          {!isUser && !message.streaming && message.resolvedScope && message.evidenceType !== "conversation" && (
            <p className="mt-2 text-xs text-muted-foreground">
              本轮范围：{message.scope === ALL_SCOPE ? "全部周期" : message.period ? `${message.period.start} 至 ${message.period.end}` : "未记录周期"}
              {message.resolvedScope.department ? ` · ${message.resolvedScope.department}` : " · 当前可访问部门"}
              {message.resolvedScope.employee ? ` · ${message.resolvedScope.employee}` : ""}
              {message.evidenceType === "structured_data" ? " · 结构化统计工具" : ""}
            </p>
          )}
          {/* 对话类回答（问候/闲聊/改写）不读周报，不存在覆盖缺口；
              即使后端状态异常也不给用户展示误导性的覆盖警告。 */}
          {!isUser && !message.streaming && message.evidenceType !== "conversation" && message.coverageStatus === "translation_gap" && (
            <p className="mt-2 rounded-control bg-warning-soft px-2.5 py-1.5 text-xs text-warning">
              中文关键词覆盖不完整：{message.keywordCoverage?.translation_gaps ?? 0} 篇英文周报没有可用译文，不能据此断言无相关内容。
            </p>
          )}
          {!isUser && !message.streaming && message.evidenceType !== "conversation" && message.coverageStatus === "incomplete" && (
            <p className="mt-2 rounded-control bg-warning-soft px-2.5 py-1.5 text-xs text-warning">
              本轮覆盖不完整：{(message.coverageGaps ?? []).map((gap) => ({
                output_budget: "模型上下文预算已截断",
                character_budget: "正文读取预算已用尽",
                pagination_pending: "检索分页尚未读完",
                pagination_cap: "检索达到分页安全上限",
                only_excerpts: "回答仅基于检索摘要，未读取任何周报全文",
                unread_search_results: "部分已检索到的周报尚未阅读全文",
                no_search_after_fallback: "原文超出全量阅读预算，且尚未检索",
              }[gap] ?? gap)).join("；") || "部分检索结果未完整载入"}。请缩小周期或关键词后重试。
            </p>
          )}
          {!isUser && !message.streaming && !message.error && message.analysisId && (
            <div className="mt-3 border-t border-border pt-2.5">
              {feedback ? (
                <p className="text-xs text-success">已记录反馈：{feedbackLabels[feedback.category]}{feedback.reason ? ` · ${feedback.reason}` : ""}</p>
              ) : (
                <button
                  type="button"
                  onClick={() => setFeedbackOpen((open) => !open)}
                  className="text-xs font-medium text-muted-foreground hover:text-primary"
                >
                  答得不对
                </button>
              )}
              {feedbackOpen && !feedback && (
                <div className="mt-2 grid gap-2 sm:grid-cols-[150px_1fr_auto]">
                  <select
                    value={feedbackCategory}
                    onChange={(event) => setFeedbackCategory(event.target.value as typeof feedbackCategory)}
                    className="h-9 rounded-control border border-border bg-surface px-2 text-xs"
                  >
                    {Object.entries(feedbackLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                  </select>
                  <input
                    value={feedbackReason}
                    onChange={(event) => setFeedbackReason(event.target.value)}
                    maxLength={2000}
                    placeholder="补充说明（可选）"
                    className="h-9 min-w-0 rounded-control border border-border bg-surface px-2 text-xs"
                  />
                  <Button size="sm" onClick={() => void submitFeedback()} loading={feedbackBusy}>提交</Button>
                </div>
              )}
              {feedbackError && <p className="mt-2 text-xs text-destructive">{feedbackError}</p>}
            </div>
          )}
        </div>
      </div>
      {isUser && (
        <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-control border border-primary/20 bg-primary-soft text-primary">
          <span className="text-xs font-semibold">我</span>
        </div>
      )}
    </div>
  );
}

function HistoryPanel({
  history,
  activeHistoryId,
  rangeText,
  keyword,
  mode = "desktop",
  pendingDeleteId,
  deletingHistoryId,
  onOpen,
  onNewChat,
  onCollapse,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
}: {
  history: AiHistoryItem[];
  activeHistoryId: number | null;
  rangeText: string;
  keyword: string;
  mode?: "desktop" | "mobile";
  pendingDeleteId: number | null;
  deletingHistoryId: number | null;
  onOpen: (item: AiHistoryItem) => void;
  onNewChat: () => void;
  onCollapse: () => void;
  onRequestDelete: (id: number) => void;
  onCancelDelete: () => void;
  onConfirmDelete: (item: AiHistoryItem) => void;
}) {
  return (
    <Card className="flex h-full min-h-0 flex-col overflow-hidden">
      <div className="card-head card-head-line">
        <div className="flex items-center gap-2">
          <History className="h-4 w-4 text-muted-foreground" />
          <span className="text-sm font-semibold">聊天记录</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onNewChat}
            title="新对话"
            aria-label="新对话"
            className="inline-flex h-8 w-8 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <MessageSquarePlus className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={onCollapse}
            title={mode === "mobile" ? "关闭聊天记录" : "收起聊天记录"}
            aria-label={mode === "mobile" ? "关闭聊天记录" : "收起聊天记录"}
            className="inline-flex h-8 w-8 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            {mode === "mobile" ? (
              <X className="h-3.5 w-3.5" />
            ) : (
              <PanelLeftClose className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {history.length === 0 ? (
          <div className="px-3 py-10 text-center">
            <History className="mx-auto h-7 w-7 text-muted-foreground/40" />
            <p className="mt-2 text-xs font-medium text-muted-foreground">还没有聊天记录</p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground/80">
              发起一次提问后，会在这里留下可回看的记录
            </p>
          </div>
        ) : (
          <div className="space-y-1">
            {history.map((item) => {
              const confirming = pendingDeleteId === item.id;
              const deleting = deletingHistoryId === item.id;
              return (
                <div
                  key={item.id}
                  className={cn(
                    "group flex items-stretch rounded-control transition-colors",
                    activeHistoryId === item.id
                      ? "bg-primary-soft text-foreground"
                      : "hover:bg-muted"
                  )}
                >
                  <button
                    type="button"
                    onClick={() => onOpen(item)}
                    className="min-w-0 flex-1 px-3 py-2.5 text-left"
                  >
                    <p className="line-clamp-2 text-xs font-medium leading-relaxed">
                      {item.question ?? "（无问题）"}
                    </p>
                    <div className="mt-1.5 flex items-center gap-1 text-xs text-muted-foreground">
                      <Clock3 className="h-3 w-3" />
                      {formatDateTime(item.created_at)}
                    </div>
                  </button>

                  {confirming ? (
                    <div className="flex shrink-0 items-center gap-0.5 pr-1.5">
                      <button
                        type="button"
                        onClick={() => onConfirmDelete(item)}
                        disabled={deleting}
                        title="确认删除"
                        aria-label="确认删除"
                        className="inline-flex h-7 w-7 items-center justify-center rounded-control text-destructive transition-colors hover:bg-destructive-soft disabled:opacity-50"
                      >
                        {deleting ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={onCancelDelete}
                        disabled={deleting}
                        title="取消"
                        aria-label="取消删除"
                        className="inline-flex h-7 w-7 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={() => onRequestDelete(item.id)}
                      title="删除聊天记录"
                      aria-label={`删除聊天记录：${item.question ?? "无问题"}`}
                      className="mr-1.5 my-1.5 inline-flex w-7 shrink-0 items-center justify-center rounded-control text-muted-foreground opacity-100 transition-colors hover:bg-destructive-soft hover:text-destructive sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-border bg-muted/40 px-3 py-3">
        <p className="text-xs font-medium text-muted-foreground">当前查阅范围</p>
        <p className="mt-1 text-xs font-medium">{rangeText}</p>
        {keyword.trim() && (
          <p className="mt-1 truncate text-xs text-muted-foreground">
            关键词：{keyword.trim()}
          </p>
        )}
      </div>
    </Card>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-medium tabular-nums">{value}</p>
    </div>
  );
}

export default function AiSearchPage() {
  const options = periodOptions();
  const [periodKey, setPeriodKey] = useState(defaultPeriodKey());
  const [customPeriod, setCustomPeriod] = useState<Period | null>(null);
  const [departmentId, setDepartmentId] = useState<number | null>(null);
  const [departmentName, setDepartmentName] = useState<string | null>(null);
  const [scopeReady, setScopeReady] = useState(false);
  const [scopeError, setScopeError] = useState<string | null>(null);
  const isAllScope = periodKey === ALL_SCOPE;
  // 「全部周期」不是一个时间段，所以关掉周期过滤后，后台批量工具仍需要一个
  // 具体周期才有意义；这里保留一个具体周期，仅在 all 模式下停用这些按钮。
  const period = options.find((item) => item.key === periodKey) ?? customPeriod ?? options[0]!;
  const scopeTabs = [
    ...options.map((option) => ({ key: option.key, label: option.label, start: option.start, end: option.end })),
    // 「全部周期」不是真实区间，start/end 填当前区间占位（选择时按 key 分流，不用区间）。
    // 链接带来的自定义区间不再单设 chip：PeriodPicker 内置的「自定义」档直接承接高亮。
    { key: ALL_SCOPE, label: "全部周期", start: period.start, end: period.end },
  ];
  const currentSelection: PeriodPickerOption = {
    key: periodKey,
    label: isAllScope
      ? "全部周期"
      : periodKey === "custom"
        ? "链接周期"
        : options.find((item) => item.key === periodKey)?.label ?? "自定义",
    start: period.start,
    end: period.end,
  };
  const rangeText = isAllScope ? "全部周期 · 不限时间" : periodLabel(period.start, period.end);
  // 提示与推荐问题跟随当前筛选周期：选「上周」就提示上周，
  // 不再固定写「本周」——否则点击推荐问题会悄悄覆盖用户选好的周期。
  const periodWord = isAllScope
    ? "全部周期"
    : periodKey === "custom"
      ? "这个周期"
      : currentSelection.label;
  const starterPromptList = [
    `${periodWord}有哪些卡点？`,
    `${periodWord}重点工作完成得怎么样？`,
    "哪些事项连续三周没有推进？",
    `按周报结构总结${periodWord}情况`,
  ];
  const departmentScopeText = departmentName
    ? `部门：${departmentName} · 仅检索该部门周报`
    : null;
  const historyMatchesDepartment = (item: AiHistoryItem) =>
    (item.department_id ?? null) === departmentId;

  const [question, setQuestion] = useState("");
  const [keyword, setKeyword] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState(createConversationId);
  const [activeHistoryId, setActiveHistoryId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<AiHistoryItem[]>([]);
  const [status, setStatus] = useState<AiStatusInfo | null>(null);
  const [usage, setUsage] = useState<AiUsageInfo | null>(null);
  const [scanning, setScanning] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [historyPreferenceReady, setHistoryPreferenceReady] = useState(false);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState<number | null>(null);
  const [deletingHistoryId, setDeletingHistoryId] = useState<number | null>(null);
  const [deletedHistory, setDeletedHistory] = useState<{
    item: AiHistoryItem;
    index: number;
  } | null>(null);
  const [historyActionError, setHistoryActionError] = useState<string | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memorySeed, setMemorySeed] = useState("");
  const undoTimerRef = useRef<number | null>(null);
  // 当前问答的取消控制器：上游卡住或用户离开时必须能中断，否则服务端会继续把
  // 剩余的检索步骤跑完（继续计费），界面也会永远停在加载态、无法重试。
  const searchAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    const linkedStart = params.get("start");
    const linkedEnd = params.get("end");
    if (linkedStart || linkedEnd) {
      if (!linkedStart || !linkedEnd) {
        setScopeError("链接中的统计周期不完整，请从部门详情重新进入。");
      } else {
        const linkedPeriod = options.find(
          (item) => item.start === linkedStart && item.end === linkedEnd
        );
        if (linkedPeriod) setPeriodKey(linkedPeriod.key);
        else {
          const validDate = (value: string) => {
            if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
            const parsed = new Date(`${value}T00:00:00Z`);
            return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
          };
          if (validDate(linkedStart) && validDate(linkedEnd) && linkedStart <= linkedEnd) {
            setCustomPeriod({ start: linkedStart, end: linkedEnd });
            setPeriodKey("custom");
          } else {
            setScopeError("链接中的统计周期无效，请从部门详情重新进入。");
          }
        }
      }
    }

    const rawDepartmentId = params.get("department_id");
    if (rawDepartmentId === null) {
      setScopeReady(true);
      return () => {
        cancelled = true;
      };
    }
    if (!/^[1-9]\d*$/.test(rawDepartmentId) || !Number.isSafeInteger(Number(rawDepartmentId))) {
      setScopeError("链接中的部门范围无效，请从部门详情重新进入。");
      setScopeReady(true);
      return () => {
        cancelled = true;
      };
    }

    const requestedDepartmentId = Number(rawDepartmentId);
    setDepartmentId(requestedDepartmentId);
    void apiGet<DepartmentItem[]>("/api/departments")
      .then((departments) => {
        if (cancelled) return;
        const department = departments.find((item) => item.id === requestedDepartmentId);
        if (!department) {
          setScopeError("所选部门不存在或当前账号无权访问，请从部门详情重新进入。");
          return;
        }
        setDepartmentName(department.name);
      })
      .catch(() => {
        if (!cancelled) setScopeError("暂时无法确认部门范围，请刷新后重试。");
      })
      .finally(() => {
        if (!cancelled) setScopeReady(true);
      });
    return () => {
      cancelled = true;
    };
  // Route context is read once on page entry; changing departments navigates from a department page.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    // 卸载或切页时中断在途问答：既避免「已卸载还 setState」，也停止继续烧 token。
    return () => {
      searchAbortRef.current?.abort();
      searchAbortRef.current = null;
    };
  }, []);

  function stopSearch() {
    searchAbortRef.current?.abort();
  }

  async function loadHistory() {
    try {
      const items = await apiGet<AiHistoryItem[]>("/api/ai/history?limit=30");
      setHistory(items);
      const current = items.find((item) => item.conversation_id === conversationId);
      if (current) setActiveHistoryId(current.id);
    } catch {
      /* 历史加载失败不影响当前检索 */
    }
  }

  async function loadGovernance() {
    try {
      setStatus(await apiGet<AiStatusInfo>("/api/ai/status"));
    } catch {
      /* 状态拿不到就退化成不显示，不阻塞主流程 */
    }
    try {
      setUsage(await apiGet<AiUsageInfo>("/api/ai/usage"));
    } catch {
      /* 同上 */
    }
  }

  // 补分析按钮已移到消费分析结果的页面（经营看板/部门/员工/洞察，共用
  // components/analysis-scan-button.tsx）；本页只保留底部「维护工具」折叠卡
  // 里的 force 重跑入口。

  useEffect(() => {
    loadHistory();
    loadGovernance();
  }, []);

  useEffect(() => {
    const stored = window.localStorage.getItem("weekly-report-ai-history-open");
    setHistoryOpen(
      stored === null
        ? window.matchMedia("(min-width: 1024px)").matches
        : stored !== "0"
    );
    setHistoryPreferenceReady(true);
  }, []);

  useEffect(() => {
    if (historyPreferenceReady) {
      window.localStorage.setItem(
        "weekly-report-ai-history-open",
        historyOpen ? "1" : "0"
      );
    }
  }, [historyOpen, historyPreferenceReady]);

  useEffect(
    () => () => {
      if (undoTimerRef.current !== null) {
        window.clearTimeout(undoTimerRef.current);
      }
    },
    []
  );

  useEffect(() => {
    if (!mobileHistoryOpen) return;
    const previousOverflow = document.body.style.overflow;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileHistoryOpen(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [mobileHistoryOpen]);

  function newChat() {
    setActiveHistoryId(null);
    setConversationId(createConversationId());
    setMessages([]);
    setQuestion("");
    setKeyword("");
    setError(null);
    setMobileHistoryOpen(false);
  }

  function openHistory(item: AiHistoryItem) {
    setActiveHistoryId(item.id);
    setConversationId(item.conversation_id ?? `history-${item.id}`);
    setError(null);
    setQuestion("");
    if (item.scope === ALL_SCOPE) {
      setPeriodKey(ALL_SCOPE);
    } else {
      const historyPeriod = item.period
        ? options.find(
            (option) =>
              option.start === item.period?.start && option.end === item.period?.end
          )
        : undefined;
      if (historyPeriod) {
        setPeriodKey(historyPeriod.key);
      } else if (item.period?.start && item.period.end) {
        setCustomPeriod(item.period);
        setPeriodKey("custom");
      }
    }
    setMobileHistoryOpen(false);
    const turns = (item.turns?.length ? item.turns : [item]).filter(
      historyMatchesDepartment
    );
    setMessages(
      turns.flatMap((turn) => [
        {
          id: `history-user-${turn.id}`,
          role: "user" as const,
          text: turn.question ?? "（无问题）",
        },
        {
          id: `history-assistant-${turn.id}`,
          role: "assistant" as const,
          text: turn.answer ?? "这次检索没有留下回答。",
          sources: turn.sources ?? [],
          validCitationIds: turn.valid_citation_ids ?? [],
          steps: turn.steps ?? [],
          escalations: turn.escalations ?? [],
          readCount: turn.read_count ?? turn.candidate_ids?.length,
          fullContext: turn.full_context,
          batchRead: turn.batch_read,
          truncatedCount: turn.truncated_report_ids?.length ?? 0,
          sourcesCited: (turn.valid_citation_ids?.length ?? 0) > 0,
          scope: (turn.scope === ALL_SCOPE ? ALL_SCOPE : "period") as "all" | "period",
          model: turn.model,
          analysisId: turn.analysis_id ?? turn.id,
          resolvedScope: turn.resolved_scope,
          period: turn.period,
          keywordCoverage: turn.keyword_coverage,
          coverageStatus: turn.coverage_status,
          coverageGaps: turn.coverage_gaps,
          evidenceType: turn.evidence_type,
          feedback: turn.feedback,
        },
      ])
    );
  }

  async function deleteHistory(item: AiHistoryItem) {
    setDeletingHistoryId(item.id);
    setHistoryActionError(null);
    try {
      await apiPatch<{ id: number; deleted: boolean }>(`/api/ai/history/${item.id}`, {
        deleted: true,
      });
      const index = history.findIndex((entry) => entry.id === item.id);
      setHistory((current) => current.filter((entry) => entry.id !== item.id));
      if (activeHistoryId === item.id) {
        newChat();
      }
      setPendingDeleteId(null);
      setDeletedHistory({ item, index: Math.max(index, 0) });
      if (undoTimerRef.current !== null) {
        window.clearTimeout(undoTimerRef.current);
      }
      undoTimerRef.current = window.setTimeout(() => {
        setDeletedHistory(null);
        undoTimerRef.current = null;
      }, 8000);
    } catch (e: unknown) {
      setHistoryActionError(apiErrorMessage(e, "删除聊天记录失败"));
    } finally {
      setDeletingHistoryId(null);
    }
  }

  async function undoDeleteHistory() {
    if (!deletedHistory) return;
    try {
      await apiPatch<{ id: number; deleted: boolean }>(
        `/api/ai/history/${deletedHistory.item.id}`,
        { deleted: false }
      );
      setHistory((current) =>
        [...current, deletedHistory.item].sort((a, b) => b.id - a.id)
      );
      setDeletedHistory(null);
      if (undoTimerRef.current !== null) {
        window.clearTimeout(undoTimerRef.current);
        undoTimerRef.current = null;
      }
    } catch (e: unknown) {
      setHistoryActionError(apiErrorMessage(e, "恢复聊天记录失败"));
    }
  }

  async function runSearch(
    nextQuestion = question,
    resolution?: AgentClarificationOption,
    resumeClarification = false,
  ) {
    if (!scopeReady || scopeError) {
      setError(scopeError ?? "正在确认部门与周期范围，请稍后再试。");
      return;
    }
    const q = nextQuestion.trim();
    if (!q) {
      setError("请先输入要问的问题");
      return;
    }
    // 同一时间只允许一轮问答：先中断上一轮（若有），再新开一个控制器。
    searchAbortRef.current?.abort();
    const controller = new AbortController();
    searchAbortRef.current = controller;
    setLoading(true);
    setError(null);
    setQuestion("");
    const assistantId = `assistant-${Date.now()}`;
    if (!resumeClarification) {
      setMessages((current) => [
        ...current,
        { id: `user-${Date.now()}`, role: "user", text: q },
        { id: assistantId, role: "assistant", text: "", steps: [], streaming: true },
      ]);
    } else {
      setMessages((current) => [
        ...current,
        { id: assistantId, role: "assistant", text: "", steps: [], streaming: true },
      ]);
    }

    const patch = (updater: (message: ChatMessage) => ChatMessage) =>
      setMessages((current) =>
        current.map((message) => (message.id === assistantId ? updater(message) : message))
      );

    const requestedDepartmentId = resolution?.department_id ?? departmentId;
    let terminalEventReceived = false;

    try {
      await apiPostStream<AgentEvent>(
        "/api/ai/agent/stream",
        {
          question: q,
          conversation_id: conversationId,
          scope: isAllScope ? ALL_SCOPE : "period",
          // 保留页面周期用于显示；问题中的明确周期由服务端优先解析。
          start: period.start,
          end: period.end,
          employee_id: resolution?.employee_id,
          department_id: requestedDepartmentId ?? undefined,
          // surface/dashboard_view 由后端按 department_id 推导，前端不重复传
          keyword: keyword.trim() || undefined,
        },
        (event) => {
          if (event.type === "delta") {
            patch((message) => ({ ...message, text: message.text + event.text }));
          } else if (event.type === "discard") {
            // 模型先出字又改成调工具：清掉这段草稿，避免留下半截答案
            patch((message) => ({ ...message, text: "" }));
          } else if (event.type === "step") {
            patch((message) => ({
              ...message,
              steps: [...(message.steps ?? []), event],
              progressText: event.tool === "read_batch" ? event.summary : "正在继续检索…",
            }));
          } else if (event.type === "progress") {
            patch((message) => ({ ...message, progressText: event.message ?? message.progressText }));
          } else if (event.type === "clarify") {
            terminalEventReceived = true;
            if (event.options.length === 0) setQuestion(q);
            patch((message) => ({
              ...message,
              text: "",
              clarification: { ...event, question: q },
              streaming: false,
            }));
          } else if (event.type === "done") {
            terminalEventReceived = true;
            const payload: AgentPayload = event.payload;
            if ((payload.department_id ?? null) !== requestedDepartmentId) {
              const message = "服务器返回的部门范围与本轮请求不一致，本次回答已拦截。";
              setScopeError(message);
              patch((item) => ({ ...item, error: message, streaming: false }));
              return;
            }
            // 本轮实际查询的周期与页面筛选不同时，让页面筛选器跟随实际周期，
            // 避免「页面显示上周、回答查的是本周」两个周期同屏打架。
            // payload.period 偏离页面周期的唯一来源是模型执行了问题中的日期
            // （问题时间词，或模型自行解析的日期）——都是问题意图驱动，同步是
            // 如实反映；范围自动放宽（escalation）不改写 period，不会被误同步。
            // 倒置区间（模型日期与页面范围交集为空）不构成有效周期，不同步。
            const actualPeriod = payload.period;
            if (
              actualPeriod &&
              actualPeriod.start <= actualPeriod.end &&
              (actualPeriod.start !== period.start || actualPeriod.end !== period.end)
            ) {
              const matched = options.find(
                (item) => item.start === actualPeriod.start && item.end === actualPeriod.end
              );
              if (matched) {
                setPeriodKey(matched.key);
                setCustomPeriod(null);
              } else {
                setCustomPeriod({ start: actualPeriod.start, end: actualPeriod.end });
                setPeriodKey("custom");
              }
            }
            if (payload.scope === ALL_SCOPE && !isAllScope) {
              // 用户在问题里要求查全部历史（如「总结所有周报」）：筛选器同步为「全部周期」。
              setPeriodKey(ALL_SCOPE);
              setCustomPeriod(null);
            }
            patch((message) => ({
              ...message,
              text: payload.answer,
              sources: payload.sources,
              validCitationIds: payload.valid_citation_ids,
              steps: payload.steps,
              escalations: payload.escalations,
              readCount: payload.read_count,
              fullContext: payload.full_context,
              batchRead: payload.batch_read,
              truncatedCount: payload.truncated_report_ids?.length ?? 0,
              sourcesCited: payload.sources_cited,
              model: payload.model,
              analysisId: payload.analysis_id,
              resolvedScope: payload.resolved_scope,
              period: payload.period,
              keywordCoverage: payload.keyword_coverage,
              coverageStatus: payload.coverage_status,
              coverageGaps: payload.coverage_gaps,
              evidenceType: payload.evidence_type,
              scope: payload.scope,
              streaming: false,
            }));
          } else if (event.type === "error") {
            terminalEventReceived = true;
            patch((message) => ({
              ...message,
              steps: event.steps ?? message.steps,
              error: event.message,
              analysisId: event.analysis_id,
              streaming: false,
            }));
          }
        }
      , { signal: controller.signal });
      if (!terminalEventReceived) {
        throw new ApiError(
          502,
          "连接在服务端确认完成前结束，本轮结果可能未保存。请重试；若重复出现，请联系管理员。"
        );
      }
      await loadHistory();
      loadGovernance();
    } catch (e: unknown) {
      // 499 = 调用方主动取消（用户点了停止/发起了新一轮），不是故障，
      // 不该弹红字错误；把已流出的部分答案保留下来即可。
      if (e instanceof ApiError && e.status === 499) {
        patch((message) => ({ ...message, streaming: false }));
      } else {
        setError(apiErrorMessage(e, "检索失败，请稍后重试"));
        patch((message) => ({
          ...message,
          error: apiErrorMessage(e, "检索失败"),
          streaming: false,
        }));
      }
    } finally {
      patch((message) => ({ ...message, streaming: false }));
      setLoading(false);
      // 只有自己仍是当前那一轮才清空引用，避免把后发起的一轮一起清掉。
      if (searchAbortRef.current === controller) searchAbortRef.current = null;
    }
  }

  async function runScan(force: boolean) {
    setScanning(true);
    setError(null);
    try {
      // 单次 POST 受 ai_scan_max_reports 上限约束；入队本身不调模型，真正的
      // 模型花费由 Worker 按队列顺序消化。这里连发直到排完，一次点击即可
      // 重跑整个周期。日常补分析在经营看板等消费页进行，本页只做维护重跑。
      for (let round = 0; round < 200; round++) {
        const data = await apiPost<ScanResult>(
          "/api/ai/scan",
          {
            start: period.start,
            end: period.end,
            force,
          },
          { timeoutMs: 30000 }
        );
        if (!data.limited || !data.remaining) break;
      }
      loadGovernance();
    } catch (e: unknown) {
      setError(apiErrorMessage(e, "分析入队失败，请稍后重试"));
    } finally {
      setScanning(false);
    }
  }

  function handleComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void runSearch();
    }
  }

  const aiState = status ? aiStateOf(status) : null;
  const aiStyle = aiState ? AI_STATE_STYLE[aiState] : null;
  const latestUserText = [...messages].reverse().find((message) => message.role === "user")?.text ?? "";
  const memorySeedCandidate = question.trim() || latestUserText;

  return (
    <div className="space-y-4">
      <PageHeader
        title={
          <span className="inline-flex items-center gap-2">
            <span className="flex h-8 w-8 items-center justify-center rounded-control bg-primary text-primary-foreground">
              <Bot className="h-4 w-4" />
            </span>
            周报智能体
          </span>
        }
        desc={
          departmentScopeText
            ? `${departmentScopeText}。回答会附可回看的原文来源。`
            : "问周报里的进展、卡点和风险，回答会附可回看的原文来源"
        }
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            {status && aiStyle && (
              <span
                title={aiState === "failed" ? status.last_error ?? undefined : undefined}
                className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs ${aiStyle.chip}`}
              >
                <span className={`h-1.5 w-1.5 rounded-full ${aiStyle.dot}`} />
                {aiStyle.label}
                {status.last_call_at && (
                  <span className="opacity-70">· {formatDateTime(status.last_call_at)}</span>
                )}
              </span>
            )}
            {/* 补分析按钮在经营看板/部门/员工/洞察页（components/analysis-scan-button.tsx）。
                对话走全量上下文读全部已同步原文，不依赖分析覆盖，这里不放按钮，
                避免被误读成对话的数据前提。 */}
            <PeriodPicker
              value={currentSelection}
              options={scopeTabs}
              syncUrl={false}
              onChange={(selection) => {
                if (selection.key === ALL_SCOPE) {
                  setPeriodKey(ALL_SCOPE);
                  setCustomPeriod(null);
                  return;
                }
                setPeriodKey(selection.key);
                if (selection.key === "custom" && selection.start && selection.end) {
                  setCustomPeriod({ start: selection.start, end: selection.end });
                } else {
                  setCustomPeriod(null);
                }
              }}
            />
          </div>
        }
      />

      {scopeError && (
        <div role="alert" className="rounded-control border border-destructive/30 bg-destructive-soft px-3 py-2 text-sm text-destructive">
          {scopeError}
        </div>
      )}
      {!scopeReady && !scopeError && (
        <p role="status" className="text-xs text-muted-foreground">
          正在确认部门与周期范围…
        </p>
      )}

      {status && !status.configured && (
        <div className="flex items-start gap-2 rounded-control bg-warning-soft px-3 py-2 text-sm text-warning">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">AI 暂不可用</p>
            <p className="mt-0.5 text-xs">{status.hint}</p>
          </div>
        </div>
      )}

      {status && aiState === "failed" && (
        <div className="flex items-start gap-2 rounded-control bg-destructive-soft px-3 py-2 text-sm text-destructive">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0">
            <p className="font-medium">模型配置存在，但最近一次调用失败了</p>
            <p className="mt-0.5 break-words text-xs text-destructive">{status.last_error}</p>
          </div>
        </div>
      )}

      <div
        className={cn(
          "grid min-h-0 gap-4 lg:h-[calc(100vh-190px)] lg:min-h-[620px]",
          historyOpen
            ? "lg:grid-cols-[260px_minmax(0,1fr)]"
            : "lg:grid-cols-[minmax(0,1fr)]"
        )}
      >
        {historyOpen && (
          <div className="hidden min-h-0 lg:block">
            <HistoryPanel
              history={scopeReady ? history.filter(historyMatchesDepartment) : []}
              activeHistoryId={activeHistoryId}
              rangeText={rangeText}
              keyword={keyword}
              pendingDeleteId={pendingDeleteId}
              deletingHistoryId={deletingHistoryId}
              onOpen={openHistory}
              onNewChat={newChat}
              onCollapse={() => setHistoryOpen(false)}
              onRequestDelete={(id) => {
                setPendingDeleteId(id);
                setHistoryActionError(null);
              }}
              onCancelDelete={() => setPendingDeleteId(null)}
              onConfirmDelete={(item) => void deleteHistory(item)}
            />
          </div>
        )}

        <Card className="flex h-[calc(100dvh-190px)] flex-col overflow-hidden lg:h-full lg:min-h-0">
          <div className="card-head card-head-line">
            <div className="flex min-w-0 items-center gap-2.5">
              {!historyOpen && (
                <button
                  type="button"
                  onClick={() => setHistoryOpen(true)}
                  title="显示聊天记录"
                  aria-label="显示聊天记录"
                  className="hidden h-8 w-8 shrink-0 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground lg:inline-flex"
                >
                  <PanelLeftOpen className="h-4 w-4" />
                </button>
              )}
              <button
                type="button"
                onClick={() => setMobileHistoryOpen(true)}
                title="显示聊天记录"
                aria-label="显示聊天记录"
                className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground lg:hidden"
              >
                <History className="h-4 w-4" />
              </button>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">
                  {activeHistoryId ? "历史对话" : messages.length ? "当前对话" : "开始一段对话"}
                </p>
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {departmentScopeText ? `${departmentScopeText} · ` : ""}{rangeText} · 仅检索已同步周报
                </p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              {/* 记忆的增删改都是写操作，只读成员不显示入口 */}
              <AdminOnly>
                <button
                  type="button"
                  onClick={() => {
                    setMemorySeed(memorySeedCandidate);
                    setMemoryOpen(true);
                  }}
                  className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
                  title={memorySeedCandidate ? "将最近的问题带入记忆表单" : "管理 Agent 长期记忆"}
                >
                  <Brain className="h-3.5 w-3.5" /> 记忆管理
                </button>
              </AdminOnly>
              {messages.length > 0 && (
                <button
                  type="button"
                  onClick={newChat}
                  className="text-xs text-muted-foreground transition-colors hover:text-foreground"
                >
                  清空当前对话
                </button>
              )}
            </div>
          </div>

          {deletedHistory && (
            <div className="flex items-center justify-between gap-3 border-b border-warning/30 bg-warning-soft px-4 py-2 text-xs text-warning sm:px-5">
              <span className="truncate">聊天记录已删除，AI 用量与审计记录仍保留。</span>
              <button
                type="button"
                onClick={() => void undoDeleteHistory()}
                className="inline-flex shrink-0 items-center gap-1 font-medium text-warning hover:underline"
              >
                <Undo2 className="h-3.5 w-3.5" /> 撤销
              </button>
            </div>
          )}

          {historyActionError && (
            <div className="flex items-center justify-between gap-3 border-b border-destructive/30 bg-destructive-soft px-4 py-2 text-xs text-destructive sm:px-5">
              <span className="truncate">{historyActionError}</span>
              <button
                type="button"
                onClick={() => setHistoryActionError(null)}
                className="shrink-0 font-medium hover:underline"
              >
                关闭
              </button>
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-y-auto bg-muted/60 px-4 py-6 sm:px-8">
            {messages.length === 0 && !loading ? (
              <div className="mx-auto flex min-h-[470px] max-w-2xl flex-col items-center justify-center text-center">
                <div className="flex h-14 w-14 items-center justify-center rounded-card bg-primary-soft text-primary">
                  <Sparkles className="h-7 w-7" />
                </div>
                <h2 className="mt-5 text-lg font-semibold">从一条周报问题开始</h2>
                <p className="mt-2 max-w-md text-sm leading-relaxed text-muted-foreground">
                  {isAllScope
                    ? "我会检索全部历史周报，再把结论和原文依据一起带回来；命中很多时只读最近若干篇，并说明读了多少。"
                    : "我会先查找当前周期的周报，再把结论和原文依据一起带回来。"}
                </p>
                <div className="mt-6 grid w-full gap-2 sm:grid-cols-2">
                  {starterPromptList.map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      onClick={() => void runSearch(prompt)}
                      className="rounded-control bg-muted/40 px-3.5 py-3 text-left text-xs font-medium transition-colors hover:bg-primary-soft/60"
                    >
                      <span className="mr-2 text-primary">↗</span>
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              <div className="mx-auto max-w-3xl space-y-6">
                {messages.map((message) => (
                  <ChatBubble
                    key={message.id}
                    message={message}
                    onClarify={(clarifiedQuestion, option, clarificationId) => {
                      setMessages((current) => current.filter((item) => item.id !== clarificationId));
                      void runSearch(clarifiedQuestion, option, true);
                    }}
                    onFeedback={(analysisId, feedback) => {
                      setMessages((current) => current.map((item) =>
                        item.analysisId === analysisId ? { ...item, feedback } : item
                      ));
                    }}
                  />
                ))}
                {loading && !messages.some((message) => message.streaming) && (
                  <div className="flex items-start gap-3">
                    <div className="mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-primary text-primary-foreground">
                      <Bot className="h-4 w-4" />
                    </div>
                    <div className="rounded-card rounded-tl-control border border-border bg-surface px-4 py-3 text-sm text-muted-foreground">
                      <span className="inline-flex items-center gap-2">
                        <span className="flex gap-1">
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:120ms]" />
                          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary [animation-delay:240ms]" />
                        </span>
                        正在回复…
                      </span>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="border-t border-border bg-surface p-3 sm:p-4">
            {error && (
              <p className="mb-3 rounded-control bg-destructive-soft px-3 py-2 text-xs text-destructive">
                {error}
              </p>
            )}
            <div className="rounded-card border border-border bg-muted/40 p-2.5 transition-[border-color,box-shadow] focus-within:border-primary/40 focus-within:shadow-focus">
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={handleComposerKeyDown}
                placeholder={`问问${periodWord}周报…（Enter 发送，Shift+Enter 换行）`}
                rows={2}
                disabled={loading || !scopeReady || !!scopeError}
                className="min-h-[52px] w-full resize-none border-0 bg-transparent px-2 py-1 text-sm leading-relaxed placeholder:text-muted-foreground focus:outline-none disabled:cursor-not-allowed"
              />
              <div className="flex flex-wrap items-center justify-between gap-2 px-1 pt-1">
                <input
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                  placeholder="关键词范围（任一命中，严格筛选）"
                  className="h-7 min-w-0 flex-1 border-0 bg-transparent px-2 text-xs text-muted-foreground placeholder:text-muted-foreground/70 focus:outline-none sm:max-w-xs"
                />
                <Button size="sm" onClick={() => void runSearch()} loading={loading} disabled={loading || !scopeReady || !!scopeError}>
                  <Send className="h-3.5 w-3.5" /> 发送
                </Button>
                {loading && (
                  <Button size="sm" variant="outline" onClick={stopSearch}>
                    <X className="h-3.5 w-3.5" /> 停止
                  </Button>
                )}
              </div>
            </div>
            <p className="mt-2 flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
              <Check className="h-3 w-3 text-success" />
              关键词按任一词严格筛选周报；无结果不会扩大范围。事实结论需有可回看的证据。
            </p>
          </div>
        </Card>
      </div>

      <MemoryManager
        open={memoryOpen}
        seed={memorySeed}
        onClose={() => {
          setMemoryOpen(false);
          setMemorySeed("");
        }}
      />

      {mobileHistoryOpen && (
        <div
          className="fixed inset-0 z-50 lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="聊天记录"
        >
          <div
            className="absolute inset-0 bg-foreground/40"
            onClick={() => setMobileHistoryOpen(false)}
            aria-hidden="true"
          />
          <div className="absolute inset-y-0 left-0 w-[min(88vw,340px)] p-2">
            <HistoryPanel
              mode="mobile"
              history={scopeReady ? history.filter(historyMatchesDepartment) : []}
              activeHistoryId={activeHistoryId}
              rangeText={rangeText}
              keyword={keyword}
              pendingDeleteId={pendingDeleteId}
              deletingHistoryId={deletingHistoryId}
              onOpen={openHistory}
              onNewChat={newChat}
              onCollapse={() => setMobileHistoryOpen(false)}
              onRequestDelete={(id) => {
                setPendingDeleteId(id);
                setHistoryActionError(null);
              }}
              onCancelDelete={() => setPendingDeleteId(null)}
              onConfirmDelete={(item) => void deleteHistory(item)}
            />
          </div>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="overflow-hidden">
          <details>
            <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Activity className="h-4 w-4 text-primary" /> AI 用量账本
              </span>
              <span className="text-xs text-muted-foreground">仅展示 token 统计 ▾</span>
            </summary>
            <div className="border-t border-border px-4 py-3">
              {!usage || usage.total_calls === 0 ? (
                <p className="text-xs text-muted-foreground">还没有 AI 调用记录。</p>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Metric label="累计调用" value={usage.total_calls} />
                    <Metric label="成功" value={usage.success} />
                    <Metric label="失败" value={usage.failed} />
                    <Metric label="总 token" value={usage.total_tokens} />
                  </div>
                  {usage.by_type.length > 0 && (
                    <p className="mt-3 border-t border-border pt-3 text-xs text-muted-foreground">
                      {usage.by_type
                        .map(
                          (item) =>
                            `${item.analysis_type}：${item.calls} 次 / ${item.prompt_tokens + item.completion_tokens} token`
                        )
                        .join(" · ")}
                    </p>
                  )}
                </>
              )}
            </div>
          </details>
        </Card>

        <Card className="overflow-hidden">
          <details>
            <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3">
              <span className="flex items-center gap-2 text-sm font-medium">
                <Zap className="h-4 w-4 text-primary" /> 维护工具
              </span>
              <span className="text-xs text-muted-foreground">重跑当前周期 ▾</span>
            </summary>
            <div className="border-t border-border px-4 py-3">
              <p className="text-xs leading-relaxed text-muted-foreground">
                {isAllScope
                  ? "需要指定周期，先切到具体周期再用。"
                  : "把当前周期已分析过的周报也重新入队（提示词升级后的修复手段），会重新消耗模型调用；日常补分析在经营看板、部门/员工详情与管理洞察页的「补分析」按钮进行。"}
              </p>
              <div className="mt-3">
                <AdminOnly>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void runScan(true)}
                    loading={scanning}
                    disabled={isAllScope}
                  >
                    全部重新入队
                  </Button>
                </AdminOnly>
              </div>
            </div>
          </details>
        </Card>
      </div>
    </div>
  );
}
