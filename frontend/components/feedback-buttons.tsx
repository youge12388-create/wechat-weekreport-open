"use client";

/**
 * 结论效度判定按钮：说得对 / 说错了 / 说不清。
 *
 * **为什么需要它**：看板的自动化测试只能证明「代码按规则跑」，证明不了「规则符合
 * 现实」。审计中实测到的三类错误（同一任务被算成两条主线、「状态不一致」把两件
 * 不同的事判成矛盾、空资源需求占掉首屏名额）在 47 项测试里全绿 —— 它们测的是规则
 * 执行，不是规则对错。没有人工表态，就永远不知道这块看板准不准、准在哪。
 *
 * 三个刻意的取舍：
 * - **「说不清」是有效答案**：证据不足的结论既不该硬判准确，也不该算作错误。
 *   少了它，人会为了「填点什么」而在两者里随便选，脏数据比没数据更糟。
 * - **判过的状态要显示出来**：否则每次打开都得重新判断一遍，没人会持续用。
 * - **不阻断点击**：按钮在可点行内部，必须 `stopPropagation`，否则会连带打开抽屉。
 */

import { useState } from "react";

import { cn } from "@/lib/cn";
import type { OperatingFeedbackEntry, OperatingFeedbackModule } from "@/lib/types";

const OPTIONS: Array<{ verdict: "correct" | "wrong" | "unclear"; label: string; title: string }> = [
  { verdict: "correct", label: "准确", title: "这条结论说的是事实，值得跟进" },
  { verdict: "wrong", label: "不准确", title: "这条结论与事实不符或指向了错的部门" },
  { verdict: "unclear", label: "说不清", title: "原文证据不足以判断对错" },
];

export function FeedbackButtons({
  module,
  itemKey,
  current,
  /** 同一条结论的其他判定（用于显示「也有人说…」）。 */
  others,
  onVerdict,
  busy,
  className,
}: {
  module: OperatingFeedbackModule;
  /**
   * 结论的**业务键**（信号标题 / 瓶颈对象名 / 项目名），不是完整指纹。
   *
   * 这里刻意只收裸键：完整指纹（模块 + 周期 + 键）由后端 `build_signal_key` 拼装，
   * 前端若把自己拼好的完整指纹再当成键传回去，后端会再拼一次周期 ——
   * 实测得到 `attention|2026-09-14|2026-09-20|attention|2026-09-14|...` 这种双重指纹，
   * 于是回显索引永远对不上、页面看不到「已判定」。
   */
  itemKey: string;
  current: OperatingFeedbackEntry | null;
  others?: OperatingFeedbackEntry[];
  onVerdict: (
    module: OperatingFeedbackModule,
    itemKey: string,
    verdict: "correct" | "wrong" | "unclear",
    reason?: string
  ) => void;
  busy?: boolean;
  className?: string;
}) {
  const [asking, setAsking] = useState<null | "wrong">(null);
  const [reason, setReason] = useState("");

  function choose(verdict: "correct" | "wrong" | "unclear") {
    if (verdict === "wrong") {
      // 判错时先问一句原因：它是改进算法最有价值的输入。
      // 但仍允许直接跳过 —— 强制填写会让人干脆不判。
      setAsking("wrong");
      return;
    }
    onVerdict(module, itemKey, verdict);
  }

  function submitWrong() {
    onVerdict(module, itemKey, "wrong", reason.trim() || undefined);
    setAsking(null);
    setReason("");
  }

  const disagreeing = (others ?? []).filter(
    (entry) => current && entry.verdict !== current.verdict
  );

  return (
    <span
      className={cn("inline-flex flex-wrap items-center gap-1", className)}
      // 行本身可能是一个可点按钮（打开详情抽屉），判定不应连带触发它。
      onClick={(event) => event.stopPropagation()}
    >
      {asking === "wrong" ? (
        <>
          <input
            autoFocus
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) submitWrong();
              if (event.key === "Escape") setAsking(null);
            }}
            placeholder="哪里不对？（可留空）"
            className="h-6 w-44 rounded-control border border-input bg-surface px-2 text-[11px] focus-visible:border-ring focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <button
            type="button"
            onClick={submitWrong}
            className="rounded-pill bg-primary px-2 py-0.5 text-[11px] text-white transition-colors hover:opacity-90"
          >
            提交
          </button>
          <button
            type="button"
            onClick={() => setAsking(null)}
            className="rounded-pill px-1.5 py-0.5 text-[11px] text-muted-foreground transition-colors hover:bg-muted"
          >
            取消
          </button>
        </>
      ) : (
        <>
          {OPTIONS.map((option) => {
            const active = current?.verdict === option.verdict;
            return (
              <button
                key={option.verdict}
                type="button"
                title={option.title}
                disabled={busy}
                onClick={() => choose(option.verdict)}
                className={cn(
                  "rounded-pill px-1.5 py-0.5 text-[11px] transition-colors disabled:opacity-50",
                  active
                    ? option.verdict === "wrong"
                      ? "bg-destructive-soft font-medium text-destructive"
                      : option.verdict === "correct"
                        ? "bg-success-soft font-medium text-success"
                        : "bg-muted font-medium text-muted-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground"
                )}
              >
                {option.label}
              </button>
            );
          })}
          {current ? (
            <span className="text-[11px] text-muted-foreground">
              已判定{current.reason ? `：${current.reason}` : ""}
            </span>
          ) : null}
          {disagreeing.length ? (
            <span className="text-[11px] text-warning">
              另有 {disagreeing.length} 人判定不同
            </span>
          ) : null}
        </>
      )}
    </span>
  );
}

/** 结论指纹的构造规则必须与后端 `build_signal_key` 一致。 */
export function signalKeyOf(module: string, start: string, end: string, key: string): string {
  return `${module}|${start}|${end}|${key}`;
}
