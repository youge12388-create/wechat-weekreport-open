import type { EChartsOption } from "echarts";
import { isDefaultTheme, mixHex, type ThemeSettings } from "@/lib/theme";

/**
 * ECharts 主题：全站唯一的图表样式来源。
 *
 * 色板与 tailwind.config.ts 的三色体系一一对应，这里的 hex 是 token 的等价写法
 * （ECharts 不读 CSS 变量，也不支持 hsl 的新语法糖，只能落成十六进制）。
 * 改配色时两处必须同步，不要在各页面里再写死颜色。
 */
export const CHART_COLORS = {
  /** primary：hsl(231 22% 13%) 的近黑，图表里抬亮到深石板色以免线条发死 —— 计划中、过程量 */
  primary: "#363c4e",
  /** accent：hsl(172 62% 38%) —— 核心数据、增长、成果 */
  accent: "#1e9c8a",
  /** accent.deep：hsl(176 58% 30%) —— 青绿渐变暗端 */
  accentDeep: "#1d7f74",
  /** 中性灰：次要系列、参照线 */
  neutral: "#9aa3b2",
  /** warning：hsl(29 90% 40%) —— 需要关注 */
  warning: "#c2710a",
  /** destructive：hsl(0 72% 48%) —— 风险 */
  destructive: "#d13a34",
  /** 环形/堆叠图里表示「未完成」的轨道灰 */
  track: "#e7e9f0",
  /** 卡片底色。环形图的扇区间隙要用它切出白缝，与页面卡片保持一致 */
  surface: "#ffffff",
  /**
   * 轴标签与图例文字。`neutral` 是给图形元素（参照线、次要系列）用的浅灰，
   * 白底对比度只有 2.5:1，当**文字**用会读不清 —— 所以文字另取一档更深的灰。
   */
  axisLabel: "#6b7385",
} as const;

export type ChartColorSet = {
  primary: string;
  accent: string;
  accentDeep: string;
  neutral: string;
  warning: string;
  destructive: string;
  track: string;
  surface: string;
  /** 轴标签与图例的**文字**色，比 neutral 深一档（浅灰当文字用读不清）。 */
  axisLabel: string;
};

/** 自定义主题只覆盖主题相关的图表颜色，其余风险/中性色保持业务语义。 */
export function chartColorsForTheme(theme: ThemeSettings): ChartColorSet {
  if (isDefaultTheme(theme)) return CHART_COLORS;
  return {
    ...CHART_COLORS,
    primary: mixHex(theme.primary, "#1d2035", 0.32),
    accent: theme.accent,
    accentDeep: mixHex(theme.accent, "#000000", 0.2),
    surface: theme.surface,
  };
}

/** 折线/柱状系列的默认色序：主色 → 青绿 → 中性 → 琥珀，保证同一页里不撞色。 */
export const CHART_SERIES_ORDER = [
  CHART_COLORS.primary,
  CHART_COLORS.accent,
  CHART_COLORS.neutral,
  CHART_COLORS.warning,
];

export function chartSeriesOrder(colors: ChartColorSet = CHART_COLORS): string[] {
  return [colors.primary, colors.accent, colors.neutral, colors.warning];
}

/** 把 hex 转成带透明度的 rgba，用于面积渐变。 */
export function withAlpha(hex: string, alpha: number) {
  const value = hex.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** 顶部渐隐的面积填充：参考图里 Money Flow 那种「有体量但不糊」的效果。 */
export function areaGradient(hex: string, topAlpha = 0.26) {
  return {
    type: "linear" as const,
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: withAlpha(hex, topAlpha) },
      { offset: 1, color: withAlpha(hex, 0) },
    ],
  };
}

/** 深色浮动提示框：与页面浅色卡片拉开对比，读数时视线不会跟背景混在一起。 */
export function tooltipStyle() {
  return {
    backgroundColor: "#171a2b",
    borderWidth: 0,
    padding: [8, 12] as [number, number],
    borderRadius: 10,
    extraCssText: "box-shadow: 0 16px 32px -16px rgb(16 24 40 / 0.5);",
    textStyle: { color: "#fff", fontSize: 12 },
  };
}

/**
 * 轴标签与图例文字：12px，与全站 text-xs 同一档，颜色取 `axisLabel`。
 *
 * 原为 11px + `neutral` 浅灰：数字勉强能读，但中文轴名与图例（如「周报篇数」）
 * 在 11px 浅灰下笔画会糊在一起。这是**图表专属**的一档 —— 不动页面字号，
 * 只让图里的字不再比正文小一号、浅一档。
 */
