/**
 * 经营看板两个视图（总览 / 每月）共用的版式零件。
 *
 * 此前 `Head` 与 `withNumbers` 在 page.tsx 与 overview.tsx 各写了一份，
 * 字号、图标底、留白各自漂移 —— 卡头是全页扫描面的锚点，必须只有一种语言。
 * 这里取总览的紧凑版（14px 标题 + 20px 图标）：看板是高频扫描面，
 * 紧凑卡头给数据留首屏高度；每月视图的卡头随之收窄一档，属预期内变化。
 */

import React from "react";

import { cn } from "@/lib/cn";

/** Head 的图标底色。与 Badge 的 tone 分开定义；"blue" 是每月视图旧叫法，并到 primary。 */
export type HeadTone = "neutral" | "green" | "amber" | "red" | "accent" | "primary";

/** 把文案里的阿拉伯数字抬成前景色 + 等宽，其余留中灰。 */
export function withNumbers(text: string, className?: string): React.ReactNode {
  const parts = text.split(/(\d+(?:\.\d+)?%?)/g);
  return (
    <>
      {parts.map((part, index) =>
        /^\d/.test(part) ? (
          <span key={index} className={cn("num font-semibold text-foreground", className)}>
            {part}
          </span>
        ) : (
          <React.Fragment key={index}>{part}</React.Fragment>
        )
      )}
    </>
  );
}

/**
 * 卡片标题行。不把标题、图标和说明各自做成一层：图标只承担定位作用，
 * 说明压成一行，给真正需要比较的数据留出首屏高度。
 */
export function Head({
  icon: Icon,
  tone = "primary",
  title,
  desc,
  action,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  tone?: HeadTone;
  title: React.ReactNode;
  desc?: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-2.5 px-4 pb-2 pt-3">
      <div className="flex min-w-0 items-start gap-2">
        {Icon ? (
          <span
            className={cn(
              "mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-md",
              tone === "red" && "bg-destructive-soft text-destructive",
              tone === "amber" && "bg-warning-soft text-warning",
              tone === "green" && "bg-success-soft text-success",
              tone === "accent" && "bg-accent-soft text-accent",
              tone === "primary" && "bg-primary-soft text-primary",
              tone === "neutral" && "bg-muted text-muted-foreground"
            )}
          >
            <Icon className="h-3 w-3" />
          </span>
        ) : null}
        <div className="min-w-0">
          <h3 className="text-[14px] font-semibold leading-5">{title}</h3>
          {desc ? (
            <p className="text-[12px] leading-5 text-muted-foreground">{desc}</p>
          ) : null}
        </div>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}
