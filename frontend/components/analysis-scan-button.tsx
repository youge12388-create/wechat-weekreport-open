"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Zap } from "lucide-react";

import { AdminOnly } from "@/components/admin-only";
import { Button } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";

interface CoverageInfo {
  reports_total: number;
  analyzed_reports: number;
  rate: number;
  remaining: number;
  queue: { pending: number; running: number; failed: number };
}

interface ScanResult {
  queued: number;
  running: number;
  limited?: boolean;
  remaining?: number;
}

/**
 * 「补分析」小按钮：把指定周期内未做结构化分析的周报加入后台队列。
 *
 * 放在消费分析结果的页面（经营看板、部门/员工/洞察）的覆盖率提示旁边，
 * 就地补跑，不再跳去别的页面。一次点击自动连发 POST 直到排完（单次
 * POST 受 ai_scan_max_reports 上限约束，入队本身不调模型，真正的模型
 * 花费由 Worker 按队列顺序消化）；入队后每 4 秒轮询覆盖率，队列消化完
 * 或补齐即停，并通过 onChanged 通知宿主刷新页面数据 —— 只在分析数真的
 * 变化后触发一次，避免重负载页面被轮询拖着反复重算。
 *
 * 取不到覆盖率（只读成员被中间件拦截、接口暂时失败）时按钮整体隐藏，
 * 不阻塞宿主页面。
 */
export function AnalysisScanButton({
  start,
  end,
  departmentId,
  employeeId,
  onChanged,
  className,
  remainingOverride,
  totalOverride,
}: {
  start: string;
  end: string;
  departmentId?: number | null;
  employeeId?: number | null;
  /** 分析数量发生变化并停止轮询后回调（宿主借此刷新页面数据）。 */
  onChanged?: () => void;
  className?: string;
  /** A caller with a stricter analysis check can keep the action available. */
  remainingOverride?: number;
  /** Keep the displayed count aligned with that stricter check as well. */
  totalOverride?: number;
}) {
  const [coverage, setCoverage] = useState<CoverageInfo | null>(null);
  const [scanning, setScanning] = useState(false);
  const [polling, setPolling] = useState(false);
  const [enqueueFailed, setEnqueueFailed] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const lastAnalyzedRef = useRef<number | null>(null);
  const dirtyRef = useRef(false);
  const queuedRef = useRef(false);
  const remainingOverrideRef = useRef(remainingOverride);
  remainingOverrideRef.current = remainingOverride;
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  const queueBusy =
    !!coverage && coverage.queue.pending + coverage.queue.running > 0;

  const load = useCallback(async () => {
    const params = new URLSearchParams({ start, end });
    if (departmentId != null) params.set("department_id", String(departmentId));
    if (employeeId != null) params.set("employee_id", String(employeeId));
    try {
      const data = await apiGet<CoverageInfo>(`/api/ai/coverage?${params.toString()}`);
      if (
        lastAnalyzedRef.current !== null &&
        data.analyzed_reports !== lastAnalyzedRef.current
      ) {
        dirtyRef.current = true;
      }
      lastAnalyzedRef.current = data.analyzed_reports;
      setCoverage(data);
      if ((remainingOverrideRef.current ?? data.remaining) === 0 || data.queue.pending + data.queue.running === 0) {
        setPolling(false);
        if (dirtyRef.current || queuedRef.current) {
          dirtyRef.current = false;
          queuedRef.current = false;
          onChangedRef.current?.();
        }
      } else {
        setPolling(true);
      }
    } catch {
      setCoverage(null);
      setPolling(false);
    }
  }, [start, end, departmentId, employeeId]);

  useEffect(() => {
    lastAnalyzedRef.current = null;
    dirtyRef.current = false;
    queuedRef.current = false;
    setCoverage(null);
    void load();
  }, [load]);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      void load();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [polling, load]);

  const runScan = useCallback(async () => {
    setScanning(true);
    setEnqueueFailed(false);
    setFeedback(null);
    try {
      let queued = 0;
      let running = 0;
      for (let round = 0; round < 200; round++) {
        const data = await apiPost<ScanResult>("/api/ai/scan", {
          start,
          end,
          department_id: departmentId ?? undefined,
          employee_id: employeeId ?? undefined,
          force: false,
        });
        queued += data.queued ?? 0;
        running += data.running ?? 0;
        if (data.limited && data.remaining && !data.queued) break;
        if (!data.limited || !data.remaining) break;
      }
      queuedRef.current = queued > 0;
      setFeedback(queued > 0
        ? `已加入后台分析队列 ${queued} 篇，完成后会自动刷新。`
        : running > 0
          ? `已有 ${running} 篇正在分析，完成后会自动刷新。`
          : "没有新任务入队，请刷新页面后重试。");
      await load();
    } catch (error) {
      setEnqueueFailed(true);
      setFeedback(`入队失败：${error instanceof Error ? error.message : "请稍后重试"}`);
    } finally {
      setScanning(false);
    }
  }, [start, end, departmentId, employeeId, load]);

  if (!coverage || coverage.reports_total === 0 || (remainingOverride ?? coverage.remaining) === 0) {
    return null;
  }

  const displayTotal = totalOverride ?? coverage.reports_total;
  const displayRemaining = remainingOverride ?? coverage.remaining;
  const displayAnalyzed = Math.max(0, displayTotal - displayRemaining);
  const title = enqueueFailed
    ? "入队失败，请稍后重试"
    : queueBusy
      ? `后台分析中（${coverage.queue.failed} 篇失败），当前确认已完成 ${displayAnalyzed}/${displayTotal} 篇`
      : `本周期已完成 ${displayAnalyzed}/${displayTotal} 篇，点击把剩余周报加入分析队列（结果供本页结论使用）`;

  return (
    <AdminOnly>
      <div className="flex flex-col items-center gap-1">
        <Button
          variant="outline"
          size="sm"
          onClick={() => void runScan()}
          loading={scanning}
          disabled={queueBusy}
          title={title}
          className={
            className ?? "border-warning/40 bg-warning-soft text-warning hover:bg-warning/15 hover:text-warning"
          }
        >
          <Zap className="h-3.5 w-3.5" />
          {queueBusy
            ? `分析中 ${displayAnalyzed}/${displayTotal}`
            : `补分析 ${displayRemaining} 篇`}
        </Button>
        {feedback && <span role={enqueueFailed ? "alert" : "status"} className="text-xs text-muted-foreground">{feedback}</span>}
      </div>
    </AdminOnly>
  );
}
