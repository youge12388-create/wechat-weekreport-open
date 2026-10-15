import type { Config } from "tailwindcss";

/**
 * 设计 token v3：三色体系 + 圆角三档。
 *
 * 三条硬规则：
 * 1. 一张页面只出现三个色族——近黑（导航与主按钮）、青绿（增长与核心数据）、
 *    冷灰（背景与容器），其余信息全部交给中性色。颜色越少，重点越突出。
 * 2. 层次靠「留白 + 分层底色 + 极弱投影」建立，不用分割线堆砌，也不用双层软阴影。
 * 3. 圆角收敛为三档：容器 rounded-card(16px) / 控件 rounded-control(10px) / 徽标与按钮 rounded-pill。
 *
 * primary 用带一点冷调的近黑而不是纯 #000：纯黑在 LCD 上发死，
 * 也无法靠明度差表达 hover/禁用等状态；带冷调才能和冷灰底、青绿点缀和谐共存。
 *
 * success 与 accent 分工明确：accent 表达「数据本身」（青绿主数据色），
 * success 只表达「状态为成功」，避免两套绿在一个页面里打架。
 *
 * 中性色用低彩度冷灰，保证在白底与浅灰底上正文对比度均 ≥ 4.5:1。
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        /* Inter 负责拉丁字母与数字（含等宽数字）；中文交给自托管的 Noto Sans SC（思源黑体），
           不再依赖「客户端装了什么系统字体」——同一份界面在 Windows / Mac / Linux 上字形一致。
           后面那串系统字体只在 webfont 尚未加载完成时兜底（swap 期间）。 */
        sans: [
          "var(--font-inter)",
          "var(--font-noto-sc)",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "Noto Sans SC",
          "sans-serif",
        ],
      },
      borderRadius: {
        /* 三档圆角：容器 / 控件 / 胶囊 */
        card: "16px",
        control: "10px",
        pill: "999px",
        /* 悬浮侧栏面板：比卡片更大的圆角，才有「一块独立面板浮在页面上」的量感 */
        panel: "24px",
        /* 侧栏导航项胶囊：介于控制件与整圆之间 */
        nav: "15px",
      },
      colors: {
        border: "hsl(220 18% 92%)",
        input: "hsl(220 18% 89%)",
        ring: "rgb(var(--ui-primary, 26 28 40) / <alpha-value>)",
        /* 页面画布：很浅的冷灰。白色面板浮在它上面才有层次，纯白底会让面板「融掉」 */
        background: "rgb(var(--ui-background, 244 246 248) / <alpha-value>)",
        /* 悬浮面板的半透明描边：比正文描边更轻，只负责勾出边缘 */
        "panel-line": "rgba(20, 30, 50, 0.06)",
        /* 应用外壳（侧栏、移动端顶栏）用纯白，与浅灰内容区形成一条干净的边界 */
        surface: "rgb(var(--ui-surface, 255 255 255) / <alpha-value>)",
        /* 模态面板的半透明磨砂面（见 globals.css 的 .glass-strong）；侧栏改用 .panel。
           不支持 backdrop-filter 的浏览器会回退成不透明白，保证内容始终可读。 */
        glass: {
          strong: "hsl(0 0% 100% / 0.86)",
          line: "hsl(0 0% 100% / 0.60)",
        },
        /* 近黑面：logo 底与代码块 */
        ink: {
          DEFAULT: "hsl(231 30% 16%)",
          foreground: "hsl(0 0% 100%)",
        },
        foreground: "rgb(var(--ui-foreground, 29 32 53) / <alpha-value>)",
        muted: {
          DEFAULT: "hsl(220 22% 96%)",
          foreground: "hsl(220 10% 44%)",
        },
        card: {
          DEFAULT: "rgb(var(--ui-surface, 255 255 255) / <alpha-value>)",
          foreground: "rgb(var(--ui-foreground, 29 32 53) / <alpha-value>)",
        },
        /* 近黑：导航选中态与主要动作按钮，一屏最多一处大面积使用 */
        primary: {
          DEFAULT: "rgb(var(--ui-primary, 26 28 40) / <alpha-value>)",
          foreground: "rgb(var(--ui-primary-foreground, 255 255 255) / <alpha-value>)",
          soft: "rgb(var(--ui-primary-soft, 237 239 243) / <alpha-value>)",
        },
        /* 青绿：增长、核心数据、首页视觉焦点。deep 用于焦点卡渐变的暗端 */
        accent: {
          DEFAULT: "rgb(var(--ui-accent, 37 157 141) / <alpha-value>)",
          foreground: "rgb(var(--ui-accent-foreground, 255 255 255) / <alpha-value>)",
          soft: "rgb(var(--ui-accent-soft, 231 249 246) / <alpha-value>)",
          deep: "rgb(var(--ui-accent-deep, 32 121 115) / <alpha-value>)",
        },
        destructive: {
          DEFAULT: "hsl(0 72% 48%)",
          foreground: "hsl(0 0% 100%)",
          soft: "hsl(0 86% 97%)",
        },
        success: {
          DEFAULT: "hsl(162 76% 30%)",
          foreground: "hsl(0 0% 100%)",
          soft: "hsl(160 64% 95%)",
        },
        warning: {
          DEFAULT: "hsl(29 90% 40%)",
          foreground: "hsl(0 0% 100%)",
          soft: "hsl(38 92% 95%)",
        },
        /* 企业微信提醒预览的实际投递样式，避免在页面里散落十六进制 */
        wechat: {
          bg: "#f4f4f4",
          title: "#1f1f1f",
          text: "#5a5a5a",
          green: "#07c160",
        },
        /* 侧栏导航态：选中是深蓝黑（不是纯黑，也不是主色），未选中是灰蓝色。
           单独成一组是为了让侧栏可以有自己的语气，不和主色按钮绑定。 */
        "nav-active": {
          DEFAULT: "rgb(var(--ui-nav-active, 24 27 39) / <alpha-value>)",
          foreground: "rgb(var(--ui-nav-active-foreground, 255 255 255) / <alpha-value>)",
        },
        "nav-idle": "#5C6579",
      },
      boxShadow: {
        /* 卡片只保留一层贴地的接触阴影，定义感交给 border，避免「糊在背景上」 */
        card: "0 1px 2px 0 rgb(15 23 42 / 0.04)",
        /* 需要真正浮起来的场景：可点卡片 hover、弹层 */
        lift: "0 12px 32px -16px rgb(15 23 42 / 0.18), 0 1px 2px 0 rgb(15 23 42 / 0.04)",
        /* 青绿焦点卡自带彩色投影，让它在灰白背景上有重量 */
        hero: "0 18px 40px -22px hsl(176 58% 30% / 0.55)",
        /* 玻璃面板比普通卡片多一层扩散阴影，浮起感才够；
           最内层那道白色 inset 是玻璃的「高光边」，是磨砂质感成立的关键 */
        glass: "inset 0 1px 0 0 rgb(255 255 255 / 0.7), 0 8px 32px -12px rgb(15 23 42 / 0.16), 0 1px 2px 0 rgb(15 23 42 / 0.04)",
        /* 悬浮侧栏面板：极弱、扩散面积大。只负责产生空间层次，不该被"看见" */
        panel: "0 8px 30px rgba(20, 30, 50, 0.05), 0 1px 2px rgba(20, 30, 50, 0.03)",
        focus: "0 0 0 3px rgb(var(--ui-primary, 26 28 40) / 0.16)",
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
    },
  },
  plugins: [],
};

export default config;
