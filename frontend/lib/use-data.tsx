"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiGet, invalidateApiCache, readApiCache } from "@/lib/api";
import { periodOptions, syncPeriodToUrl } from "@/lib/format";
import { cn } from "@/lib/cn";

interface AsyncState<T> {
  data: T | null;
  error: string | null;
  /** 首次加载：还没有任何数据可展示，页面据此显示骨架屏。 */
  loading: boolean;
  /** 已有数据、正在后台刷新：页面应继续展示旧数据，不要退回骨架屏。 */
  refreshing: boolean;
}

/**
 * 简易数据请求 hook：deps 变化自动重取，暴露 reload 手动刷新。
 *
 * **首屏 `loading` 与后台刷新 `refreshing` 必须分开**，两者语义不同：
 * - `deps` 变化（切周期、改筛选）意味着**要展示的是一份新数据**，此时显示骨架屏是对的
 *   —— 若沿用旧数据，用户会看到上周的数字配上本周的标题。
 * - `reload()`（轮询 / 手动刷新）展示的仍是同一份数据，必须保留旧内容后台更新：
 *   首页每 4 秒轮询一次，若把 `loading` 置回 true，整块内容每 4 秒变回骨架屏，
 *   趋势图被反复销毁重建、正在看的列表一直闪。
 *
 * `cacheKey` 传入本请求的 GET 缓存键（`apiCacheKey(path, params)`）后，挂载时会
 * **同步**取用尚未过期的缓存先渲染，再后台取新数据 —— 这是「切走再切回」不再
 * 白屏几秒的关键：有缓存时首帧就是内容而不是骨架屏。传 `reload()` 会先让该键失效，
 * 所以轮询/手动刷新永远走网络、不会读到缓存。
 *
 * `mutate(updater)` 做**本地合并**：适用于「写操作只改变了已返回数据的一小块」
 * 的场景（如判定提交后更新回显索引）。它不重取、不发请求，仅更新内存里的数据 ——
 * 一个只影响展示状态的按钮点击不值得触发整页重算。
 */
