"use client";

import React, { useState } from "react";
import Link from "next/link";
import {
  AlertCircle,
  ArrowDownRight,
  ArrowUpRight,
  ChevronDown,
  Inbox,
  Layers,
  Loader2,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";

/* ---------------- 基础原语（shadcn/ui 风格，精简实现） ---------------- */

export function Button({
  className,
  variant = "primary",
  size = "md",
  loading,
  disabled,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  /* accent = 青绿实心，用于「核心数据正向动作」；一张卡里最多出现一个 */
  variant?: "primary" | "accent" | "outline" | "ghost" | "onHero";
  size?: "sm" | "md";
  loading?: boolean;
}) {
  return (
    <button
      disabled={disabled || loading}
      className={cn(
        "inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-pill font-medium transition-[background-color,border-color,color,box-shadow,transform] duration-150 active:scale-[.985] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50",
        size === "sm" ? "h-8 px-3.5 text-xs" : "h-9 px-4 text-sm",
        variant === "primary" && "bg-primary text-primary-foreground hover:bg-primary/90 active:bg-primary/85",
        variant === "accent" && "bg-accent text-accent-foreground hover:bg-accent/90 active:bg-accent/85",
        variant === "outline" && "border border-input bg-surface text-foreground hover:border-primary/35 hover:bg-muted active:bg-muted",
        variant === "ghost" && "text-muted-foreground hover:bg-muted hover:text-foreground active:bg-muted",
        /* 青绿焦点卡内部：白底胶囊 + 近黑字，或半透明白胶囊 */
        variant === "onHero" && "bg-white text-accent-deep hover:bg-white/90 active:bg-white/85",
        className
      )}
      {...props}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
      {children}
    </button>
  );
}

/** Keep overlays mounted just long enough to finish a short exit animation. */
export function useMotionPresence(open: boolean, exitDurationMs = 180) {
  const [present, setPresent] = React.useState(open);

  React.useEffect(() => {
    if (open) {
      setPresent(true);
      return;
    }
    if (!present) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setPresent(false);
      return;
    }

    const timeoutId = window.setTimeout(() => setPresent(false), exitDurationMs);
    return () => window.clearTimeout(timeoutId);
  }, [open, present, exitDurationMs]);

  return open || present;
}

export function Card({
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("card", className)} {...props}>
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  desc,
  action,
  divider = false,
}: {
  title: React.ReactNode;
  desc?: React.ReactNode;
  action?: React.ReactNode;
  /** 默认靠留白分栏；只有内容本身需要「表头/表体」语义时才画线 */
  divider?: boolean;
}) {
  return (
    <div className={cn("card-head", divider && "card-head-line")}>
      <div className="min-w-0">
        <h3 className="card-title">{title}</h3>
        {desc && <p className="card-desc">{desc}</p>}
      </div>
      {action}
    </div>
  );
}

/** 卡头左侧的「图标 chip + 标题 + 说明」组合，避免每页重写一遍同样的三行结构。 */
export function CardHeading({
  icon,
  title,
  desc,
  tone = "primary",
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title: React.ReactNode;
  desc?: React.ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      {icon && <IconChip icon={icon} tone={tone} />}
      <div className="min-w-0">
        <h3 className="text-sm font-semibold">{title}</h3>
        {desc && <p className="mt-0.5 truncate text-xs text-muted-foreground">{desc}</p>}
      </div>
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "green" | "amber" | "red" | "blue" | "accent";
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "bg-muted text-muted-foreground ring-border",
    green: "bg-success-soft text-success ring-success/20",
    amber: "bg-warning-soft text-warning ring-warning/20",
    red: "bg-destructive-soft text-destructive ring-destructive/20",
    blue: "bg-primary-soft text-primary ring-primary/20",
    accent: "bg-accent-soft text-accent ring-accent/20",
  };
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-pill px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset",
        tones[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export function Skeleton({
  className,
  animationDelayMs = 0,
}: {
  className?: string;
  animationDelayMs?: number;
}) {
  return (
    <div
      aria-hidden="true"
      className={cn("skeleton-shimmer rounded-control bg-muted", className)}
      style={{ "--skeleton-delay": `${animationDelayMs}ms` } as React.CSSProperties}
    />
  );
}

/**
 * 状态圆点：表格和列表里表达状态用它，比再来一个彩色胶囊安静得多。
 * 一张表里最多两三种状态时，圆点的可扫读性明显好于同等数量的 Badge。
 */
export function StatusDot({
  tone = "neutral",
  children,
  className,
}: {
  tone?: "green" | "amber" | "red" | "blue" | "neutral";
  children?: React.ReactNode;
  className?: string;
}) {
  const dots: Record<string, string> = {
    green: "bg-success",
    amber: "bg-warning",
    red: "bg-destructive",
    blue: "bg-primary",
    neutral: "bg-muted-foreground/50",
  };
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-xs text-muted-foreground",
        className
      )}
    >
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", dots[tone])} />
      {children}
    </span>
  );
}

