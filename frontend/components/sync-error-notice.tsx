"use client";

import { useState } from "react";
import { AlertTriangle, ChevronDown, CheckCircle2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatDateTime } from "@/lib/format";
import type { SyncErrorDetail } from "@/lib/types";

/** 同步任务类型 -> 中文名，避免页面上出现 weekly_reports 这类内部代号。 */
const TYPE_LABELS: Record<string, string> = {
  weekly_reports: "周报",
  departments: "部门",
  employees: "员工",
};

/**
 * 同步失败提示：一句话结论 + 处理建议 + 可展开的原始报文。
 *
 * 后端已经把中英混排的企微报文整理成 summary/hint，这里只负责排版。
 * 原始报文默认折叠 —— 它又长又含 hint 链接，铺满首页会把关键信息挤没。
 * 若该失败之后已经成功过（last_error_resolved），显示为已恢复的浅色提示，
 * 而不是一直挂着的红色告警。
 */
export function SyncErrorNotice({
  detail,
  resolved = false,
  variant = "inline",
  className,
}: {
  detail: SyncErrorDetail | null;
  resolved?: boolean;
  /** inline = 首页状态行里的一段；block = 设置页里的整块提示。 */
  variant?: "inline" | "block";
  className?: string;
}) {
  const [open, setOpen] = useState(false);

  if (resolved && !detail) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 text-xs text-muted-foreground",
          className
        )}
      >
        <CheckCircle2 className="h-3.5 w-3.5 text-success" />
        历史同步失败已恢复
      </span>
    );
  }
  if (!detail) return null;

  const typeLabel = TYPE_LABELS[detail.type] ?? detail.type;
  const when = formatDateTime(detail.at);
  const hint = detail.hint;

  if (variant === "inline") {
    return (
      <span className={cn("inline-flex flex-wrap items-baseline gap-x-1.5", className)}>
        <span className="inline-flex items-center gap-1 font-medium text-destructive">
          <AlertTriangle className="h-3.5 w-3.5" />
          {typeLabel}同步失败
        </span>
        <span className="text-foreground">{detail.summary}</span>
        {hint && <span className="text-muted-foreground">处理：{hint}</span>}
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="inline-flex items-center gap-0.5 text-muted-foreground underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
        >
          技术细节
          <ChevronDown
            className={cn("h-3 w-3 transition-transform", open && "rotate-180")}
          />
        </button>
        {open && (
          <span className="mt-1 block w-full whitespace-pre-wrap break-all rounded-control bg-muted/60 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
            {when && <span className="block">时间：{when}</span>}
            {detail.raw}
          </span>
        )}
      </span>
    );
  }

  return (
    <div
      className={cn(
        "border-t border-destructive/30 bg-destructive-soft px-5 py-3.5",
        className
      )}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-destructive">
          <AlertTriangle className="h-4 w-4" />
          {detail.title}
        </span>
        <span className="text-xs text-destructive/80">
          {typeLabel}同步 · {when}
        </span>
      </div>
      <p className="mt-1.5 text-sm leading-relaxed text-foreground">
        {detail.summary}
      </p>
      {hint && (
        <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
          处理：{hint}
        </p>
      )}
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
      >
        {open ? "收起技术细节" : "查看技术细节"}
        <ChevronDown
          className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-control bg-surface px-3 py-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">
          {detail.raw}
        </pre>
      )}
    </div>
  );
}