export function useAsyncData<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  options: { cacheKey?: string } = {}
): AsyncState<T> & { reload: () => void; mutate: (updater: (current: T | null) => T | null) => void } {
  const cacheKey = options.cacheKey;
  const [state, setState] = useState<AsyncState<T>>(() => {
    const seeded = cacheKey ? readApiCache<T>(cacheKey) : undefined;
    return {
      data: seeded ?? null,
      error: null,
      // 命中缓存 = 已经有内容可看，首屏不该再显示骨架屏（后台仍在取新数据）
      loading: seeded === undefined,
      refreshing: seeded !== undefined,
    };
  });
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // 上一次请求的 deps 指纹：用来区分「deps 变了」与「只是 reload」。
  // 注意：比较走 JSON.stringify，**deps 只放原始值**（数字/字符串/布尔/null）。
  // 传对象字面量也能工作，但依赖键序，属于未定义行为——需要对象条件时
  // 先在外面算成原始值再放进来（参见 reports 页的 historyAllActive 写法）。
  const depsKey = JSON.stringify(deps ?? []);
  const lastDepsKeyRef = useRef<string | null>(null);

  // 轮询与手动刷新必须拿到**新**数据，所以先让这个键失效再取；
  // 否则 30 秒 TTL 内每次 reload 都会被自己的缓存挡住，等于不刷新。
  const reload = useCallback(() => {
    if (cacheKey) invalidateApiCache(cacheKey);
    setTick((t) => t + 1);
  }, [cacheKey]);

  // 本地合并：不动缓存、不发请求，只更新当前持有的数据。
  // deps 变化（重取）后这份合并结果会被新数据自然覆盖 —— 合并只对「当前这份」负责。
  const mutate = useCallback((updater: (current: T | null) => T | null) => {
    setState((s) => ({ ...s, data: updater(s.data) }));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const depsChanged = lastDepsKeyRef.current !== depsKey;
    lastDepsKeyRef.current = depsKey;
    // deps 变化（切周期/改筛选）时先看这份数据是否刚取过：命中就直接用，
    // 省掉一次骨架屏闪烁；未命中才退回骨架屏。
    const seeded =
      depsChanged && cacheKey ? readApiCache<T>(cacheKey) : undefined;
    setState((s) => {
      if (seeded !== undefined) {
        return { data: seeded, error: null, loading: false, refreshing: true };
      }
      // deps 变化 = 新数据的骨架屏；reload = 保留旧数据的后台刷新。
      const isFirstLoad = s.data === null;
      return {
        ...s,
        loading: isFirstLoad || depsChanged,
        refreshing: !isFirstLoad && !depsChanged,
        error: null,
      };
    });
    fetcherRef
      .current()
      .then((data) => {
        if (!cancelled) {
          setState({ data, error: null, loading: false, refreshing: false });
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setState((s) => ({
            // 刷新失败保留上一次数据：单次轮询失败不该把已经看到的页面清空。
            data: s.data,
            error:
              error instanceof ApiError ? error.message : "加载失败，请稍后重试",
            loading: false,
            refreshing: false,
          }));
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { ...state, reload, mutate };
}

export interface PeriodPickerOption {
  key: string;
  label: string;
  start: string;
  end: string;
}

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * 统一周期选择器：快捷档 chips + 自定义起止日期，全站唯一入口（替代 PeriodTabs）。
 *
 * - `value` 是完整周期对象（key/label/start/end），`onChange` 回传新对象 ——
 *   自定义区间不再是只能靠链接进入的隐藏功能（刘总 2026-10-09 反馈的直接根源）。
 * - 选中任何带区间的档位都会把 start/end 写进地址栏（replaceState，不触发导航），
 *   复制链接发给别人打开看到的是同一段数据。页面自己管理 URL 的（reports）传 `syncUrl={false}`。
 * - 快捷档高亮按 key 匹配；自定义区间高亮「自定义」chip 本身。
 */
export function PeriodPicker({
  value,
  onChange,
  options: customOptions,
  allowCustom = true,
  syncUrl = true,
}: {
  value: PeriodPickerOption;
  onChange: (period: PeriodPickerOption) => void;
  /** 覆盖默认快选档；纯语义档（如「全部周期」）的 start/end 填当前区间占位即可。 */
  options?: PeriodPickerOption[];
  allowCustom?: boolean;
  syncUrl?: boolean;
}) {
  const options = customOptions ?? periodOptions();
  const [customOpen, setCustomOpen] = useState(false);
  const [draftStart, setDraftStart] = useState("");
  const [draftEnd, setDraftEnd] = useState("");

  const openCustom = () => {
    setDraftStart(value.start ?? "");
    setDraftEnd(value.end ?? "");
    setCustomOpen(true);
  };

  const draftValid =
    ISO_DATE_RE.test(draftStart) && ISO_DATE_RE.test(draftEnd) && draftStart <= draftEnd;

  const select = (option: PeriodPickerOption) => {
    if (syncUrl && option.start && option.end) syncPeriodToUrl(option.start, option.end);
    onChange(option);
  };

  const applyCustom = () => {
    if (!draftValid) return;
    select({ key: "custom", label: "自定义", start: draftStart, end: draftEnd });
    setCustomOpen(false);
  };

  return (
    <div className="inline-flex shrink-0 flex-col items-end gap-1.5">
      <div className="inline-flex items-center rounded-pill border border-input bg-surface p-1">
        {options.map((option) => (
          <button
            key={option.key}
            onClick={() => {
              setCustomOpen(false);
              select(option);
            }}
            aria-pressed={value.key === option.key}
            className={cn(
              "rounded-pill px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              value.key === option.key
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            {option.label}
          </button>
        ))}
        {allowCustom && (
          <button
            onClick={() => (customOpen ? setCustomOpen(false) : openCustom())}
            aria-expanded={customOpen}
            className={cn(
              "rounded-pill px-3 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              value.key === "custom" && !customOpen
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            自定义
          </button>
        )}
      </div>
      {allowCustom && customOpen && (
        <div className="flex flex-wrap items-end gap-2 rounded-control border border-border bg-surface px-3 py-2.5">
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            开始日期
            <input
              type="date"
              value={draftStart}
              max={draftEnd || undefined}
              onChange={(event) => setDraftStart(event.target.value)}
              className="rounded-control border border-input bg-background px-2 py-1 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <span className="pb-1.5 text-xs text-muted-foreground">至</span>
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            结束日期
            <input
              type="date"
              value={draftEnd}
              min={draftStart || undefined}
              onChange={(event) => setDraftEnd(event.target.value)}
              className="rounded-control border border-input bg-background px-2 py-1 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </label>
          <button
            onClick={applyCustom}
            disabled={!draftValid}
            className="rounded-control bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            应用
          </button>
          {!draftValid && (draftStart || draftEnd) && (
            <span className="pb-1.5 text-[11px] text-destructive">
              {draftStart && draftEnd ? "开始日期不能晚于结束日期" : "请选择完整起止日期"}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export { apiGet };