export function Input({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(
        "h-9 w-full rounded-control border border-input bg-surface px-3 text-sm transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground",
        className
      )}
      {...props}
    />
  );
}

export function Select({
  className,
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={cn(
        "h-9 rounded-control border border-input bg-surface px-2.5 text-sm transition-colors focus-visible:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground",
        className
      )}
      {...props}
    >
      {children}
    </select>
  );
}

/* ---------------- 页面级复合组件 ---------------- */

export function PageHeader({
  title,
  desc,
  action,
}: {
  title: React.ReactNode;
  desc?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {desc && <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{desc}</p>}
      </div>
      {action}
    </div>
  );
}

export function StatCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
  accent?: "red" | "green" | "amber" | "blue" | "neutral";
}) {
  const accents: Record<string, string> = {
    red: "text-destructive",
    green: "text-success",
    amber: "text-warning",
    blue: "text-primary",
    neutral: "text-foreground",
  };
  return (
    <Card className="px-5 py-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p
        className={cn(
          "mt-2 text-3xl font-semibold leading-none tracking-tight num",
          accents[accent ?? "neutral"]
        )}
      >
        {value}
      </p>
      {hint && <p className="mt-2 truncate text-xs text-muted-foreground">{hint}</p>}
    </Card>
  );
}

/* ---------------- 指标卡三件套：图标 chip + 数字 + 变化药丸 ---------------- */

type Tone = "primary" | "accent" | "green" | "amber" | "red" | "neutral";

const CHIP_TONES: Record<Tone, string> = {
  primary: "bg-primary-soft text-primary",
  /* 青绿 chip 表达「数据/成果」，与近黑的「导航/动作」区分开 */
  accent: "bg-accent-soft text-accent",
  green: "bg-success-soft text-success",
  amber: "bg-warning-soft text-warning",
  red: "bg-destructive-soft text-destructive",
  neutral: "bg-muted text-muted-foreground",
};

/** 指标卡左侧的圆角方形图标底：让每张卡有「身份」，也方便扫读时快速定位。 */
export function IconChip({
  icon: Icon = Layers,
  tone = "primary",
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-control",
        CHIP_TONES[tone],
        className
      )}
    >
      <Icon className="h-4 w-4" />
    </span>
  );
}

/**
 * 环比变化药丸。箭头负责表达方向、底色负责表达好坏，
 * 不写成 "+3/-3" 是因为数字本身已经够多了，方向用图形更快。
 * filled 用于指标卡（需要一点视觉重量），默认文字式用于表格等密集场景。
 */
