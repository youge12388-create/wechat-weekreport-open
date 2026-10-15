"use client";

import React, { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  AlertCircle,
  ArrowRight,
  Building2,
  ClipboardCheck,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  QrCode,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui";
import { apiGet, apiPost } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { PublicConfig } from "@/lib/types";

/** 后端拒绝原因 -> 登录页展示文案（与 auth.py 的 _DENY_MESSAGES 保持同一口径）。 */
const DENY_LABELS: Record<string, string> = {
  not_found: "你的企业微信账号不在本系统通讯录中。",
  inactive: "你的账号当前不是在职状态。",
  not_authorized: "你还没有被授权访问本系统。",
  denied_by_department: "你所在的部门被显式禁止访问本系统。",
  login_disabled: "管理员暂时关闭了企业微信登录。",
  not_member: "当前扫码的账号不是本企业成员。",
  invalid_state: "登录链接已失效，请重新发起。",
  failed: "企业微信登录失败，请稍后重试。",
};

/** 截止日的星期文案：后端 report_due_weekday 以周一为 0（默认 4 = 周五）。 */
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

function periodMeta(config: PublicConfig | null): string {
  if (!config) return "企业内部 AI 数据平台";
  const weekday = WEEKDAY_LABELS[((config.due_weekday % 7) + 7) % 7] ?? "周五";
  const hour = String(config.due_hour).padStart(2, "0");
  return `统计口径 ${config.period_rule_label} · 截止 ${weekday} ${hour}:00`;
}

/**
 * 登录页自带的样式。
 * 只服务本页（前缀 zl-），不写进 globals.css：登录页是独立整屏，动效密度与业务页不同，
 * 放进全局会污染其他页面的基调。prefers-reduced-motion 由 globals.css 统一兜底。
 *
 * 关于 .zl-glass：全站唯一的玻璃档 .glass-strong 是「近白面 + 28px 模糊」（为浅色画布上的
 * 模态面板设计），这里整页是一块深墨画布，白色玻璃会把画面重新切成两块，所以换成深色面。
 *
 * 它**刻意不做 backdrop-filter**，原因有两个，都是实测踩到的：
 * 1. 视觉上没必要 —— 卡片背后是 blur 110~140px 的巨型光晕，对一片已经极度平滑的底色
 *    再模糊 28px，肉眼分辨不出差别；
 * 2. 代价很大 —— 有 backdrop-filter 时，背后任何动画都会让这一层每帧重新栅格化。
 *    实测三个常驻动画的模糊层在上面时，合成器会丢帧到卡片内容整块不渲染（截图里按钮全空）。
 * 因此这里只用「半透明深色渐变 + 描边」表达「同一场景里更亮的一块」，
 * 配合卡片背后的柔光晕，融合感来自布光本身而不是模糊。
 */
const LOGIN_CSS = `
@keyframes zl-rise {
  /* 入场只做位移，**不做透明度**：起始 opacity:0 + fill-mode both 的写法，
     在动画被暂停或节流时会（实测也会）让元素停在不可见状态 —— 卡片整块空白，
     而卡片里就是登录表单。位移在动画不跑时只是"少动一下"，内容始终可读。 */
  from { transform: translate3d(0, 16px, 0); }
  to { transform: none; }
}
/* 页脚与网格的淡入：这两处是装饰与次要信息，
   即使动画被节流导致停在透明态，也不影响登录本身（登录卡与品牌区一律用 zl-rise 的位移入场）。 */
@keyframes zl-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes zl-drift-a {
  0%, 100% { transform: translate3d(0, 0, 0); }
  50% { transform: translate3d(7%, 6%, 0); }
}
@keyframes zl-drift-b {
  0%, 100% { transform: translate3d(0, 0, 0); }
  50% { transform: translate3d(-8%, -6%, 0); }
}
@keyframes zl-drift-c {
  0%, 100% { transform: translate3d(-4%, 3%, 0); }
  50% { transform: translate3d(5%, -5%, 0); }
}
@keyframes zl-grid-pan {
  from { background-position: 0 0, 0 0; }
  to { background-position: 56px 56px, 56px 56px; }
}
@keyframes zl-veil {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes zl-shake {
  10%, 90% { transform: translateX(-1.5px); }
  20%, 80% { transform: translateX(2px); }
  30%, 50%, 70% { transform: translateX(-4px); }
  40%, 60% { transform: translateX(4px); }
}
@keyframes zl-sheen {
  from { transform: translateX(-140%) skewX(-14deg); }
  to { transform: translateX(240%) skewX(-14deg); }
}
@keyframes zl-scan {
  from { transform: translateX(-110%); }
  to { transform: translateX(320%); }
}

.zl-rise { animation: zl-rise .78s cubic-bezier(.22, 1, .36, 1) both; }
.zl-fade { animation: zl-fade 1.2s ease both; }
.zl-veil { animation: zl-veil 1.6s ease both; }
.zl-shake { animation: zl-shake .5s cubic-bezier(.36, .07, .19, .97) both; }
.zl-drift-a { animation: zl-drift-a 26s ease-in-out infinite; }
.zl-drift-b { animation: zl-drift-b 32s ease-in-out infinite; }
.zl-drift-c { animation: zl-drift-c 38s ease-in-out infinite; }
.zl-grid-pan { animation: zl-grid-pan 22s linear infinite; }
.zl-scan { animation: zl-scan 1.5s cubic-bezier(.4, 0, .2, 1) infinite; }

/* 深色面板：半透明暗底 + 一层斜面高光，不做模糊（原因见文件头注释）。
   暗底不能省 —— 只留高光渐变的话，卡片正面的正文对比度会随背后的光晕漂移而变，
   而正文里就有输入框和按钮。 */
.zl-glass {
  background-color: rgb(14 17 26 / 0.42);
  background-image: linear-gradient(160deg, rgb(255 255 255 / 0.10), rgb(255 255 255 / 0.035) 46%, rgb(255 255 255 / 0.055));
}

/* 高光扫过：只在 hover / 键盘聚焦时跑一次，不做常驻动画 */
.zl-sheen { position: relative; overflow: hidden; }
.zl-sheen::after {
  content: "";
  position: absolute;
  inset: -20% -30%;
  pointer-events: none;
  background: linear-gradient(100deg, transparent 25%, rgb(255 255 255 / .34) 50%, transparent 75%);
  transform: translateX(-140%) skewX(-14deg);
}
.zl-sheen:hover::after,
.zl-sheen:focus-visible::after { animation: zl-sheen 1.05s cubic-bezier(.22, 1, .36, 1); }
`;

/** 卡下方的能力点：一句话说清系统真的做了什么，不写形容词。 */
const HIGHLIGHTS = [
  { icon: RefreshCw, title: "自动同步" },
  { icon: Sparkles, title: "AI 洞察" },
  { icon: ShieldCheck, title: "授权准入" },
];

function LoginForm() {
  const params = useSearchParams();
  const denied = params.get("denied");

  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [configLoaded, setConfigLoaded] = useState(false);
  const [password, setPassword] = useState("");
  const [revealPassword, setRevealPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 密码登录默认折叠：企微登录是同事的日常路径，密码只服务管理员兜底。
  const [showPassword, setShowPassword] = useState(false);
  // 点击企微入口到整页跳转之间有一段空档，用按钮内联 loading 顶住重复点击
  const [redirecting, setRedirecting] = useState<"qr" | "oauth" | null>(null);

  const stageRef = useRef<HTMLDivElement>(null);
  const passwordInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    apiGet<PublicConfig>("/api/config")
      .then(setConfig)
      .catch(() => setConfig(null))
      .finally(() => setConfigLoaded(true));
  }, []);

  useEffect(() => {
    if (denied) setError(DENY_LABELS[denied] ?? DENY_LABELS.failed);
  }, [denied]);

  const wecomEnabled = config?.wecom_login_enabled ?? false;
  // 配置读不到时也走密码路径：让唯一不依赖企微的方式先可点，而不是干等一个按钮
  const configFailed = configLoaded && config === null;
  const passwordOnly = configLoaded && !wecomEnabled;
  const passwordOpen = showPassword || passwordOnly;

  // 展开才聚焦：折叠状态下聚焦隐藏输入框会把页面上滚一格
  useEffect(() => {
    if (passwordOpen) passwordInputRef.current?.focus();
  }, [passwordOpen]);

  // 整页是一个场景，跟随光晕铺满全屏：只驱动两个 CSS 变量，不做 React 状态更新
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    if (typeof window === "undefined") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;

    let frame = 0;
    const onMove = (event: PointerEvent) => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        stage.style.setProperty("--zl-x", `${event.clientX}px`);
        stage.style.setProperty("--zl-y", `${event.clientY}px`);
      });
    };
    window.addEventListener("pointermove", onMove);
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await apiPost<{ authenticated: boolean }>("/api/auth/login", { password });
      // 登录成功：整页跳转，让后端会话 Cookie 与后续鉴权生效
      window.location.href = "/dashboard";
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  const wecomLogin = (mode: "qr" | "oauth") => {
    const next = params.get("next") || "/dashboard";
    setRedirecting(mode);
    window.location.href = `/api/auth/wecom/start?mode=${mode}&next=${encodeURIComponent(next)}`;
  };

  return (
    <div
      ref={stageRef}
      style={{ "--zl-x": "50%", "--zl-y": "38%" } as React.CSSProperties}
      className="relative flex min-h-screen flex-col overflow-x-hidden bg-ink text-white"
    >
      <style>{LOGIN_CSS}</style>

      {/* ---- 背景层：整页一个场景，不切块 ----
          全部用 fixed 锚在视口上（而不是 inset-0 的 absolute）：
          卡片展开或出现报错时页面会变高，absolute 的层会跟着被拉伸，
          网格蒙版的边界就会在屏幕中间显出一条硬边。fixed 让场景始终就是眼前这一屏。 */}
      {/* 缓慢平移的网格，向四周淡出，避免整页铺满网格显得机械 */}
      <div
        aria-hidden
        className="zl-grid-pan zl-veil pointer-events-none fixed inset-0"
        style={{
          backgroundImage:
            "linear-gradient(rgb(255 255 255 / 0.055) 1px, transparent 1px), linear-gradient(90deg, rgb(255 255 255 / 0.055) 1px, transparent 1px)",
          backgroundSize: "56px 56px, 56px 56px",
          maskImage: "radial-gradient(128% 96% at 50% 28%, #000 8%, transparent 78%)",
          WebkitMaskImage: "radial-gradient(128% 96% at 50% 28%, #000 8%, transparent 78%)",
        }}
      />
      {/* 三团极光：用 radial-gradient 画，**不用 filter: blur()**。
          实测给几千像素的层加 blur(110~140px) 再让它常驻动画，光栅化成本高到截图都会超时；
          渐变本身就是平滑过渡，视觉几乎一致而成本低几个量级。
          强度压在「隐约有颜色」这一档 —— 它们负责让深色面有空间感，不该被看见形状。 */}
      <div
        aria-hidden
        className="zl-drift-a pointer-events-none fixed -left-40 -top-48 h-[40rem] w-[40rem]"
        style={{
          background: "radial-gradient(closest-side, rgb(37 157 141 / 0.42), rgb(37 157 141 / 0.14) 55%, transparent 78%)",
        }}
      />
      <div
        aria-hidden
        className="zl-drift-b pointer-events-none fixed -bottom-64 -right-48 h-[46rem] w-[46rem]"
        style={{
          background: "radial-gradient(closest-side, rgb(32 121 115 / 0.40), rgb(32 121 115 / 0.13) 55%, transparent 78%)",
        }}
      />
      <div
        aria-hidden
        className="zl-drift-c pointer-events-none fixed left-1/2 top-[12%] h-[32rem] w-[56rem] -translate-x-1/2"
        style={{
          background: "radial-gradient(closest-side, rgb(37 157 141 / 0.22), transparent 76%)",
        }}
      />
      {/* 竖向渐变：把画布从顶部偏亮的深蓝洗到底部近黑，整页才有纵深 */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          background:
            "linear-gradient(180deg, rgb(30 42 62 / 0.55) 0%, transparent 42%, rgb(9 11 18 / 0.55) 100%)",
        }}
      />
      {/* 跟随鼠标的光晕：铺满整页，落在哪儿哪儿亮一点 */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 hidden lg:block"
        style={{
          background:
            "radial-gradient(420px circle at var(--zl-x) var(--zl-y), rgb(37 157 141 / 0.14), transparent 70%)",
        }}
      />
      {/* 上下两端压暗：把视线收在中轴，也让页脚安静下来 */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          background:
            "linear-gradient(180deg, rgb(6 8 15 / 0.55) 0%, transparent 22%, transparent 62%, rgb(6 8 15 / 0.6) 100%)",
        }}
      />

      {/* 内容用 my-auto 居中而不是 justify-center：
          justify-center 在内容高于容器时会把顶部等量推出可视区（品牌标题直接被裁掉，
          实测卡片一展开就发生）。auto 外边距在装得下时居中、装不下时归零并从头排。 */}
      <main className="relative z-10 flex flex-1 flex-col items-center px-5 py-12">
        <div className="my-auto flex w-full flex-col items-center">
        {/* 品牌：居中在中轴上，与卡片同一条竖线，不再另起一栏 */}
        <div className="zl-rise flex flex-col items-center text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-control bg-white/[0.08] text-white ring-1 ring-inset ring-white/15">
            <ClipboardCheck className="h-5 w-5" />
          </span>
          <h1 className="mt-4 text-lg font-semibold tracking-tight text-white">周报平台</h1>
          <p className="mt-2 max-w-[22rem] text-sm leading-relaxed text-white/50">
            让每一份周报，都变成可执行的管理线索。
          </p>
        </div>

        <div className="relative mt-8 w-full max-w-[27rem]">
          {/* 卡片背后的柔光：卡片于是「亮在同一片光里」，而不是贴上去的一块板。
              同样用渐变而非 blur 滤镜（见背景层注释）。 */}
          <div
            aria-hidden
            className="pointer-events-none absolute -inset-x-20 -inset-y-14"
            style={{
              background: "radial-gradient(closest-side, rgb(37 157 141 / 0.20), transparent 74%)",
            }}
          />

          <div
            className="zl-rise zl-glass relative overflow-hidden rounded-panel border border-white/12 px-7 py-8 shadow-glass sm:px-8"
            style={{ animationDelay: "70ms" }}
          >
            <span
              aria-hidden
              className="absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-accent/70 to-transparent"
            />
            {/* 提交中：顶部不定量进度条，替代把整张卡变成 loading 骨架 */}
            {loading && (
              <span aria-hidden className="absolute inset-x-0 top-0 h-0.5 overflow-hidden">
                <span className="zl-scan block h-full w-1/3 rounded-pill bg-accent" />
              </span>
            )}

            <div className="zl-rise" style={{ animationDelay: "130ms" }}>
              <p className="text-xs font-medium tracking-[0.16em] text-white/45">企业内部系统</p>
              <h2 className="mt-2.5 text-2xl font-semibold tracking-tight text-white">
                欢迎回来
              </h2>
              <p className="mt-2 text-sm leading-relaxed text-white/60">
                {wecomEnabled
                  ? "使用企业微信身份进入，或输入管理员访问密码。"
                  : "请输入访问密码进入管理后台。"}
              </p>
            </div>

            {error && (
              <div
                role="alert"
                className="zl-shake mt-5 flex items-start gap-2.5 rounded-control border border-destructive/25 bg-destructive/12 px-3.5 py-3 text-xs leading-relaxed text-white/90"
              >
                <AlertCircle className="mt-px h-4 w-4 shrink-0 text-destructive" />
                <span>{error}</span>
              </div>
            )}

            {configFailed && (
              <p className="mt-5 flex items-start gap-2 rounded-control border border-warning/25 bg-warning/15 px-3.5 py-3 text-xs leading-relaxed text-white/85">
                <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0 text-warning" />
                未读取到登录方式配置，请使用访问密码；若仍无法登录请稍后重试。
              </p>
            )}

            {!configLoaded ? (
              <div className="mt-7 space-y-3" aria-hidden>
                <div className="skeleton-shimmer skeleton-shimmer-on-dark h-11 rounded-control bg-white/[0.07]" />
                <div className="skeleton-shimmer skeleton-shimmer-on-dark h-11 rounded-control bg-white/[0.07]" style={{ "--skeleton-delay": "100ms" } as React.CSSProperties} />
              </div>
            ) : (
              <div className="zl-rise" style={{ animationDelay: "210ms" }}>
                {wecomEnabled && (
                  <div className="mt-7 space-y-3">
                    <Button
                      type="button"
                      variant="accent"
                      className="zl-sheen h-11 w-full justify-between"
                      disabled={redirecting !== null}
                      onClick={() => wecomLogin("qr")}
                    >
                      <span className="flex items-center gap-2.5">
                        {redirecting === "qr" ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <QrCode className="h-4 w-4" />
                        )}
                        企业微信扫码登录
                      </span>
                      <ArrowRight
                        aria-hidden
                        className={cn(
                          "h-4 w-4 transition-transform duration-300",
                          redirecting === "qr" ? "translate-x-0.5 opacity-90" : "opacity-70"
                        )}
                      />
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      className="h-11 w-full justify-between border-white/15 bg-white/[0.05] text-white transition-[border-color,background-color,transform] hover:-translate-y-px hover:border-white/25 hover:bg-white/[0.09]"
                      disabled={redirecting !== null}
                      onClick={() => wecomLogin("oauth")}
                    >
                      <span className="flex items-center gap-2.5">
                        {redirecting === "oauth" ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Building2 className="h-4 w-4" />
                        )}
                        企业微信客户端内登录
                      </span>
                      <ArrowRight
                        aria-hidden
                        className={cn(
                          "h-4 w-4 transition-transform duration-300",
                          redirecting === "oauth"
                            ? "translate-x-0.5 opacity-90"
                            : "text-white/40"
                        )}
                      />
                    </Button>
                    <p className="text-center text-xs leading-relaxed text-white/45">
                      扫码用手机企业微信确认；在企业微信里打开本页可免扫码。
                    </p>
                  </div>
                )}

                {/* 密码入口：企微配置坏掉或需要超管视角时的兜底，始终保留。
                    展开/收起走 grid-rows 过渡，避免卡片高度硬跳。 */}
                <div
                  className={cn(
                    "grid transition-[grid-template-rows] duration-500 ease-out",
                    !passwordOnly && wecomEnabled && "border-t border-white/10"
                  )}
                  style={{ gridTemplateRows: passwordOpen ? "0fr" : "1fr" }}
                  inert={passwordOpen}
                >
                  <div className="min-h-0 overflow-hidden">
                    <div className={cn("flex justify-center pt-5", !wecomEnabled && "pt-0")}>
                      <button
                        type="button"
                        onClick={() => setShowPassword(true)}
                        className="group inline-flex items-center gap-1.5 rounded-pill px-3 py-1.5 text-xs text-white/55 transition-colors hover:bg-white/[0.07] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        <KeyRound className="h-3.5 w-3.5" />
                        使用访问密码登录
                        <ArrowRight
                          aria-hidden
                          className="h-3.5 w-3.5 transition-transform duration-300 group-hover:translate-x-0.5"
                        />
                      </button>
                    </div>
                  </div>
                </div>

                <div
                  className="grid transition-[grid-template-rows] duration-500 ease-out"
                  style={{ gridTemplateRows: passwordOpen ? "1fr" : "0fr" }}
                  inert={!passwordOpen}
                >
                  <div className="min-h-0 overflow-hidden">
                    <form
                      onSubmit={submit}
                      className={cn(
                        "space-y-4 pt-5 transition-opacity duration-500",
                        passwordOpen ? "opacity-100" : "opacity-0",
                        wecomEnabled && "border-t border-white/10"
                      )}
                    >
                      <div>
                        <label
                          htmlFor="access-password"
                          className="mb-2 block text-xs font-medium text-white/60"
                        >
                          访问密码
                        </label>
                        <div className="relative">
                          {/* 样式与 components/ui.tsx 的 Input 同一套，只是配色改为深色面；
                              单独写原生 input 是为了挂 ref —— 展开时聚焦需要它 */}
                          <input
                            id="access-password"
                            ref={passwordInputRef}
                            type={revealPassword ? "text" : "password"}
                            value={password}
                            autoComplete="current-password"
                            onChange={(e) => setPassword(e.target.value)}
                            placeholder="请输入密码"
                            className="h-11 w-full rounded-control border border-white/15 bg-white/[0.06] px-3.5 pr-11 text-sm text-white transition-colors placeholder:text-white/35 focus-visible:border-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/25"
                          />
                          <button
                            type="button"
                            onClick={() => setRevealPassword((prev) => !prev)}
                            aria-label={revealPassword ? "隐藏密码" : "显示密码"}
                            className="absolute right-1 top-1/2 inline-flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-control text-white/45 transition-colors hover:bg-white/[0.08] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          >
                            {revealPassword ? (
                              <EyeOff className="h-4 w-4" />
                            ) : (
                              <Eye className="h-4 w-4" />
                            )}
                          </button>
                        </div>
                      </div>
                      <Button
                        type="submit"
                        loading={loading}
                        className="h-11 w-full bg-white text-ink hover:bg-white/90 active:bg-white/85"
                      >
                        {loading ? "正在验证…" : "登录"}
                      </Button>
                      <p className="text-center text-xs text-white/45">
                        需要开通访问权限，请联系系统管理员。
                      </p>
                    </form>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* 能力点：与卡片同一条中轴，横向一行，不再单独成块 */}
        <ul
          className="zl-rise mt-7 grid w-full max-w-[27rem] grid-cols-1 gap-2 sm:grid-cols-3"
          style={{ animationDelay: "330ms" }}
        >
          {HIGHLIGHTS.map((item) => {
            const Icon = item.icon;
            return (
              <li
                key={item.title}
                className="flex items-center justify-center gap-2 text-xs text-white/55"
              >
                <Icon className="h-3.5 w-3.5 shrink-0 text-accent/80" />
                <span className="whitespace-nowrap">{item.title}</span>
              </li>
            );
          })}
        </ul>
        </div>
      </main>

      <footer
        className="zl-fade relative z-10 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 px-5 pb-7 text-xs text-white/35"
        style={{ animationDelay: "520ms" }}
      >
        <span>{periodMeta(config)}</span>
        <span aria-hidden className="h-1 w-1 rounded-full bg-white/20" />
        <span>仅限授权成员访问</span>
      </footer>
    </div>
  );
}

export default function LoginPage() {
  // useSearchParams 必须包在 Suspense 里，否则 Next.js 会退化成整页客户端渲染
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen flex-col items-center justify-center gap-6 bg-ink px-5">
          <div className="skeleton-shimmer skeleton-shimmer-on-dark h-12 w-12 rounded-control bg-white/10" />
          <div className="skeleton-shimmer skeleton-shimmer-on-dark h-64 w-full max-w-[27rem] rounded-panel bg-white/[0.06]" style={{ "--skeleton-delay": "100ms" } as React.CSSProperties} />
        </div>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
