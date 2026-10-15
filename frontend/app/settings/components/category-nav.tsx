"use client";

import type React from "react";
import { BellRing, Bot, Palette, RefreshCw, ShieldCheck } from "lucide-react";

export type SettingsCategory = "sync" | "rules" | "access" | "ai" | "theme";

/** 设置分类：一页只展示一类，避免「什么都堆在一页」。 */
export const SETTINGS_CATEGORIES: {
  key: SettingsCategory;
  label: string;
  desc: string;
  icon: typeof RefreshCw;
}[] = [
  {
    key: "sync",
    label: "同步与抓取",
    desc: "企业微信周报的自动拉取、手动补抓与通讯录同步",
    icon: RefreshCw,
  },
  {
    key: "rules",
    label: "周报规则",
    desc: "催交时间、免提交名单等业务口径",
    icon: BellRing,
  },
  {
    key: "access",
    label: "登录与授权",
    desc: "企业微信登录与部门树状授权范围",
    icon: ShieldCheck,
  },
  {
    key: "ai",
    label: "AI 与翻译",
    desc: "大模型密钥、模型选择与翻译服务",
    icon: Bot,
  },
  {
    key: "theme",
    label: "界面主题",
    desc: "自定义主色、强调色与页面底色",
    icon: Palette,
  },
];

export function SettingsCategoryNav({
  value,
  onChange,
  badges,
}: {
  value: SettingsCategory;
  onChange: (key: SettingsCategory) => void;
  badges?: Partial<Record<SettingsCategory, React.ReactNode>>;
}) {
  return (
    <nav
      aria-label="设置分类"
      className="lg:sticky lg:top-6 lg:w-56 lg:shrink-0"
    >
      <ul className="scroll-x-clean flex gap-1.5 overflow-x-auto pb-1 lg:flex-col lg:overflow-visible lg:pb-0">
        {SETTINGS_CATEGORIES.map((category) => {
          const Icon = category.icon;
          const active = value === category.key;
          return (
            <li key={category.key} className="shrink-0 lg:shrink lg:grow-0">
              <button
                type="button"
                onClick={() => onChange(category.key)}
                aria-current={active ? "page" : undefined}
                className={
                  active
                    ? "flex w-full items-center gap-2.5 rounded-control bg-primary px-3.5 py-2.5 text-left text-sm font-medium text-primary-foreground transition-colors"
                    : "flex w-full items-center gap-2.5 rounded-control px-3.5 py-2.5 text-left text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
                }
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="whitespace-nowrap">{category.label}</span>
                {badges?.[category.key] && (
                  <span className="ml-auto hidden lg:inline-flex">
                    {badges[category.key]}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
      <p className="mt-3 hidden px-3.5 text-xs leading-relaxed text-muted-foreground lg:block">
        {SETTINGS_CATEGORIES.find((item) => item.key === value)?.desc}
      </p>
    </nav>
  );
}