export function DeltaPill({
  value,
  positiveIsGood = true,
  unit = "",
  filled = false,
  className,
}: {
  value: number;
  positiveIsGood?: boolean;
  unit?: string;
  filled?: boolean;
  className?: string;
}) {
  const flat = value === 0;
  const good = positiveIsGood ? value > 0 : value < 0;
  const Icon = value < 0 ? ArrowDownRight : ArrowUpRight;
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-0.5 text-xs font-medium num",
        filled && "rounded-pill px-1.5 py-0.5",
        filled && (flat ? "bg-muted text-muted-foreground" : good ? "bg-success-soft text-success" : "bg-destructive-soft text-destructive"),
        !filled && (flat ? "text-muted-foreground" : good ? "text-success" : "text-destructive"),
        className
      )}
      title={flat ? "与上一周期持平" : good ? "较上一周期改善" : "较上一周期变差"}
    >
      {!flat && <Icon className="h-3 w-3" />}
      {`${Math.abs(value)}${unit}`}
    </span>
  );
}

/**
 * 姓名头像。没有真实头像的数据源，就用姓名首字 + 由姓名决定的稳定底色，
 * 比纯文字胶囊更容易在名单里定位到同一个人。
 */
const AVATAR_HUES = [222, 196, 168, 262, 32, 344];

function avatarTone(name: string) {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) % 9973;
  const hue = AVATAR_HUES[hash % AVATAR_HUES.length];
  return {
    backgroundColor: `hsl(${hue} 52% 92%)`,
    color: `hsl(${hue} 46% 32%)`,
  };
}

function avatarInitial(name: string) {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  // 只取一个字符：两个字符在 28px 的圆里会被相邻头像压掉。
  // 中英混排（如「Ruzaliya 李想」）优先取中文姓氏，纯英文名取首字母。
  const cjk = trimmed.match(/[\u3400-\u9fff]/);
  if (cjk) return cjk[0];
  return trimmed[0].toUpperCase();
}

export function Avatar({
  name,
  size = 28,
  className,
}: {
  name: string;
  size?: number;
  className?: string;
}) {
  return (
    <span
      title={name}
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center rounded-full font-medium leading-none",
        className
      )}
      style={{
        ...avatarTone(name),
        width: size,
        height: size,
        fontSize: Math.round(size * 0.4),
      }}
    >
      {avatarInitial(name)}
    </span>
  );
}

/** 头像堆叠：一屏里表达「这些人」比罗列姓名省一半宽度。 */
export function AvatarStack({
  names,
  max = 5,
  size = 28,
  className,
}: {
  names: string[];
  max?: number;
  size?: number;
  className?: string;
}) {
  const shown = names.slice(0, max);
  const rest = names.length - shown.length;
  return (
    <div className={cn("flex items-center", className)}>
      {shown.map((name, index) => (
        <span
          key={`${name}-${index}`}
          className="rounded-full ring-2 ring-card"
          style={{ marginLeft: index === 0 ? 0 : -8 }}
        >
          <Avatar name={name} size={size} />
        </span>
      ))}
      {rest > 0 && (
        <span className="ml-1.5 text-xs text-muted-foreground num">+{rest}</span>
      )}
    </div>
  );
}

