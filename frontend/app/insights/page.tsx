"use client";

import React, { useState } from "react";
import Link from "next/link";
import { ExternalLink, Lightbulb } from "lucide-react";
import { Badge, Card, EmptyState, ErrorState, LoadingBlock, PageHeader, PageIntro } from "@/components/ui";
import { AnalysisScanButton } from "@/components/analysis-scan-button";
import { apiGet } from "@/lib/api";
import { periodFromUrl, periodLabel, periodOptions } from "@/lib/format";
import { PeriodPicker, useAsyncData } from "@/lib/use-data";
import type { AnalysisCoverage, InsightItem, Period } from "@/lib/types";

export default function InsightsPage() {
  // 洞察按周回看（月/季粒度太粗，结论只对完整周期有意义），隐藏月份快捷档
  const options = periodOptions().filter((option) => option.key !== "this_month");
  const [period, setPeriod] = useState(() => {
    if (typeof window === "undefined") return periodFromUrl(null, null, options);
    const search = new URLSearchParams(window.location.search);
    return periodFromUrl(search.get("start"), search.get("end"), options);
  });
  const response = useAsyncData<{ period: Period; coverage: AnalysisCoverage; items: InsightItem[] }>(
    () => apiGet("/api/insights", { start: period.start, end: period.end }),
    [period.key, period.start, period.end]
  );
  const data = response.data;
  const coveragePending = Boolean(data && data.coverage.reports_total > data.coverage.analyzed_reports);
  return <div>
    <PageHeader title="管理洞察" desc={data ? `统计周期：${periodLabel(data.period.start, data.period.end)}` : "基于已分析周报的可追溯规则洞察"} action={<PeriodPicker value={period} onChange={setPeriod} options={options} />} />
    <PageIntro>洞察只使用已结构化且带原文来源的数据。它提示需要关注的事实，不对员工动机、态度或绩效作判断。</PageIntro>
    {data && <Card className="mb-4 border-primary/20 bg-primary-soft/40 px-4 py-3 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-3"><p>当前仅分析了 <b className="num">{data.coverage.analyzed_reports}/{data.coverage.reports_total}</b> 篇周报（{data.coverage.rate}%）；以下结论仅代表已分析样本。</p>{coveragePending && <AnalysisScanButton start={period.start} end={period.end} onChanged={response.reload} />}</div>
    </Card>}
    {response.loading ? <Card><LoadingBlock rows={6} /></Card> : response.error ? <Card><ErrorState message={response.error} onRetry={response.reload} /></Card> : data?.items.length === 0 ? <Card><EmptyState title={coveragePending ? "等待分析完成" : data?.coverage.reports_total === 0 ? "本期暂无周报" : "暂无可确认的管理洞察"} desc={coveragePending ? "本期仍有周报未完成结构化分析，完成后这里会显示有原文依据的管理信号。" : data?.coverage.reports_total === 0 ? "同步周报后，系统会基于真实来源生成管理洞察。" : "洞察必须有真实周报来源；当前已分析样本中暂未发现需要确认的管理信号。"} /></Card> :
      <div className="space-y-3">{data?.items.map((item, index) => <Card key={`${item.kind}-${index}`} className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-3"><div>
          <div className="flex flex-wrap items-center gap-2"><Lightbulb className="h-4 w-4 text-primary" /><h2 className="text-sm font-semibold">{item.title}</h2><Badge tone={item.risk_level === "高" ? "red" : item.risk_level === "中" ? "amber" : "blue"}>{item.risk_level}风险</Badge></div>
          <p className="mt-2 text-sm text-muted-foreground">{item.conclusion}</p>
          <p className="mt-2 text-xs text-muted-foreground">依据：{item.reason} · {item.evidence_count} 条证据{item.departments.length ? ` · ${item.departments.join("、")}` : ""}</p>
        </div></div>
        <div className="mt-3 space-y-2 border-t border-border pt-3"><p className="text-xs font-medium text-muted-foreground">周报依据</p>{item.sources.map((source) => <Link key={source.report_id} href={`/reports/${source.report_id}`} className="block rounded-control bg-muted/40 px-3.5 py-2.5 transition-colors hover:bg-primary-soft"><div className="flex justify-between gap-2 text-xs"><span className="font-medium">{source.employee_name ?? "未知员工"} · {source.title ?? "周报"}</span><span className="inline-flex items-center gap-1 text-primary">查看原文 <ExternalLink className="h-3 w-3" /></span></div><p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{source.source_text}</p></Link>)}</div>
      </Card>)}</div>}
  </div>;
}
