"use client";

import React, { useMemo } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ArrowLeft, ArrowUpRight, CalendarCheck2 } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingBlock,
  StatCard,
} from "@/components/ui";
import Chart from "@/components/chart";
import type { EChartsOption } from "echarts";
import { barSeries, chartColorsForTheme, lineSeries } from "@/lib/chart-theme";
import { useTheme } from "@/components/theme-provider";
import { apiGet } from "@/lib/api";
import { AnalysisScanButton } from "@/components/analysis-scan-button";
import { formatDate, formatDateTime, periodFromUrl, periodLabel, periodOptions } from "@/lib/format";
import { PeriodPicker, useAsyncData } from "@/lib/use-data";
import type { EmployeeAnalysis, EmployeeItem, ReportItem } from "@/lib/types";

const TYPE_LABELS: Record<string, string> = {
  weekly: "周报",
  daily: "日报",
  monthly: "月报",
  other: "其他",
};

export default function EmployeeDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const id = Number(params.id);
  const { theme } = useTheme();
  const chartColors = useMemo(() => chartColorsForTheme(theme), [theme]);

  const employee = useAsyncData<EmployeeItem>(() => apiGet(`/api/employees/${id}`), [id]);
  const reports = useAsyncData<ReportItem[]>(() => apiGet(`/api/employees/${id}/reports`), [id]);

  const options = periodOptions().filter((option) => option.key !== "this_month");
  const [period, setPeriod] = React.useState(() => {
    if (typeof window === "undefined") return periodFromUrl(null, null, options);
    const search = new URLSearchParams(window.location.search);
    return periodFromUrl(search.get("start"), search.get("end"), options);
  });
  const analysis = useAsyncData<EmployeeAnalysis>(
    () => apiGet(`/api/employees/${id}/analysis`, { end: period.end, weeks: 8 }),
    [id, period.end]
  );
  const hasThisWeek = (reports.data ?? []).some(
    (r) => r.period_start && r.period_start >= period.start && r.period_start <= period.end
  );
  const periodQuery = `start=${period.start}&end=${period.end}`;
  const analysisPending = Boolean(
    analysis.data &&
      analysis.data.coverage.reports_total > analysis.data.coverage.analyzed_reports
  );

  const trendOption = useMemo<EChartsOption>(() => {
    const data = [...(reports.data ?? [])].sort(
      (a, b) => (a.period_start ?? "").localeCompare(b.period_start ?? "")
    );
    const grouped = new Map<string, number>();
    for (const r of data) {
      if (r.period_start) grouped.set(r.period_start, (grouped.get(r.period_start) ?? 0) + 1);
    }
    const keys = [...grouped.keys()];
    return {
      xAxis: { type: "category", data: keys.map((k) => formatDate(k).slice(5)) },
      yAxis: { type: "value", minInterval: 1 },
      series: [
        lineSeries({
          name: "提交数",
          data: keys.map((k) => grouped.get(k) ?? 0),
          color: chartColors.accent,
          area: true,
        }),
      ],
    };
  }, [chartColors, reports.data]);

  const workTrendOption = useMemo<EChartsOption>(() => ({
    legend: { data: ["完成/成果", "进行中", "问题事项", "质量分"] },
    xAxis: { type: "category", data: (analysis.data?.trend ?? []).map((p) => formatDate(p.period_start).slice(5)) },
    yAxis: [{ type: "value", minInterval: 1 }, { type: "value", min: 0, max: 100, splitLine: { show: false } }],
    series: [
      barSeries({ name: "完成/成果", data: (analysis.data?.trend ?? []).map((p) => p.completed), color: chartColors.accent }),
      barSeries({ name: "进行中", data: (analysis.data?.trend ?? []).map((p) => p.ongoing), color: chartColors.primary }),
      lineSeries({ name: "问题事项", data: (analysis.data?.trend ?? []).map((p) => p.blockers), color: chartColors.warning, width: 2 }),
      lineSeries({ name: "质量分", data: (analysis.data?.trend ?? []).map((p) => p.quality_score), color: chartColors.neutral, yAxisIndex: 1, width: 2 }),
    ],
  }), [analysis.data, chartColors]);

  if (employee.error) {
    return <Card><ErrorState message={employee.error} onRetry={employee.reload} /></Card>;
  }
  if (employee.loading || !employee.data) {
    return <Card><LoadingBlock rows={6} /></Card>;
  }

  const person = employee.data;
  const reportList = reports.data ?? [];

  return (
    <div>
      <Button variant="ghost" size="sm" onClick={() => window.history.back()} className="mb-3 -ml-2 text-muted-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> 返回
      </Button>

      {/* 个人信息 */}
      <Card className="mb-4 p-4">
        <div className="flex items-center gap-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10 text-xl font-bold text-primary">
            {person.name.slice(0, 1)}
          </div>
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold">{person.name}</h1>
              <Badge tone={person.status === "active" ? "green" : "neutral"}>{person.status_label}</Badge>
              {hasThisWeek && <Badge tone="blue"><CalendarCheck2 className="mr-1 h-3 w-3" />{period.key === "this_week" ? "本周" : period.key === "last_week" ? "上周" : "本期"}已提交</Badge>}
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {person.department_name ?? "未分配部门"}
              {person.position ? ` · ${person.position}` : ""}
            </p>
          </div>
          <div className="text-right text-xs text-muted-foreground">
            <p>最近数据同步</p>
            <p className="mt-0.5 text-sm tabular-nums text-foreground">{formatDateTime(person.last_seen_at)}</p>
          </div>
        </div>
      </Card>

      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="累计周报" value={person.reports_total} />
        <StatCard label={`${period.key === "this_week" ? "本周" : period.key === "last_week" ? "上周" : "本期"}提交`} value={hasThisWeek ? "已提交" : "未提交"} accent={hasThisWeek ? "green" : "red"} />
        <StatCard label="最近提交" value={formatDateTime(reportList[0]?.submitted_at)} hint={reportList[0]?.title ?? ""} />
        <StatCard label="数据来源" value={person.source === "wecom" ? "企业微信" : person.source} accent="neutral" />
      </div>

      <Card className="mb-4 border-primary/20 bg-primary-soft/40 px-4 py-3 text-sm text-foreground/80">
        {analysis.loading ? "正在汇总已分析周报…" : analysis.data ? <div className="flex flex-wrap items-center justify-between gap-3"><p>近 8 周已分析 <b className="num">{analysis.data.coverage.analyzed_reports}/{analysis.data.coverage.reports_total}</b> 篇（{analysis.data.coverage.rate}%）。{analysisPending ? "结构化事项和卡点将在分析完成后更新。" : analysis.data.summary}</p>{analysisPending && <AnalysisScanButton start={analysis.data.period.start} end={analysis.data.period.end} employeeId={id} onChanged={analysis.reload} />}</div> : "暂无结构化分析数据。"}
      </Card>

      <div className="mb-4 grid grid-cols-1 gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3"><CardHeader title="工作与质量趋势" desc="完成/进行中/问题事项和周报质量分，仅基于已分析周报" />{analysis.loading ? <LoadingBlock /> : analysis.error ? <ErrorState message={analysis.error} onRetry={analysis.reload} /> : analysisPending ? <EmptyState title="等待 AI 分析" desc={`当前已分析 ${analysis.data?.coverage.analyzed_reports}/${analysis.data?.coverage.reports_total} 篇，完成后才会生成趋势。`} /> : analysis.data?.trend.every((p) => p.analyzed_reports === 0) ? <EmptyState desc="暂无结构化周报可生成工作趋势" /> : <div className="p-3"><Chart option={workTrendOption} height={280} /></div>}</Card>
        <Card className="lg:col-span-2"><CardHeader title="当前需要关注" desc="带问题、依赖或风险证据的未完成事项" />{analysis.loading ? <LoadingBlock rows={4} /> : analysis.data?.current_blockers.length ? <ul className="space-y-2 px-4 pb-4">{analysis.data.current_blockers.slice(0, 5).map((task) => <li key={task.task_id}><Link href={`/reports/${task.sources[0]?.report_id}?${periodQuery}`} className="block rounded-control bg-muted/40 px-3.5 py-3 transition-colors hover:bg-muted/60"><p className="text-sm font-medium">{task.title}</p><p className="mt-1 text-xs text-muted-foreground">持续 {task.duration_weeks} 周 · 查看周报依据</p></Link></li>)}</ul> : analysisPending ? <div className="px-4 py-6"><EmptyState title="等待 AI 分析" desc="当前没有可确认的卡点并不代表原文中没有资源需求。" /></div> : <EmptyState title="当前没有可确认的卡点" desc="仅显示有结构化原文依据的事项" />}</Card>
      </div>

      {analysis.data?.long_tasks.length ? <Card className="mb-4"><CardHeader title="持续超过两周的事项" desc="尚未标记完成，点击可回看原始周报" /><ul className="space-y-2 px-4 pb-4">{analysis.data.long_tasks.map((task) => <li key={task.task_id}><Link href={`/reports/${task.sources[0]?.report_id}`} className="flex items-center justify-between gap-3 rounded-control bg-muted/40 px-3.5 py-3 transition-colors hover:bg-muted/60"><span className="text-sm font-medium">{task.title}</span><span className="shrink-0 text-xs text-muted-foreground">持续 {task.duration_weeks} 周 →</span></Link></li>)}</ul></Card> : null}

      <div className="mb-4 flex flex-wrap items-center justify-between gap-2"><span className="text-xs text-muted-foreground">统计周期：<span className="font-medium text-foreground">{periodLabel(period.start, period.end)}</span></span><PeriodPicker value={period} onChange={setPeriod} options={options} /></div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        {/* 提交趋势 */}
        <Card className="lg:col-span-2">
          <CardHeader title="提交趋势" desc="按周报周期统计的提交数量" />
          <div className="p-3">
            {reports.loading ? (
              <LoadingBlock />
            ) : reportList.length === 0 ? (
              <EmptyState title="暂无周报记录" />
            ) : (
              <Chart option={trendOption} height={260} />
            )}
          </div>
        </Card>

        {/* 历史周报 */}
        <Card className="lg:col-span-3">
          <CardHeader title="历史周报" desc={<>共 {reportList.length} 篇 · 点击记录或“查看”打开正文</>} />
          {reports.loading ? (
            <LoadingBlock rows={5} />
          ) : reportList.length === 0 ? (
            <EmptyState title="暂无周报记录" desc="同步周报后这里会展示历史记录" />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead className="border-b border-border bg-muted/40">
                  <tr>
                    <th className="thead-cell">周期</th>
                    <th className="thead-cell">标题</th>
                    <th className="thead-cell">类型</th>
                    <th className="thead-cell">提交时间</th>
                    <th className="thead-cell text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {reportList.map((r) => (
                    <tr
                      key={r.id}
                      role="link"
                      tabIndex={0}
                      onClick={() => router.push(`/reports/${r.id}`)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          router.push(`/reports/${r.id}`);
                        }
                      }}
                      className="cursor-pointer border-b border-border/60 transition-colors hover:bg-primary-soft/30"
                    >
                      <td className="tbody-cell tabular-nums">
                        {r.period_start && r.period_end
                          ? `${formatDate(r.period_start)} ~ ${formatDate(r.period_end)}`
                          : "-"}
                      </td>
                      <td className="tbody-cell">
                        <Link href={`/reports/${r.id}`} onClick={(event) => event.stopPropagation()} className="hover:text-primary hover:underline">
                          {r.title ?? "(无标题)"}
                        </Link>
                      </td>
                      <td className="tbody-cell">
                        <Badge tone="neutral">{TYPE_LABELS[r.report_type] ?? r.report_type}</Badge>
                      </td>
                      <td className="tbody-cell tabular-nums text-muted-foreground">{formatDateTime(r.submitted_at)}</td>
                      <td className="tbody-cell text-right">
                        <Link
                          href={`/reports/${r.id}`}
                          onClick={(event) => event.stopPropagation()}
                          className="inline-flex items-center gap-1 rounded-control bg-primary-soft/60 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary-soft"
                        >
                          查看 <ArrowUpRight className="h-3 w-3" />
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

    </div>
  );
}
