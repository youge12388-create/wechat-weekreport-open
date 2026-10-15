import type { ReactNode } from "react";

// 处理中心的周期卡片和权限状态来自运行时接口；禁止把页面 HTML 长期
// 静态缓存，否则发布后浏览器或反向代理可能继续复用带有旧入口的页面。
export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function BlockersLayout({ children }: { children: ReactNode }) {
  return children;
}
