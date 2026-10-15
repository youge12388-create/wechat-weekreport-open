import Link from "next/link";

/**
 * 404 页（Next.js App Router 约定文件）。
 *
 * 此前不存在：访问不存在的路径时展示 Next.js 默认的英文 404，
 * 与全站中文界面和导航结构脱节。这里给出明确的回站入口。
 */
export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="text-sm font-medium text-muted-foreground">404</p>
      <h1 className="text-xl font-semibold tracking-tight">页面不存在</h1>
      <p className="max-w-md text-sm leading-relaxed text-muted-foreground">
        你访问的地址不存在或已被移除。可能链接已过期，或地址输入有误。
      </p>
      <Link
        href="/dashboard"
        className="mt-2 inline-flex h-9 items-center rounded-control bg-primary px-4 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
      >
        回管理简报
      </Link>
    </div>
  );
}
