export interface ThemeSettings {
  primary: string;
  nav: string;
  accent: string;
  background: string;
  surface: string;
}

export type ThemeColorKey = keyof ThemeSettings;

/** 与 tailwind.config.ts 的原始颜色等价，未保存主题时保持旧界面。 */
export const DEFAULT_THEME: ThemeSettings = {
  primary: "#1a1c28",
  nav: "#181b27",
  accent: "#259d8d",
  background: "#f4f6f8",
  surface: "#ffffff",
};

export const THEME_COLOR_FIELDS: {
  key: ThemeColorKey;
  label: string;
  description: string;
}[] = [
  { key: "primary", label: "主色", description: "按钮、选中状态与链接" },
  { key: "nav", label: "侧栏激活色", description: "左侧导航当前项" },
  { key: "accent", label: "强调色", description: "核心数据、图表与重点卡片" },
  { key: "background", label: "页面背景", description: "主内容区域的底色" },
  { key: "surface", label: "面板底色", description: "侧栏、卡片与输入框" },
];

export const THEME_PRESETS: {
  key: string;
  label: string;
  description: string;
  theme: ThemeSettings;
}[] = [
  {
    key: "original",
    label: "原始深色",
    description: "当前默认配色",
    theme: DEFAULT_THEME,
  },
  {
    key: "rose",
    label: "雾粉雅致",
    description: "低饱和粉玫瑰",
    theme: {
      primary: "#9b5f76",
      nav: "#754459",
      accent: "#b66d83",
      background: "#faf5f7",
      surface: "#fffdfd",
    },
  },
  {
    key: "ocean",
    label: "静谧海盐",
    description: "冷静蓝青色调",
    theme: {
      primary: "#355b76",
      nav: "#29465d",
      accent: "#328b91",
      background: "#f3f7f9",
      surface: "#fcfefe",
    },
  },
];

const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;

export function isValidHexColor(value: string): boolean {
  return HEX_COLOR.test(value);
}

function parseHex(value: string): [number, number, number] | null {
  if (!isValidHexColor(value)) return null;
  return [
    Number.parseInt(value.slice(1, 3), 16),
    Number.parseInt(value.slice(3, 5), 16),
    Number.parseInt(value.slice(5, 7), 16),
  ];
}

function toHex(value: number): string {
  return Math.max(0, Math.min(255, Math.round(value)))
    .toString(16)
    .padStart(2, "0");
}

export function hexToRgb(value: string): string {
  return (parseHex(value) ?? parseHex(DEFAULT_THEME.primary)!).join(" ");
}

/** 根据底色自动选择对比度更高的文字色，避免用户选浅色后白字消失。 */
export function contrastColor(value: string): string {
  const [r, g, b] = parseHex(value) ?? parseHex(DEFAULT_THEME.primary)!;
  const channel = (component: number) => {
    const normalized = component / 255;
    return normalized <= 0.03928
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  return luminance > 0.42 ? "#1d2035" : "#ffffff";
}

/** 返回向目标色混合后的颜色，供动态主题生成柔和背景与深色渐变端。 */
export function mixHex(value: string, target: string, amount: number): string {
  const sourceRgb = parseHex(value) ?? parseHex(DEFAULT_THEME.primary)!;
  const targetRgb = parseHex(target) ?? [255, 255, 255];
  const ratio = Math.max(0, Math.min(1, amount));
  return `#${sourceRgb
    .map((component, index) => toHex(component + (targetRgb[index] - component) * ratio))
    .join("")}`;
}

export function normalizeTheme(value: Partial<ThemeSettings> | null | undefined): ThemeSettings {
  const next = { ...DEFAULT_THEME };
  for (const field of Object.keys(DEFAULT_THEME) as ThemeColorKey[]) {
    const candidate = value?.[field];
    if (typeof candidate === "string" && isValidHexColor(candidate)) {
      next[field] = candidate.toLowerCase();
    }
  }
  return next;
}

export function isDefaultTheme(theme: ThemeSettings): boolean {
  return (Object.keys(DEFAULT_THEME) as ThemeColorKey[]).every(
    (key) => theme[key].toLowerCase() === DEFAULT_THEME[key]
  );
}

/** 把后端主题映射成 Tailwind token 使用的 RGB CSS 变量。 */
export function applyThemeToDocument(value: Partial<ThemeSettings> | null | undefined): void {
  if (typeof document === "undefined") return;
  const theme = normalizeTheme(value);
  const root = document.documentElement;
  const setColor = (name: string, color: string) => {
    root.style.setProperty(name, hexToRgb(color));
  };

  setColor("--ui-primary", theme.primary);
  setColor("--ui-primary-foreground", contrastColor(theme.primary));
  setColor(
    "--ui-primary-soft",
    isDefaultTheme(theme) ? "#edeff3" : mixHex(theme.primary, "#ffffff", 0.9)
  );
  setColor("--ui-nav-active", theme.nav);
  setColor("--ui-nav-active-foreground", contrastColor(theme.nav));
  setColor("--ui-accent", theme.accent);
  setColor("--ui-accent-foreground", contrastColor(theme.accent));
  setColor(
    "--ui-accent-soft",
    isDefaultTheme(theme) ? "#e7f9f6" : mixHex(theme.accent, "#ffffff", 0.9)
  );
  setColor(
    "--ui-accent-deep",
    isDefaultTheme(theme) ? "#207973" : mixHex(theme.accent, "#000000", 0.2)
  );
  setColor("--ui-background", theme.background);
  setColor("--ui-surface", theme.surface);
}
