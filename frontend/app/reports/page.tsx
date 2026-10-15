"use client";

import React, { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowUpRight, Download, Search, X } from "lucide-react";
import {
  Avatar,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Input,
  LoadingBlock,
  PageHeader,
  Pagination,
  Select,
} from "@/components/ui";
import { apiCacheKey, apiDownload, apiGet } from "@/lib/api";
import { formatDateTime, formatDate, periodLabel } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type {
  DepartmentItem,
  EmployeeItem,
  ReportItem,
  ReportListResponse,
} from "@/lib/types";

/**
 * 列表行只需要这几个字段：按员工查历史（/api/employees/{id}/reports）返回的结构
 * 用这个子集统一。
 */
type ReportRow = Pick<
  ReportItem,
  | "id"
  | "employee_id"
  | "employee_name"
  | "department_name"
  | "report_type"
  | "title"
  | "content_language"
  | "period_start"
  | "period_end"
  | "submitted_at"
>;

const TYPE_LABELS: Record<string, string> = {
  weekly: "周报",
  daily: "日报",
  monthly: "月报",
  other: "其他",
};

function initialPage(value: string | null): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : 1;
}

function ReportHistoryCard({
  report,
  href,
}: {
  report: ReportRow;
  href: string;
}) {
  return (
    <Link href={href} className="group block focus-visible:outline-none">
      <Card className="h-full p-4 transition-colors group-hover:border-primary/35 group-hover:bg-primary-soft/20 group-focus-visible:ring-2 group-focus-visible:ring-ring group-focus-visible:ring-offset-2">
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="neutral">
              {TYPE_LABELS[report.report_type] ?? report.report_type}
            </Badge>
            {report.content_language === "english" && (
              <Badge tone="blue">英文</Badge>
            )}
          </div>
          <ArrowUpRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:-translate-y-0.5 group-hover:translate-x-0.5 group-hover:text-primary" />
        </div>
        <p className="mt-3 line-clamp-2 min-h-10 text-sm font-semibold leading-5">
          {report.title ?? "(无标题)"}
        </p>
        <p className="mt-2 text-xs tabular-nums text-foreground">
          {report.period_start && report.period_end
            ? `${formatDate(report.period_start)} ~ ${formatDate(report.period_end)}`
            : "周期未记录"}
        </p>
        <div className="mt-3 flex items-center justify-between gap-3 border-t border-border/70 pt-3 text-xs text-muted-foreground">
          <span className="tabular-nums">
            提交 {formatDateTime(report.submitted_at)}
          </span>
          <span className="font-medium text-primary">查看详情</span>
        </div>
      </Card>
    </Link>
  );
}

function ReportsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [departmentId, setDepartmentId] = useState(searchParams.get("department_id") ?? "");
  const [keyword, setKeyword] = useState(searchParams.get("q") ?? "");
  const [keywordInput, setKeywordInput] = useState(searchParams.get("q") ?? "");
  const [page, setPage] = useState(() => initialPage(searchParams.get("page")));
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const exportAllHistory = async () => {
    if (exporting) return;
    setExporting(true);
    setExportError(null);
    try {
      await apiDownload("/api/exports/reports", { all_history: true });
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "导出失败，请稍后重试");
    } finally {
      setExporting(false);
    }
  };

  const departments = useAsyncData<DepartmentItem[]>(() => apiGet("/api/departments"), [], {
    cacheKey: apiCacheKey("/api/departments"),
  });
  const deptList = departments.data ?? [];

  // 员工历史模式：URL 带 employee_id 时，这页就是「某人的周报历史」。
  // 从首页/处理中心/名单页点进来必须看到这个人的全部周报。
  const employeeIdParam = searchParams.get("employee_id");
  const employeeId =
    employeeIdParam && /^\d+$/.test(employeeIdParam) ? Number(employeeIdParam) : null;
  const isHistory = employeeId !== null;
  // 历史页默认只显示最近一期；全部历史收成右上角一个按钮，用同一个 query-param 切换
  // （与 employee_id 同一种做法），不新增路由、不新增接口。
  const historyViewAll = isHistory && searchParams.get("view") === "all";

  const employee = useAsyncData<EmployeeItem | null>(
    () => (employeeId === null ? Promise.resolve(null) : apiGet(`/api/employees/${employeeId}`)),
    [employeeId],
    { cacheKey: employeeId === null ? undefined : apiCacheKey(`/api/employees/${employeeId}`) }
  );
  const historyParams = { limit: 100 };
  const history = useAsyncData<ReportRow[]>(
    () =>
      employeeId === null
        ? Promise.resolve([])
        : apiGet(`/api/employees/${employeeId}/reports`, historyParams),
    [employeeId],
    {
      cacheKey:
        employeeId === null
          ? undefined
          : apiCacheKey(`/api/employees/${employeeId}/reports`, historyParams),
    }
  );
  // 「全部历史」改走 /api/reports 的员工筛选 + 真分页：/api/employees/{id}/reports
  // 只有 limit（上限 200）且不返回总数，任职多年的老员工第 101 篇之后就看不到了。
  // /api/reports 本身支持 employee_id 筛选，复用分页语义即可，不新增接口。
  const historyAllActive = isHistory && historyViewAll && employeeId !== null;
  const historyAll = useAsyncData<ReportListResponse | null>(
    () =>
      historyAllActive && employeeId !== null
        ? apiGet<ReportListResponse>("/api/reports", {
            employee_id: employeeId,
            all_history: true,
            page,
            page_size: 15,
          })
        : Promise.resolve(null),
    [historyAllActive, employeeId, page],
    {
      cacheKey:
        historyAllActive && employeeId !== null
          ? apiCacheKey("/api/reports", { employee_id: employeeId, all_history: true, page, page_size: 15 })
          : undefined,
    }
  );
  // 进入历史 / 全部历史模式时页码归位：page 与主名单共用一个 state，
  // 上一次浏览名单留下的第 N 页不能作为「全部历史」的起点。
  useEffect(() => {
    if (isHistory) setPage(1);
  }, [isHistory, historyViewAll]);

  // ---- 名单模式（默认）：应交周报人员 ----
  // 口径与统计/催交完全一致（reportable=true，后端同一条件函数）：
  // 在职且未免提交。企微同步入职自动进名单、离职自动出名单；
  // 人工豁免走「系统设置 → 免提交名单」，名单不需要手工维护第二份。
  const rosterParams = {
    reportable: true,
    department_id: departmentId || undefined,
    keyword: keyword || undefined,
    page,
    page_size: 50,
  };
  const roster = useAsyncData<{ items: EmployeeItem[]; total: number }>(
    () =>
      isHistory
        ? Promise.resolve({ items: [], total: 0 })
        : apiGet("/api/employees", rosterParams),
    [isHistory, departmentId, keyword, page],
    {
      cacheKey: isHistory ? undefined : apiCacheKey("/api/employees", rosterParams),
    }
  );
  const rosterRows = roster.data?.items ?? [];
  const rosterTotal = roster.data?.total ?? 0;

  useEffect(() => {
    if (isHistory) return; // 历史模式没有筛选需要写回 URL，同时保住 URL 上的 employee_id
    const params = new URLSearchParams();
    if (departmentId) params.set("department_id", departmentId);
    if (keyword) params.set("q", keyword);
    if (page > 1) params.set("page", String(page));
    const nextQuery = params.toString();
    if (nextQuery !== searchParams.toString()) {
      router.replace(nextQuery ? `/reports?${nextQuery}` : "/reports", { scroll: false });
    }
  }, [departmentId, isHistory, keyword, page, router, searchParams]);

  const applyFilters = useCallback(() => {
    setKeyword(keywordInput.trim());
    setPage(1);
  }, [keywordInput]);

  const resetFilters = useCallback(() => {
    setDepartmentId("");
    setKeywordInput("");
    setKeyword("");
    setPage(1);
  }, []);

  const hasActiveFilter = Boolean(departmentId || keywordInput.trim() || keyword);

  // ---- 历史模式数据 ----
  const rows = !isHistory ? [] : historyViewAll ? historyAll.data?.items ?? [] : history.data ?? [];
  const total = !isHistory
    ? 0
    : historyViewAll
      ? historyAll.data?.total ?? 0
      : rows.length;
  const listLoading = isHistory
    ? historyViewAll
      ? historyAll.loading
      : history.loading
    : roster.loading;
  const listError = isHistory
    ? historyViewAll
      ? historyAll.error
      : history.error
    : roster.error;
  const latestHistoryRow = rows[0];
  const historyCardHref = (report: ReportRow) =>
    report.period_start && report.period_end
      ? `/reports/${report.id}?start=${report.period_start}&end=${report.period_end}`
      : `/reports/${report.id}`;

  return (
    <div>
      <PageHeader
        title={isHistory ? (historyViewAll ? "全部历史周报" : "周报历史") : "周报查询"}
        desc={
          isHistory ? (
            historyViewAll ? (
              <>
                <span className="font-medium text-foreground">{employee.data?.name ?? "该员工"}</span>
                {employee.data?.department_name && (
                  <span className="text-muted-foreground"> · {employee.data.department_name}</span>
                )}
                <span className="mx-2 text-border">|</span>
                共 <span className="font-medium text-foreground">{total}</span> 篇 · 按提交时间倒序
              </>
            ) : (
              <>
                <span className="font-medium text-foreground">最近一期</span>
                <span className="mx-2 text-border">|</span>
                {latestHistoryRow?.period_start && latestHistoryRow?.period_end
                  ? periodLabel(latestHistoryRow.period_start, latestHistoryRow.period_end)
                  : "尚无提交记录"}
                {total > 1 && (
                  <>
                    <span className="mx-2 text-border">|</span>
                    另有 <span className="font-medium text-foreground">{total - 1}</span> 篇历史
                  </>
                )}
              </>
            )
          ) : (
            <>
              应交周报人员 <span className="font-medium text-foreground">{rosterTotal}</span> 人
              <span className="mx-2 text-border">|</span>
              名单 = 在职且未免提交，随通讯录同步自动增减
            </>
          )
        }
        action={
          isHistory ? (
            <div className="flex flex-wrap items-center gap-2">
              {historyViewAll ? (
                <Link
                  href={`/reports?employee_id=${employeeId}`}
                  className="inline-flex items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                >
                  返回最近一期
                </Link>
              ) : (
                total > 1 && (
                  <Link
                    href={`/reports?employee_id=${employeeId}&view=all`}
                    className="inline-flex items-center gap-1.5 rounded-pill bg-primary px-3.5 py-2 text-xs font-medium text-primary-foreground transition-opacity hover:opacity-90"
                  >
                    全部历史周报 ({total}) <ArrowUpRight className="h-3.5 w-3.5" />
                  </Link>
                )
              )}
              <Link
                href="/reports"
                className="inline-flex items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
              >
                返回人员名单 <ArrowUpRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => void exportAllHistory()}
                disabled={exporting}
                title="导出系统中已同步的全部历史周报，不受当前筛选影响"
                className="inline-flex items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
              >
                <Download className="h-3.5 w-3.5" /> {exporting ? "正在导出…" : "导出全部历史周报"}
              </button>
            </div>
          )
        }
      />

      {exportError && (
        <p role="alert" className="mb-4 rounded-control bg-destructive-soft px-4 py-2.5 text-sm text-destructive">
          导出失败：{exportError}
        </p>
      )}

      {isHistory ? (
        <Card className="mb-4 flex flex-wrap items-center justify-between gap-3 p-4">
          <div className="flex min-w-0 items-center gap-3">
            <Avatar name={employee.data?.name ?? "?"} size={40} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{employee.data?.name ?? "员工"}</p>
              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                {[employee.data?.department_name, employee.data?.position].filter(Boolean).join(" · ") ||
                  "部门未记录"}
              </p>
            </div>
          </div>
          {/* 篇数已经在右上角按钮与页头里出现。全部历史已改为真分页，
              「显示不全」这个约束只存在于最近一期模式。 */}
          {!historyViewAll && history.data && history.data.length >= 100 && (
            <div className="flex flex-wrap items-center gap-2">
              <Badge>仅显示最近 100 篇</Badge>
            </div>
          )}
        </Card>
      ) : (
        <Card className="mb-4 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={departmentId}
              onChange={(e) => {
                setDepartmentId(e.target.value);
                setPage(1);
              }}
              className="w-40"
            >
              <option value="">全部部门</option>
              {deptList.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </Select>

            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                value={keywordInput}
                onChange={(e) => setKeywordInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && applyFilters()}
                placeholder="搜索人员姓名"
                className="w-56 pl-8"
              />
            </div>

            <Button onClick={applyFilters}>
              <Search className="h-3.5 w-3.5" /> 查询
            </Button>
            {hasActiveFilter && (
              <Button variant="ghost" onClick={resetFilters}>
                <X className="h-3.5 w-3.5" /> 重置
              </Button>
            )}
            <p className="min-w-0 text-xs text-muted-foreground">
              点开人员即可查看TA的全部历史周报；免提交、离职的人不会出现在名单里。
            </p>
          </div>
        </Card>
      )}

      {listError ? (
        <Card>
          <ErrorState
            message={listError}
            onRetry={isHistory ? (historyViewAll ? historyAll.reload : history.reload) : roster.reload}
          />
        </Card>
      ) : isHistory ? (
        <div className="space-y-4">
          {listLoading ? (
            <Card><LoadingBlock rows={5} /></Card>
          ) : rows.length === 0 ? (
            <Card>
              <EmptyState
                title="这位员工还没有周报记录"
                desc="企业微信里没有同步到这个人的周报，或该员工还没有提交过"
                action={
                  <Link
                    href="/reports"
                    className="inline-flex items-center gap-1.5 rounded-pill border border-input bg-surface px-3.5 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                  >
                    返回人员名单 <ArrowUpRight className="h-3.5 w-3.5" />
                  </Link>
                }
              />
            </Card>
          ) : (
            <>
              {historyViewAll ? (
                <>
                  <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {rows.map((report) => (
                      <ReportHistoryCard
                        key={report.id}
                        report={report}
                        href={historyCardHref(report)}
                      />
                    ))}
                  </div>
                  <Pagination page={page} pageSize={15} total={total} onChange={setPage} />
                </>
              ) : (
                latestHistoryRow && (
                  <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                    <ReportHistoryCard
                      report={latestHistoryRow}
                      href={historyCardHref(latestHistoryRow)}
                    />
                  </div>
                )
              )}
            </>
          )}
        </div>
      ) : (
        <Card className="min-w-0 p-0">
          <div className="card-head card-head-line">
            <p className="text-sm font-semibold">应交周报人员名单</p>
            <span className="text-xs text-muted-foreground">
              点击一行查看该人员的全部历史周报
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-border bg-muted/60">
                <tr>
                  <th className="thead-cell">员工</th>
                  <th className="thead-cell">部门</th>
                  <th className="thead-cell">职位</th>
                  <th className="thead-cell">累计篇数</th>
                  <th className="thead-cell">最近提交</th>
                  <th className="thead-cell text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {listLoading ? (
                  <tr><td colSpan={6}><LoadingBlock rows={5} /></td></tr>
                ) : rosterRows.length === 0 ? (
                  <tr>
                    <td colSpan={6}>
                      <EmptyState
                        title={hasActiveFilter ? "筛选下没有人员" : "还没有应交周报的人员"}
                        desc={
                          hasActiveFilter
                            ? "清空筛选条件再试，或等下一次企业微信通讯录同步。"
                            : "名单由企业微信通讯录自动维护：同步到在职且未免提交的同事后，这里会自动出现。可在「系统设置」调整免提交名单。"
                        }
                        action={
                          hasActiveFilter ? (
                            <Button variant="outline" size="sm" onClick={resetFilters}>
                              清空筛选
                            </Button>
                          ) : undefined
                        }
                      />
                    </td>
                  </tr>
                ) : (
                  rosterRows.map((person) => (
                    <tr
                      key={person.id}
                      role="button"
                      tabIndex={0}
                      aria-label={`查看 ${person.name} 的历史周报`}
                      onClick={() => router.push(`/reports?employee_id=${person.id}`)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          router.push(`/reports?employee_id=${person.id}`);
                        }
                      }}
                      className="cursor-pointer border-b border-border/60 transition-colors hover:bg-muted/60"
                    >
                      <td className="tbody-cell">
                        <span className="inline-flex items-center gap-2 font-medium text-foreground">
                          <Avatar name={person.name} size={24} />
                          {person.name}
                        </span>
                      </td>
                      <td className="tbody-cell text-muted-foreground">{person.department_name ?? "-"}</td>
                      <td className="tbody-cell text-muted-foreground">{person.position ?? "-"}</td>
                      <td className="tbody-cell tabular-nums">
                        {person.reports_total ?? 0} 篇
                      </td>
                      <td className="tbody-cell whitespace-nowrap tabular-nums text-muted-foreground">
                        {person.last_report_at ? formatDateTime(person.last_report_at) : "从未提交"}
                      </td>
                      <td className="tbody-cell text-right">
                        <Link
                          href={`/reports?employee_id=${person.id}`}
                          onClick={(event) => event.stopPropagation()}
                          className="inline-flex items-center rounded-control bg-primary-soft/60 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary-soft"
                        >
                          查看历史 <ArrowUpRight className="ml-0.5 h-3 w-3" />
                        </Link>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
          <Pagination page={page} pageSize={50} total={rosterTotal} onChange={setPage} />
        </Card>
      )}
    </div>
  );
}

export default function ReportsPage() {
  return (
    <Suspense fallback={<Card><LoadingBlock rows={6} /></Card>}>
      <ReportsPageContent />
    </Suspense>
  );
}
