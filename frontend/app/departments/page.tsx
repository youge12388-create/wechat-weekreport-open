"use client";

import React, { useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  PageIntro,
  ProgressBar,
} from "@/components/ui";
import { apiGet } from "@/lib/api";
import { periodFromUrl, periodLabel } from "@/lib/format";
import { PeriodPicker, useAsyncData } from "@/lib/use-data";
import type { DashboardDepartments } from "@/lib/types";

export default function DepartmentsPage() {
  const router = useRouter();
  const [period, setPeriod] = useState(() => {
    if (typeof window === "undefined") return periodFromUrl(null, null);
    const params = new URLSearchParams(window.location.search);
    return periodFromUrl(params.get("start"), params.get("end"));
  });

  const deps = useMemo(() => [period.key, period.start, period.end], [period.key, period.start, period.end]);
  const data = useAsyncData<DashboardDepartments>(
    () => apiGet("/api/dashboard/departments", { start: period.start, end: period.end }),
    deps
  );

  // 未提交多的排前面：先处理问题最大的部门
  const items = useMemo(
    () =>
      [...(data.data?.items ?? [])].sort(
        (a, b) => b.unsubmitted - a.unsubmitted || b.employee_count - a.employee_count
      ),
    [data.data]
  );

  return (
    <div>
      <PageHeader
        title="部门"
        desc={
          <>
            统计周期：<span className="font-medium text-foreground">{periodLabel(period.start, period.end)}</span>
          </>
        }
        action={<PeriodPicker value={period} onChange={setPeriod} />}
      />

      <PageIntro>
        对比各部门的周报提交进度，<b>排在最上面的是欠得最多的部门</b>。点任意一行可以看这个部门每个人的提交情况。
      </PageIntro>

      {data.error && <Card className="mb-4"><ErrorState message={data.error} onRetry={data.reload} /></Card>}

      <Card>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="border-b border-border bg-muted/40">
              <tr>
                <th className="thead-cell">部门</th>
                <th className="thead-cell text-right">人数</th>
                <th className="thead-cell text-right">已提交</th>
                <th className="thead-cell text-right">未提交</th>
                <th className="thead-cell w-48">提交进度</th>
                <th className="thead-cell text-right">周报数</th>
                <th className="thead-cell text-right">AI 分析</th>
              </tr>
            </thead>
            <tbody>
              {data.loading ? (
                <tr><td colSpan={7}><LoadingBlock rows={5} /></td></tr>
              ) : items.length === 0 ? (
                <tr><td colSpan={7}><EmptyState /></td></tr>
              ) : (
                items.map((dept) => (
                  <tr
                    key={dept.id}
                    className="cursor-pointer border-b border-border/60 transition-colors last:border-0 hover:bg-muted/40"
                    onClick={() => router.push(`/departments/${dept.id}?start=${period.start}&end=${period.end}`)}
                  >
                    <td className="tbody-cell font-medium">
                      {dept.name}
                      {dept.exempt && (
                        <Badge tone="neutral" className="ml-2">免提交</Badge>
                      )}
                    </td>
                    <td className="tbody-cell text-right num">{dept.employee_count}</td>
                    <td className="tbody-cell text-right num text-success">{dept.submitted}</td>
                    <td className="tbody-cell text-right num">
                      {dept.unsubmitted > 0 ? (
                        <span className="text-warning">{dept.unsubmitted}</span>
                      ) : (
                        dept.unsubmitted
                      )}
                    </td>
                    <td className="tbody-cell">
                      <ProgressBar value={dept.submitted} total={dept.employee_count} />
                    </td>
                    <td className="tbody-cell text-right num text-muted-foreground">
                      {dept.reports_count ?? "-"}
                    </td>
                    <td className="tbody-cell text-right">
                      {!dept.analysis_coverage || dept.analysis_coverage.reports_total === 0 ? (
                        <span className="text-xs text-muted-foreground">暂无周报</span>
                      ) : dept.analysis_coverage.analyzed_reports < dept.analysis_coverage.reports_total ? (
                        <Badge tone="amber">待分析 {dept.analysis_coverage.analyzed_reports}/{dept.analysis_coverage.reports_total}</Badge>
                      ) : (
                        <Badge tone="green">已完成</Badge>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