function useAnimatedMetric(target: number | null) {
  const [displayValue, setDisplayValue] = React.useState(0);
  const displayRef = React.useRef(0);
  const [reduceMotion, setReduceMotion] = React.useState(() =>
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  React.useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = (event: MediaQueryListEvent) => setReduceMotion(event.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  React.useEffect(() => {
    if (target === null || !Number.isFinite(target)) return;
    if (reduceMotion) {
      displayRef.current = target;
      setDisplayValue(target);
      return;
    }

    const startValue = displayRef.current;
    const startedAt = window.performance.now();
    let frame = 0;
    const animate = (now: number) => {
      const progress = Math.min((now - startedAt) / 650, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const nextValue = startValue + (target - startValue) * eased;
      displayRef.current = nextValue;
      setDisplayValue(nextValue);
      if (progress < 1) frame = window.requestAnimationFrame(animate);
    };

    frame = window.requestAnimationFrame(animate);
    return () => window.cancelAnimationFrame(frame);
  }, [target, reduceMotion]);

  return target === null || !Number.isFinite(target) ? null : displayValue;
}

function formatAnimatedMetric(value: number, target: number) {
  const fractionDigits = Math.min(3, target.toString().split(".")[1]?.length ?? 0);
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

/**
 * 指标卡：图标 chip + 主数字 + 环比药丸 + 一行说明。
 * 传 href 时整卡可点，用于「这个数字背后还有一层明细」的场景。
 */
export function KpiCard({
  label,
  value,
  unit,
  hint,
  icon,
  tone = "primary",
  valueTone = "default",
  valueClassName,
  delta,
  deltaPositiveIsGood = true,
  deltaUnit,
  href,
  onClick,
  className,
  loading = false,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  unit?: string;
  hint?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  tone?: Tone;
  valueTone?: "default" | "primary" | "accent" | "green" | "amber" | "red" | "muted";
  valueClassName?: string;
  delta?: number;
  deltaPositiveIsGood?: boolean;
  deltaUnit?: string;
  href?: string;
  onClick?: () => void;
  className?: string;
  loading?: boolean;
}) {
  const numericTarget = !loading && typeof value === "number" ? value : null;
  const animatedValue = useAnimatedMetric(numericTarget);
  const valueTones: Record<string, string> = {
    default: "text-foreground",
    primary: "text-primary",
    accent: "text-accent",
    green: "text-success",
    amber: "text-warning",
    red: "text-destructive",
    muted: "text-muted-foreground",
  };
  const inner = (
    <>
      <div className="flex items-start justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2.5">
          <IconChip icon={icon} tone={tone} />
          <span className="truncate text-sm text-muted-foreground">{label}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {typeof delta === "number" && (
            <DeltaPill value={delta} positiveIsGood={deltaPositiveIsGood} unit={deltaUnit} filled />
          )}
          {(href || onClick) && (
            <ArrowUpRight
              aria-hidden
              className="h-3.5 w-3.5 text-muted-foreground/70 transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-primary"
            />
          )}
        </span>
      </div>
      <p
        className={cn(
          "mt-4 text-2xl font-semibold leading-none tracking-tight num",
          valueTones[valueTone],
          valueClassName
        )}
        aria-busy={loading || undefined}
      >
        {loading ? (
          <span aria-label="正在加载" className="skeleton-shimmer inline-block h-8 w-12 align-middle rounded-control bg-muted" />
        ) : animatedValue !== null && numericTarget !== null ? (
          formatAnimatedMetric(animatedValue, numericTarget)
        ) : value}
        {!loading && unit && (
          <span className="ml-1 text-sm font-normal text-muted-foreground">{unit}</span>
        )}
      </p>
      {hint && <p className="mt-2.5 truncate text-xs text-muted-foreground">{hint}</p>}
    </>
  );

  if (!href && !onClick) return <Card className={cn("p-5", className)}>{inner}</Card>;
  if (onClick) {
    return (
      <Card
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onClick();
          }
        }}
        className={cn(
          "group cursor-pointer p-5 transition-[border-color,box-shadow,background-color] hover:border-primary/25 hover:shadow-lift focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
          className
        )}
      >
        {inner}
      </Card>
    );
  }
  return (
    <Link
      href={href!}
      className="group block rounded-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card
        className={cn(
          "p-5 transition-[border-color,box-shadow,background-color] hover:border-primary/25 hover:shadow-lift",
          className
        )}
      >
        {inner}
      </Card>
    </Link>
  );
}

/**
 * 首页唯一视觉焦点：青绿渐变卡。
 * 一屏只允许出现一张；数字用白字压住渐变，动作按钮给近黑实心或白底胶囊。
 */
export function HeroMetricCard({
  eyebrow,
  value,
  unit,
  delta,
  deltaPositiveIsGood = true,
  caption,
  stats,
  icon,
  ring,
  actions,
  children,
  footer,
  className,
}: {
  eyebrow: React.ReactNode;
  value: React.ReactNode;
  unit?: string;
  delta?: number;
  deltaPositiveIsGood?: boolean;
  caption?: React.ReactNode;
  /** 焦点下方的两列小指标，用来把卡片高度的余量填成有信息量的内容 */
  stats?: { label: React.ReactNode; value: React.ReactNode }[];
  icon?: React.ComponentType<{ className?: string }>;
  /** 传 0-100 会在右侧渲染粗环；不传则只用数字与说明 */
  ring?: number;
  actions?: React.ReactNode;
  /** 任意补充内容（如未提交名单预览），渲染在动作按钮与 footer 之间 */
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}) {
  const Icon = icon;
  const numericTarget = typeof value === "number" ? value : null;
  const animatedValue = useAnimatedMetric(numericTarget);
  return (
    <div className={cn("card-hero flex flex-col p-5", className)}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-medium text-accent-foreground/75">{eyebrow}</p>
          <div className="mt-3 flex items-end gap-2">
            <p className="text-5xl font-semibold leading-none tracking-tight num">
              {animatedValue !== null && numericTarget !== null
                ? formatAnimatedMetric(animatedValue, numericTarget)
                : value}
              {unit && <span className="ml-1 text-xl font-medium text-accent-foreground/70">{unit}</span>}
            </p>
            {typeof delta === "number" && (
              <span
                className={cn(
                  "mb-1 inline-flex shrink-0 items-center gap-0.5 rounded-pill px-1.5 py-0.5 text-xs font-medium num",
                  "bg-white/20 text-white"
                )}
                title={delta === 0 ? "与上一周期持平" : delta > 0 === deltaPositiveIsGood ? "较上一周期改善" : "较上一周期变差"}
              >
                {delta !== 0 && (delta < 0 ? <ArrowDownRight className="h-3 w-3" /> : <ArrowUpRight className="h-3 w-3" />)}
                {`${Math.abs(delta)}${"%"}`}
              </span>
            )}
          </div>
        </div>
        {typeof ring === "number" ? (
          <HeroRing value={ring} />
        ) : (
          Icon && (
            <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-control bg-white/20 text-white">
              <Icon className="h-4 w-4" />
            </span>
          )
        )}
      </div>

      {caption && (
        <p className="mt-3 text-sm leading-relaxed text-accent-foreground/85">{caption}</p>
      )}

      {actions && <div className="mt-4 flex flex-wrap items-center gap-2">{actions}</div>}

      {children && <div className="mt-4">{children}</div>}

      {stats && stats.length > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-4 border-t border-white/20 pt-3.5">
          {stats.map((stat, index) => (
            <div key={index} className="min-w-0">
              <p className="flex items-center gap-1 truncate text-xs text-accent-foreground/75">{stat.label}</p>
              <p className="mt-1.5 text-lg font-semibold leading-none num">{stat.value}</p>
            </div>
          ))}
        </div>
      )}

      {footer && (
        <p className="mt-auto flex items-center gap-1.5 pt-4 text-xs text-accent-foreground/70">
          {footer}
        </p>
      )}
    </div>
  );
}

