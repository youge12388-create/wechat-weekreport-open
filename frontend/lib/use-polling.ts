"use client";

import * as React from "react";

/**
 * 任务进度轮询 hook：条件触发 + 页面不可见暂停 + 可选最大轮询时长兜底。
 *
 * 替代手写的 `setInterval(() => reload(), 4000)` 模式，补上三条此前缺失的护栏：
 *
 * - **可见性门控**：标签页切到后台时暂停请求，`visibilitychange` 回到前台时
 *   立即补刷一次。后台标签页没有人在看，持续打接口是纯浪费的服务器负载。
 * - **最大时长兜底（可选）**：后台任务卡死（如 Worker 挂掉、任务永远停在
 *   pending）时，轮询满 `maxDurationMs` 自动停止并调用 `onGiveUp`，让界面
 *   有机会提示「任务长时间未完成」—— 而不是永远「生成中」+ 无限轮询。
 * - **回调经 ref 转发**：callback 的身份变化（如 `pollStatus` 随周期重建）
 *   不会重启轮询定时器，下一次 tick 自然用上新回调；只有 `enabled`、
 *   `intervalMs`、`maxDurationMs` 变化才会重建定时器。
 *
 * 与原手写模式一致的部分：`enabled` 为 false 时不发任何请求；卸载时清理。
 * 注意：本 hook **不会**在启动时立即执行一次回调 —— 首次数据由页面自己的
 * `useAsyncData` 负责，轮询只负责「之后有没有新进展」。
 */
export function usePolling(
  callback: () => void,
  intervalMs: number,
  options: { enabled: boolean; maxDurationMs?: number; onGiveUp?: () => void }
): void {
  const { enabled, maxDurationMs, onGiveUp } = options;
  const callbackRef = React.useRef(callback);
  callbackRef.current = callback;
  const giveUpRef = React.useRef(onGiveUp);
  giveUpRef.current = onGiveUp;

  React.useEffect(() => {
    if (!enabled) return;

    const startedAt = Date.now();
    let timer: number | undefined;
    let stopped = false;

    const stop = () => {
      if (stopped) return;
      stopped = true;
      if (timer !== undefined) {
        window.clearInterval(timer);
        timer = undefined;
      }
      document.removeEventListener("visibilitychange", handleVisibility);
    };

    const handleVisibility = () => {
      // 切回前台立即补刷一次，不必等下一个周期；切走时不做任何事，
      // 定时器保留（空转的成本可以忽略），请求统一在 tick 里被跳过。
      if (document.visibilityState === "visible") callbackRef.current();
    };

    const tick = () => {
      if (document.visibilityState === "hidden") return;
      if (maxDurationMs !== undefined && Date.now() - startedAt > maxDurationMs) {
        stop();
        giveUpRef.current?.();
        return;
      }
      callbackRef.current();
    };

    timer = window.setInterval(tick, intervalMs);
    document.addEventListener("visibilitychange", handleVisibility);
    return stop;
  }, [enabled, intervalMs, maxDurationMs]);
}

/** 默认的「任务卡死」判定时长：超过后轮询放弃并提示用户。 */
export const TASK_POLL_MAX_MS = 5 * 60_000;
