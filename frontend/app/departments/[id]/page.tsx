"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Building2, CheckCircle2, Clock3 } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingBlock,
  PageHeader,
  StatCard,
} from "@/components/ui";
import { apiGet } from "@/lib/api";
import { AnalysisScanButton } from "@/components/analysis-scan-button";
import { periodFromUrl, periodLabel } from "@/lib/format";
import { PeriodPicker, useAsyncData } from "@/lib/use-data";
import type { DepartmentAnalysis } from "@/lib/types";

interface DepartmentDetail {
  id: number;
  name: string;
  parent_id: number | null;
  status: string;
  source: string;
  employee_count: number;
  period: { start: string; end: string };
  stats: {
    employee_total: number;
    submitted: number;
    unsubmitted: number;
    reports_count: number;
  };
  employees: Array<{
    id: number;
    name: string;
    position: string | null;
    status: string;
    status_label: string;
    has_submitted: boolean;
  }>;
  analysis: DepartmentAnalysis;
}

export default function DepartmentDetailPage() {
  const params = useParams<{ id: string }>();
  const id = Number(params.id);
  const [period, setPeriod] = useState(() => {
    if (typeof window === "undefined") return periodFromUrl(null, null);
    const search = new URLSearchParams(window.location.search);
    return periodFromUrl(search.get("start"), search.get("end"));
  });
  const [weeks, setWeeks] = useState<1 | 4 | 12>(1);

  const deps = useMemo(() => [period.key, period.start, period.end], [period.key, period.start, period.end]);
  const detail = useAsyncData<DepartmentDetail>(
    () => apiGet(`/api/departments/${id}`, { start: period.start, end: period.end, weeks }),
    [...deps, weeks]
  );

  if (detail.error) {
    return <Card><ErrorState message={detail.error} onRetry={detail.reload} /></Card>;
  }

  const data = detail.data;
  const submitRate = data?.stats.employee_total ? Math.round((data.stats.submitted / data.stats.employee_total) * 100) : 0;
  const coverage = data?.analysis.coverage;
  const analysisReady = Boolean(
    coverage && coverage.reports_total > 0 && coverage.analyzed_reports >= coverage.reports_total
  );
  const analysisPending = Boolean(
    coverage && coverage.reports_total > 0 && coverage.analyzed_reports < coverage.reports_total
  );
  const periodQuery = `start=${period.start}&end=${period.end}`;
  const reloadAnalysis = () => detail.reload();

  return (
    <div>
      <Button variant="ghost" size="sm" onClick={() => window.history.back()} className="mb-3 -ml-2 text-muted-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> 返回
      </Button>

      {detail.loading || !data ? (
        <Card><LoadingBlock rows={6} /></Card>
      ) : (
        <div className="space-y-4">
          <PageHeader
            title={
              <span className="flex items-center gap-2">
                <Building2 className="h-5 w-5 text-primary" />
                {data.name}
              </span>
            }
            desc={<>统计周期：{periodLabel(data.period.start, data.period.end)}</>}
            action={<PeriodPicker value={period} onChange={setPeriod} />}
          />

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label="部门人数" value={data.stats.employee_total} />
            <StatCard label="已提交" value={data.stats.submitted} accent="green" />
            <StatCard
              label="未提交"
              value={data.stats.unsubmitted}
              accent={data.stats.unsubmitted > 0 ? "red" : "neutral"}
            />
            <StatCard label="提交率" value={`${submitRate}%`} accent="blue" hint={`周期内 ${data.stats.reports_count} 篇周报`} />
          </div>

          <Card className="border-primary/20 bg-primary-soft/40 px-4 py-3 text-sm text-foreground/80">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p>当前范围已分析 <b className="num">{data.analysis.coverage.analyzed_reports}/{data.analysis.coverage.reports_total}</b> 篇周报（{data.analysis.coverage.rate}%）。{analysisPending ? "结构化事项和卡点将在分析完成后更新。" : data.analysis.summary}</p>
              <div className="flex flex-wrap items-center gap-3">
                {analysisPending && <AnalysisScanButton start={period.start} end={period.end} departmentId={id} onChanged={reloadAnalysis} />}
                <Link href={`/ai?${periodQuery}&department_id=${id}`} className="shrink-0 text-xs font-medium text-primary hover:underline">按本部门检索 AI 周报</Link>
              </div>
            </div>
          </Card>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div><h2 className="text-base font-semibold">部门工作分析</h2><p className="mt-0.5 text-xs text-muted-foreground">工作事项、卡点和趋势只统计已完成结构化分析的周报。</p></div>
            <div className="flex rounded-pill border border-input bg-surface p-0.5">{([1, 4, 12] as const).map((value) => <button key={value} onClick={() => setWeeks(value)} className={`rounded-pill px-3.5 py-1.5 text-xs transition-colors ${weeks === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>近 {value} 周</button>)}</div>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label="部门状态" value={analysisReady ? data.analysis.status : coverage?.reports_total ? "待分析" : "—"} accent={analysisReady && data.analysis.status === "注意" ? "amber" : analysisReady ? "green" : "neutral"} />
            <StatCard label="完成/成果" value={analysisReady ? data.analysis.counts.completed : coverage?.reports_total ? "待分析" : "—"} accent={analysisReady ? "green" : "neutral"} />
            <StatCard label="进行中" value={analysisReady ? data.analysis.counts.ongoing : coverage?.reports_total ? "待分析" : "—"} accent={analysisReady ? "blue" : "neutral"} />
            <StatCard label="当前卡点" value={analysisReady ? data.analysis.blockers.length : coverage?.reports_total ? "待分析" : "—"} accent={analysisReady && data.analysis.blockers.length ? "amber" : "neutral"} />
          </div>

          <Card>
            <CardHeader title="部门当前卡点" desc="只显示可追溯到本部门周报的开放卡点" />
            {analysisPending ? <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-6"><div className="flex items-start gap-2 text-sm text-muted-foreground"><span className="mt-1 h-2 w-2 shrink-0 animate-pulse rounded-full bg-primary" /><div><p className="font-medium text-foreground">等待 AI 分析本期周报</p><p className="mt-1 text-xs">当前 {coverage?.analyzed_reports}/{coverage?.reports_total} 篇已完成，不把未分析误记为没有卡点。</p></div></div><AnalysisScanButton start={period.start} end={period.end} departmentId={id} onChanged={reloadAnalysis} /></div> : data.analysis.blockers.length === 0 ? <EmptyState title="当前没有可确认的卡点" desc="仅统计已分析且能回到本部门周报原文的事项。" /> : <ul className="space-y-2 px-4 pb-4">{data.analysis.blockers.map((blocker) => <li key={blocker.id}><Link href={`/reports/${blocker.sources[0]?.report_id}?${periodQuery}`} className="flex items-center justify-between gap-3 rounded-control bg-muted/40 px-3.5 py-3 transition-colors hover:bg-muted/60"><div><p className="text-sm font-medium">{blocker.title}</p><p className="mt-1 text-xs text-muted-foreground">持续 {blocker.duration_weeks} 周 · 有原始周报依据</p></div><Badge tone={blocker.risk_level === "高" ? "red" : blocker.risk_level === "中" ? "amber" : "neutral"}>{blocker.risk_level}风险</Badge></Link></li>)}</ul>}
          </Card>

          <Card>
            <CardHeader title="部门员工" desc={<>点击员工查看详情 · {data.employees.length} 人</>} />
            {data.employees.length === 0 ? (
              <EmptyState title="本部门暂无在职员工" />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead className="border-b border-border bg-muted/40">
                    <tr>
                      <th className="thead-cell">姓名</th>
                      <th className="thead-cell">岗位</th>
                      <th className="thead-cell">状态</th>
                      <th className="thead-cell">本期提交</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.employees.map((emp) => (
                      <tr key={emp.id} className="border-b border-border/60 transition-colors hover:bg-muted/40">
                        <td className="tbody-cell">
                          <Link href={`/employees/${emp.id}?${periodQuery}`} className="font-medium text-primary hover:underline">
                            {emp.name}
                          </Link>
                        </td>
                        <td className="tbody-cell text-muted-foreground">{emp.position ?? "-"}</td>
                        <td className="tbody-cell">
                          <Badge tone={emp.status === "active" ? "green" : "neutral"}>{emp.status_label}</Badge>
                        </td>
                        <td className="tbody-cell">
                          {emp.has_submitted ? (
                            <span className="inline-flex items-center gap-1 text-xs font-medium text-success">
                              <CheckCircle2 className="h-3.5 w-3.5" /> 已提交
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-xs font-medium text-warning">
                              <Clock3 className="h-3.5 w-3.5" /> 未提交
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