/** 比例类图形首次出现时从 0 绘制到真实值，并遵循系统减少动态效果偏好。 */
function useAnimatedPercent(target: number) {
  const [percent, setPercent] = React.useState(0);

  React.useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setPercent(target);
      return;
    }
    const frame = window.requestAnimationFrame(() => setPercent(target));
    return () => window.cancelAnimationFrame(frame);
  }, [target]);

  return percent;
}

function HeroRing({ value }: { value: number }) {
  const size = 68;
  const stroke = 8;
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  const animatedPct = useAnimatedPercent(pct);
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} aria-hidden>
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="rgb(255 255 255 / 0.28)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="#fff"
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - animatedPct / 100)}
          style={{ transition: "stroke-dashoffset .8s cubic-bezier(.22,1,.36,1)" }}
        />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-sm font-semibold text-white num">
        {pct}%
      </span>
    </div>
  );
}

/** 统计小方块：设置页与详情页反复手写的「标签 + 值 + 说明」格子。 */
export function StatTile({
  label,
  value,
  hint,
  tone = "default",
  className,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: "default" | "primary" | "accent" | "green" | "amber" | "red" | "muted";
  className?: string;
}) {
  const tones: Record<string, string> = {
    default: "text-foreground",
    primary: "text-primary",
    accent: "text-accent",
    green: "text-success",
    amber: "text-warning",
    red: "text-destructive",
    muted: "text-muted-foreground",
  };
  return (
    <div className={cn("tile", className)}>
      <p className="truncate text-xs text-muted-foreground">{label}</p>
      <p className={cn("mt-1.5 text-lg font-semibold leading-tight num", tones[tone])}>{value}</p>
      {hint && <p className="mt-1 truncate text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/* ---------------- 让数据自己说话的基础件 ---------------- */

/** 横向进度条。比堆叠柱状图少一次「读图例 + 读坐标轴」的脑力开销。 */
export function ProgressBar({
  value,
  total,
  tone,
  showPct = true,
  className,
}: {
  value: number;
  total: number;
  /** 不传则按完成度自动取色；传入可强制（如青绿代表核心数据） */
  tone?: "accent" | "primary" | "success" | "warning" | "destructive" | "muted";
  showPct?: boolean;
  className?: string;
}) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0;
  const animatedPct = useAnimatedPercent(pct);
  const auto = pct >= 100 ? "bg-accent" : pct >= 60 ? "bg-primary" : "bg-warning";
  const tones: Record<string, string> = {
    accent: "bg-accent",
    primary: "bg-primary",
    success: "bg-success",
    warning: "bg-warning",
    destructive: "bg-destructive",
    muted: "bg-muted-foreground/40",
  };
  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <div
        className="h-2 min-w-0 flex-1 overflow-hidden rounded-pill bg-muted"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={cn("h-full rounded-pill transition-[width] duration-[750ms] ease-out", tone ? tones[tone] : auto)}
          style={{ width: `${animatedPct}%` }}
        />
      </div>
      {showPct && (
        <span className="w-10 shrink-0 text-right text-xs font-medium num text-muted-foreground">
          {pct}%
        </span>
      )}
    </div>
  );
}

/** 环形提交率。整个页面的视觉焦点，只表达一个数字。 */
export function ProgressRing({
  value,
  size = 128,
  stroke = 10,
  caption,
  tone,
}: {
  value: number;
  size?: number;
  stroke?: number;
  caption?: React.ReactNode;
  tone?: "accent" | "primary" | "success" | "warning";
}) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  const animatedPct = useAnimatedPercent(pct);
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - animatedPct / 100);
  const tones: Record<string, string> = {
    accent: "text-accent",
    primary: "text-primary",
    success: "text-success",
    warning: "text-warning",
  };
  const auto = pct >= 90 ? "text-accent" : pct >= 60 ? "text-primary" : "text-warning";

  return (
    <div className="flex items-center gap-5">
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg width={size} height={size} className="-rotate-90">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            className="text-border"
            stroke="currentColor"
            strokeWidth={stroke}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            className={tone ? tones[tone] : auto}
            stroke="currentColor"
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            style={{ transition: "stroke-dashoffset .8s cubic-bezier(.22,1,.36,1)" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-3xl font-semibold leading-none tracking-tight num">
            {pct}
            <span className="ml-0.5 text-base font-medium text-muted-foreground">
              %
            </span>
          </span>
          <span className="mt-1 text-xs text-muted-foreground">提交率</span>
        </div>
      </div>
      {caption && <div className="text-sm text-muted-foreground">{caption}</div>}
    </div>
  );
}

/** 可折叠区块：次要信息默认收起，避免一屏塞满让用户无从下手。

 * 支持受控模式（传 open）：数据异步到达后才决定展开的场景必须用它，
 * 因为 useState 初值只在挂载时取一次，那时数据往往还没回来。
 */
export function Collapsible({
  title,
  badge,
  defaultOpen = false,
  open: openProp,
  onOpenChange,
  action,
  children,
}: {
  title: React.ReactNode;
  badge?: React.ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [uncontrolled, setUncontrolled] = useState(defaultOpen);
  const open = openProp ?? uncontrolled;
  const setOpen = (next: boolean) => {
    if (openProp === undefined) setUncontrolled(next);
    onOpenChange?.(next);
  };

  return (
    <Card>
      <div className="card-head">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="group flex min-w-0 items-center gap-2 rounded-control text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          <ChevronDown
            className={cn(
              "h-4 w-4 text-muted-foreground transition-transform",
              open ? "rotate-0" : "-rotate-90"
            )}
          />
          {title}
          {badge}
        </button>
        {action && (
          <div className="shrink-0" onClick={(e) => e.stopPropagation()}>
            {action}
          </div>
        )}
      </div>
      {open && <div className="px-5 pb-5 pt-3">{children}</div>}
    </Card>
  );
}

/** 区块标题：与 CardHeader 的区别是不带下边框，用于分区。 */
export function SectionHeader({
  title,
  desc,
  action,
}: {
  title: React.ReactNode;
  desc?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-x-3 gap-y-2">
      <div className="min-w-0">
        <h2 className="text-base font-semibold tracking-tight">{title}</h2>
        {desc && <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{desc}</p>}
      </div>
      {action}
    </div>
  );
}

/** 模态框。用于不可逆操作的二次确认（比如真的把消息发出去）。 */
export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  title: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: "md" | "lg" | "xl";
}) {
  const present = useMotionPresence(open);

  React.useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  if (!present) return null;
  return (
    <div
      role="dialog"
      aria-modal={open}
      aria-hidden={!open}
      inert={!open}
      className={cn(
        "fixed inset-0 z-50 flex items-center justify-center p-4",
        !open && "pointer-events-none"
      )}
    >
      <div
        className={cn(
          "absolute inset-0 bg-ink/35 backdrop-blur-sm",
          open ? "ui-motion-fade-in" : "ui-motion-fade-out"
        )}
        onClick={onClose}
        aria-hidden
      />
      <div
        className={cn(
          "relative flex max-h-[85vh] w-full flex-col rounded-card border border-glass-line glass-strong",
          open ? "ui-motion-dialog-in" : "ui-motion-dialog-out",
          size === "xl" ? "max-w-4xl" : size === "lg" ? "max-w-2xl" : "max-w-lg"
        )}
      >
        <div className="flex items-center justify-between gap-3 px-5 pb-3 pt-4">
          <h3 className="card-title">{title}</h3>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="-mr-1 inline-flex h-7 w-7 items-center justify-center rounded-control text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-border/70 px-5 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

/** 页面顶部的一句话说明：告诉用户「这页能干嘛」，降低上手成本。 */
export function PageIntro({ children }: { children: React.ReactNode }) {
  return (
    <p className="mb-6 rounded-card bg-primary-soft/70 px-4 py-3 text-sm leading-relaxed text-foreground/80">
      {children}
    </p>
  );
}

export function EmptyState({
  title = "暂无数据",
  desc,
  action,
}: {
  title?: string;
  desc?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <div className="mb-1 flex h-10 w-10 items-center justify-center rounded-full bg-muted">
        <Inbox className="h-5 w-5 text-muted-foreground" />
      </div>
      <p className="text-sm font-medium text-foreground">{title}</p>
      {desc && <p className="max-w-md text-xs leading-relaxed text-muted-foreground">{desc}</p>}
      {action && <div className="mt-2 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-destructive-soft">
        <AlertCircle className="h-5 w-5 text-destructive" />
      </div>
      <p className="max-w-md text-sm leading-relaxed text-muted-foreground">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          重试
        </Button>
      )}
    </div>
  );
}

export function LoadingBlock({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-5" aria-busy="true">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-9 w-full" animationDelayMs={i * 75} />
      ))}
    </div>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  onChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  if (totalPages <= 1) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 px-5 py-3 text-xs text-muted-foreground">
      <span>
        共 {total} 条 · 第 {page}/{totalPages} 页
      </span>
      <div className="flex items-center gap-1.5">
        <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => onChange(page - 1)}>
          上一页
        </Button>
        <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => onChange(page + 1)}>
          下一页
        </Button>
      </div>
    </div>
  );
}
