"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
// **按需引入，不要整包 `import * as echarts from "echarts"`**：整包会把所有图表类型与
// 组件一起打进首屏（全站只用到折线与环形两种图），这是页面加载慢的一块。
// 新增图表类型时在这里补注册，页面代码不受影响。
import * as echarts from "echarts/core";
// BarChart 必须注册：员工页「工作与质量趋势」用 barSeries（type: "bar"）。
// 未注册时 setOption 会在 Scheduler 里抛 `getProgressive` of undefined，
// 之前被「等待分析」空状态挡住几乎不渲染，补分析可用后才暴露。
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { useTheme } from "@/components/theme-provider";
import { chartBase, chartColorsForTheme, chartSeriesOrder, type ChartColorSet } from "@/lib/chart-theme";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TooltipComponent,
  CanvasRenderer,
]);

/** 把主题的基础骨架与页面传入的 option 合并；页面传的字段优先。 */
function mergeBase(option: echarts.EChartsCoreOption, colors: ChartColorSet): echarts.EChartsCoreOption {
  const base = chartBase(colors);
  return {
    ...base,
    color: chartSeriesOrder(colors),
    ...option,
    /* 网格与图例是骨架，页面若没显式覆盖就沿用主题，避免每个页面各写一套边距 */
    grid: { ...(base.grid as object), ...((option.grid as object) ?? {}) },
    tooltip: { ...(base.tooltip as object), ...((option.tooltip as object) ?? {}) },
    xAxis: option.xAxis ?? base.xAxis,
    yAxis: option.yAxis ?? base.yAxis,
  };
}

export default function Chart({
  option,
  height = 260,
  className,
  respectReducedMotion = true,
  onReady,
}: {
  option: echarts.EChartsCoreOption;
  height?: number;
  className?: string;
  respectReducedMotion?: boolean;
  /**
   * 实例创建后回调一次，供页面做 ECharts 原生交互（如 dispatchAction 高亮、
   * 绑定 mouseover）。页面需要「图例悬停→扇区高亮」这类联动时用它，
   * 不必把实例暴露成全局状态。回调只在挂载时调用，之后改引用不会重建实例。
   */
  onReady?: (chart: echarts.ECharts) => void;
}) {
  const { theme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const [reduceMotion, setReduceMotion] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
  const colors = useMemo(() => chartColorsForTheme(theme), [theme]);
  /* 回调存进 ref：页面通常传内联函数，直接进依赖会让实例反复销毁重建 */
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const merged = useMemo(() => mergeBase(option, colors), [colors, option]);
  const renderedOption = useMemo(
    () => respectReducedMotion && reduceMotion
      ? { ...merged, animation: false, animationDuration: 0, animationDurationUpdate: 0 }
      : merged,
    [merged, reduceMotion, respectReducedMotion]
  );

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = (event: MediaQueryListEvent) => setReduceMotion(event.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, {
      renderer: "canvas",
    });
    chartRef.current = chart;
    onReadyRef.current?.(chart);

    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(renderedOption, { notMerge: true });
  }, [renderedOption]);

  return <div ref={containerRef} style={{ height }} className={className} />;
}