const AXIS_LABEL = (colors: ChartColorSet = CHART_COLORS) => ({
  color: colors.axisLabel,
  fontSize: 12,
});

/** 直角坐标系的基础骨架：网格、图例、提示框、轴样式都收在这里。 */
export function chartBase(colors: ChartColorSet = CHART_COLORS): EChartsOption {
  const axisLabel = AXIS_LABEL(colors);
  return {
    animationDuration: 820,
    animationDurationUpdate: 420,
    animationEasing: "cubicOut",
    animationEasingUpdate: "cubicOut",
    grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 16,
      icon: "circle",
      /* 与轴标签同一档：图例也是要读的文字，不是图形元素 */
      textStyle: axisLabel,
    },
    tooltip: {
      trigger: "axis",
      ...tooltipStyle(),
      axisPointer: {
        type: "line",
        lineStyle: { color: colors.neutral, type: "dashed" },
      },
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      axisLine: { lineStyle: { color: "#e6e8ee" } },
      axisTick: { show: false },
      axisLabel,
    },
    yAxis: {
      type: "value",
      splitNumber: 3,
      splitLine: { lineStyle: { color: "#f1f2f6", type: "dashed" } },
      axisLabel,
    },
  };
}

/**
 * 数值轴配置。minInterval=1 用于人数这类整数指标，避免出现 0.5 人。
 */
export function valueAxis(
  options?: { minInterval?: number; max?: number; show?: boolean },
  colors: ChartColorSet = CHART_COLORS
) {
  return {
    type: "value" as const,
    minInterval: options?.minInterval,
    max: options?.max,
    splitNumber: 3,
    splitLine: options?.show === false ? { show: false } : { lineStyle: { color: "#f1f2f6", type: "dashed" as const } },
    axisLabel: AXIS_LABEL(colors),
  };
}

/** 平滑折线系列：统一线宽、圆点与描边，页面只传数据和颜色。 */
export function lineSeries(options: {
  name: string;
  data: (number | null)[];
  color: string;
  /** 面积渐变填充（表达体量的核心指标用 true，次要参照线用 false） */
  area?: boolean;
  dashed?: boolean;
  yAxisIndex?: number;
  width?: number;
  connectNulls?: boolean;
}) {
  return {
    name: options.name,
    type: "line" as const,
    smooth: true,
    showSymbol: false,
    symbol: "circle",
    symbolSize: 7,
    yAxisIndex: options.yAxisIndex,
    connectNulls: options.connectNulls ?? false,
    data: options.data,
    itemStyle: { color: options.color, borderColor: "#fff", borderWidth: 2 },
    lineStyle: {
      color: options.color,
      width: options.width ?? 2.4,
      type: options.dashed ? ("dashed" as const) : ("solid" as const),
    },
    ...(options.area ? { areaStyle: { color: areaGradient(options.color) } } : {}),
  };
}

/** 柱状系列：顶部小圆角，与卡片圆角语言呼应。 */
export function barSeries(options: {
  name: string;
  data: number[];
  color: string;
  yAxisIndex?: number;
}) {
  return {
    name: options.name,
    type: "bar" as const,
    barMaxWidth: 18,
    yAxisIndex: options.yAxisIndex,
    itemStyle: { color: options.color, borderRadius: [4, 4, 0, 0] },
    data: options.data,
  };
}

/**
 * 分类色板：环形图/堆叠图用「一眼能对上号」的颜色。
 *
 * 两组约束同时成立：
 * - **必须各有其色**。这些类别是并列的几摊工作，同色深浅序列（试过一版）会把
 *   「哪一块是招生」变成必须先读图例才知道的事 —— 而图例的作用正是省掉这一步。
 * - **不能花**。八个高饱和色相就是「廉价饼图」的来源。这里统一压到中低饱和、
 *   中明度（S34~46 / L33~53），相邻色相间隔 ≥ 30°，在白卡片上密度一致，
 *   谁也不比谁更抢眼。琥珀刻意避开 `warning` 的橙（本站语义是「需关注」）
 *   与 `destructive` 的红。
 *
 * 第一块（通常是最大的一摊）取主题 accent，部署换了强调色时主块跟着换；
 * 其余色相固定，保证八类在任意主题下都能互相区分。类别多于八时循环取用。
 */
export function chartCategorical(colors: ChartColorSet = CHART_COLORS, count?: number): string[] {
  const palette = [
    colors.accent,
    "#3a4f74",
    "#4b83ab",
    "#7069a6",
    "#96628c",
    "#b06a5e",
    "#a9853f",
    "#7f8b9c",
  ];
  if (!count || count <= 0) return palette;
  return Array.from({ length: count }, (_, index) => palette[index % palette.length]);
}
