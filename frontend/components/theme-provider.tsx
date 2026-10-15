"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { apiGet } from "@/lib/api";
import {
  applyThemeToDocument,
  DEFAULT_THEME,
  normalizeTheme,
  type ThemeSettings,
} from "@/lib/theme";

interface ThemeContextValue {
  /** 当前生效的主题，包含设置页的未保存实时预览。 */
  theme: ThemeSettings;
  /** 后台已经保存的主题，用于判断设置页是否有未保存修改。 */
  savedTheme: ThemeSettings;
  loading: boolean;
  previewTheme: (next: ThemeSettings) => void;
  commitTheme: (next: ThemeSettings) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<ThemeSettings>(DEFAULT_THEME);
  const [savedTheme, setSavedTheme] = useState<ThemeSettings>(DEFAULT_THEME);
  const [loading, setLoading] = useState(true);

  const previewTheme = useCallback((next: ThemeSettings) => {
    setTheme(normalizeTheme(next));
  }, []);

  const commitTheme = useCallback((next: ThemeSettings) => {
    const normalized = normalizeTheme(next);
    setTheme(normalized);
    setSavedTheme(normalized);
  }, []);

  useEffect(() => {
    applyThemeToDocument(theme);
  }, [theme]);

  useEffect(() => {
    let cancelled = false;
    apiGet<ThemeSettings>("/api/settings/theme")
      .then((saved) => {
        if (!cancelled) {
          const normalized = normalizeTheme(saved);
          setTheme(normalized);
          setSavedTheme(normalized);
        }
      })
      .catch(() => {
        // 主题读取失败不应阻塞业务页面，继续使用原始默认配色。
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const value = useMemo(
    () => ({ theme, savedTheme, loading, previewTheme, commitTheme }),
    [commitTheme, loading, previewTheme, savedTheme, theme]
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
