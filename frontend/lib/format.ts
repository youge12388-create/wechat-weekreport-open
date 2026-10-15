/** 周期计算与日期格式化（浏览器本地时区）。 */

export interface PeriodOption {
  key: string;
  label: string;
  start: string;
  end: string;
}

function pad(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

function iso(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function startOfWeek(d: Date): Date {
  const date = new Date(d);
  const day = (date.getDay() + 6) % 7; // 周一=0
  date.setDate(date.getDate() - day);
  return date;
}

export function currentWeek(): { start: string; end: string } {
  const monday = startOfWeek(new Date());
  const sunday = new Date(monday);
  sunday.setDate(sunday.getDate() + 6);
  return { start: iso(monday), end: iso(sunday) };
}

/**
 * 默认周期：没到「周五 21:00」就看上周，过了就看本周。
 * 与后端 view_service.default_period 是同一套规则，改一处必须改另一处。
 * 周报周五晚上才交，周一一早盯着本周只会看到满屏假警报。
 */
const DUE_WEEKDAY = 4; // 周五（周一=0）
const DUE_HOUR = 21;

export function defaultPeriodKey(now: Date = new Date()): string {
  const monday = startOfWeek(now);
  const due = new Date(monday);
  due.setDate(due.getDate() + DUE_WEEKDAY);
  due.setHours(DUE_HOUR, 0, 0, 0);
  return now < due ? "last_week" : "this_week";
}

export function periodOptions(): PeriodOption[] {
  const now = new Date();
  const thisMonday = startOfWeek(now);
  const thisSunday = new Date(thisMonday);
  thisSunday.setDate(thisSunday.getDate() + 6);

  const lastMonday = new Date(thisMonday);
  lastMonday.setDate(lastMonday.getDate() - 7);
  const lastSunday = new Date(thisMonday);
  lastSunday.setDate(lastSunday.getDate() - 1);

  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
  const monthEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);

  const lastMonthEnd = new Date(now.getFullYear(), now.getMonth(), 0);
  const lastMonthStart = new Date(now.getFullYear(), now.getMonth() - 1, 1);

  const quarterStartMonth = Math.floor(now.getMonth() / 3) * 3;
  const quarterStart = new Date(now.getFullYear(), quarterStartMonth, 1);
  const quarterEnd = new Date(now.getFullYear(), quarterStartMonth + 3, 0);

  return [
    { key: "this_week", label: "本周", start: iso(thisMonday), end: iso(thisSunday) },
    { key: "last_week", label: "上周", start: iso(lastMonday), end: iso(lastSunday) },
    { key: "this_month", label: "本月", start: iso(monthStart), end: iso(monthEnd) },
    { key: "last_month", label: "上月", start: iso(lastMonthStart), end: iso(lastMonthEnd) },
    { key: "this_quarter", label: "本季", start: iso(quarterStart), end: iso(quarterEnd) },
    { key: "this_year", label: "今年", start: iso(new Date(now.getFullYear(), 0, 1)), end: iso(new Date(now.getFullYear(), 11, 31)) },
  ];
}

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * URL 区间 → 完整周期对象（统一入口，替代散落的 periodKeyForRange + options.find）。
 * 匹配到快选档就用快选档；合法但不匹配的区间接成「自定义」，不再静默回退到默认周期
 * （此前分享出去的链接和页面实际看到的周期会对不上）。
 */
export function periodFromUrl(
  start: string | null | undefined,
  end: string | null | undefined,
  options: PeriodOption[] = periodOptions(),
  fallbackKey = defaultPeriodKey(),
): PeriodOption {
  if (
    start && end
    && ISO_DATE_RE.test(start) && ISO_DATE_RE.test(end)
    && start <= end
  ) {
    const matched = options.find((option) => option.start === start && option.end === end);
    if (matched) return matched;
    return { key: "custom", label: "自定义", start, end };
  }
  return options.find((option) => option.key === fallbackKey) ?? options[0];
}

/**
 * 把当前周期写进地址栏（replaceState，不触发导航、不触发重渲染）。
 * 目的：用户选好一段区间后，复制地址栏发给别人，对方打开看到的是同一段数据 ——
 * 「银行查流水」的心智：查到一段，链接就是凭证。
 */
export function syncPeriodToUrl(start: string, end: string): void {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  if (params.get("start") === start && params.get("end") === end) return;
  params.set("start", start);
  params.set("end", end);
  const query = params.toString();
  window.history.replaceState(null, "", `${window.location.pathname}${query ? `?${query}` : ""}`);
}

/**
 * 将 URL 中的统计区间映射回页面上的周期标签。
 * 跨页面跳转时统一使用这个入口，避免首页显示「上周」、详情页却悄悄回到「本周」。
 */
export function periodKeyForRange(
  start: string | null | undefined,
  end: string | null | undefined,
  fallback = defaultPeriodKey(),
): string {
  if (!start || !end) return fallback;
  return periodOptions().find((option) => option.start === start && option.end === end)?.key ?? fallback;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return value.slice(0, 10);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "-";
  // 后端 datetime 统一为 UTC naive；没有时区标记时必须显式按 UTC 解析，
  // 否则上海浏览器会把 19:00 的计划点显示成 11:00。
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  const date = new Date(hasTimezone ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16).replace("T", " ");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function periodLabel(start: string, end: string): string {
  return `${formatDate(start)} ~ ${formatDate(end)}`;
}
