import Link from "next/link";

import { InlineCitations } from "@/components/assistant-answer";
import type {
  OperatingAnomaly,
  OperatingDashboardPayload,
  OperatingDepartmentBrief,
  OperatingIntervention,
  OperatingSharedIssue,
  OperatingStuckItem,
} from "@/lib/types";

type Range = { start: string; end: string };
type MonthlyPayload = OperatingDashboardPayload;

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-card border border-border bg-surface px-5 py-4 shadow-card">
      <h2 className="text-base font-semibold text-foreground">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  );
}

function EvidenceLink({ ids, range, size = "xs" }: { ids: number[]; range: Range; size?: "xs" | "sm" }) {
  if (!ids.length) return null;
  return (
    <Link
      className={`shrink-0 text-[13px] ${size === "sm" ? "font-medium" : ""} text-primary hover:underline`}
      href={"/reports/" + ids[0] + "?start=" + range.start + "&end=" + range.end}
    >
      查看来源
    </Link>
  );
}

function isSupported(text: string | undefined, ids: number[]) {
  return Boolean(text?.trim() && ids.length);
}

export function MonthlyMainlines({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  const names = payload.narrative?.mainline_names ?? [];
  const items = names
    .map((name) => payload.mainlines?.items.find((item) => item.name === name))
    .filter((item) => item && isSupported(item.ai_text, item.source_report_ids))
    .slice(0, 6);
  if (!items.length || !payload.narrative?.mainline_summary?.trim()) return null;
  return (
    <Section title="公司现在主要推进什么">
      <p className="text-[15px] leading-7 text-foreground/85">
        <InlineCitations text={payload.narrative.mainline_summary} />
      </p>
      <ol className="mt-3 space-y-1 border-t border-border/70 pt-3">
        {items.map((item, index) => item && (
          <li key={item.name} className="flex items-start gap-3 py-1.5 text-sm leading-7">
            <span className="w-5 shrink-0 font-semibold text-primary">{index + 1}.</span>
            <div className="min-w-0 flex-1">
              <span className="font-semibold text-foreground">{item.name}：</span>
              <InlineCitations text={item.ai_text ?? ""} />
            </div>
            <EvidenceLink ids={item.source_report_ids} range={range} />
          </li>
        ))}
      </ol>
    </Section>
  );
}

export function MonthlyChanges({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  const block = payload.changes;
  const items = (block?.items ?? [])
    .filter((item) => isSupported(item.ai_text, item.source_report_ids))
    .filter((item) => !item.needs_baseline || block?.baseline.comparable)
    .slice(0, 3);
  if (!items.length) return null;
  const summary = payload.narrative?.change_summary?.trim();
  const sourceIds = Array.from(new Set(items.flatMap((item) => item.source_report_ids)));
  return (
    <Section title="最近一个月发生了什么变化">
      {summary ? (
        <div className="mb-3 flex items-start gap-3 border-b border-border/70 pb-3 text-sm leading-7">
          <p className="min-w-0 flex-1 font-medium text-foreground/90">
            <InlineCitations text={summary} />
          </p>
          <EvidenceLink ids={sourceIds} range={range} />
        </div>
      ) : null}
      <ul className="space-y-3">
        {items.map((item) => (
          <li key={item.kind + item.title} className="flex items-start gap-3 text-sm leading-7">
            <div className="min-w-0 flex-1">
              <span className="font-semibold text-foreground">{item.title}：</span>
              <InlineCitations text={item.ai_text ?? ""} />
            </div>
            <EvidenceLink ids={item.source_report_ids} range={range} />
          </li>
        ))}
      </ul>
    </Section>
  );
}

function departmentSources(brief: OperatingDepartmentBrief) {
  return Array.from(new Set([
    ...brief.mainlines.flatMap((item) => item.source_report_ids),
    ...brief.blockers.flatMap((item) => item.source_report_ids),
    ...brief.waiting.flatMap((item) => item.source_report_ids),
    ...brief.changes.flatMap((item) => item.source_report_ids),
  ]));
}

export function MonthlyDepartments({ payload, range, onGenerate, generating, error }: {
  payload: MonthlyPayload; range: Range; onGenerate: () => void; generating: boolean; error: string | null;
}) {
  const briefs = payload.department_briefs ?? [];
  if (!briefs.length) return null;
  const state = payload.summary.department_cards;
  const busy = generating || state?.status === "pending" || state?.status === "running";
  return (
    <Section title="按部门看">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-[13px] text-muted-foreground">
        <span>{state?.items.filter((item) => item.status === "current").length ?? 0}/{briefs.length} 个部门已可查看</span>
        {(state?.ready_to_generate ?? 0) > 0 && (
          <button type="button" className="rounded-control border border-border px-3 py-1.5 font-medium text-foreground disabled:opacity-50"
            onClick={onGenerate} disabled={busy}>
            {busy ? "部门简报生成中…" : `生成已就绪部门（${state?.ready_to_generate}）`}
          </button>
        )}
      </div>
      {error && <p className="mb-3 text-[13px] text-destructive">提交部门简报失败：{error}</p>}
      {state?.last_error && (state.status === "failed" || state.ready_to_generate > 0) &&
        <p className="mb-3 text-[13px] text-destructive">部门简报待处理：{state.last_error}</p>}
      <ul className="grid grid-cols-1 gap-3 lg:grid-cols-2 2xl:grid-cols-3">
        {briefs.map((brief) => {
          const hasBlocker = Boolean(brief.ai_card?.blocker.trim()) || brief.open_blockers > 0;
          return (
            <li key={brief.department_id ?? brief.name} className="flex min-w-0 flex-col rounded-[18px] border border-border bg-surface p-5 text-sm leading-7">
              <div className="flex items-center justify-between gap-3">
                <h3 className="min-w-0 text-base font-semibold text-foreground">{brief.name}</h3>
                {brief.ai_card && (
                  <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ring-inset ${
                    hasBlocker ? "bg-warning-soft text-warning ring-warning/20" : "bg-success-soft text-success ring-success/20"
                  }`}>
                    <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${hasBlocker ? "bg-warning" : "bg-success"}`} />
                    {hasBlocker ? "有阻碍" : "有进展"}
                  </span>
                )}
              </div>
              {brief.ai_status?.status !== "current" ? (
                <div className="mt-3 text-sm leading-7 text-muted-foreground">
                  <p>{brief.ai_status?.status === "waiting_analysis"
                    ? `本部门已分析 ${brief.ai_status.analysis_completed}/${brief.ai_status.analysis_total} 篇，等待其余周报分析完成。`
                    : brief.ai_status?.status === "failed"
                      ? "本部门卡片生成失败，可重试。"
                      : brief.ai_status?.analysis_total === 0
                        ? "本期暂无可分析周报。"
                        : busy ? "正在生成本部门结论…" : "本部门结论待生成。"}</p>
                  {!!brief.ai_status?.analysis_failed && <p className="mt-1 text-destructive">{brief.ai_status.analysis_failed} 篇分析失败</p>}
                </div>
              ) : brief.ai_card ? (
                <>
                  <p className="mt-3 text-[17px] font-semibold leading-7 tracking-[-0.01em] text-foreground">{brief.ai_card.headline}</p>
                  <div className="mt-3 space-y-2.5 text-sm leading-7">
                    {brief.ai_card.progress && <p className="grid grid-cols-[2.75rem_minmax(0,1fr)] gap-2.5 text-foreground/85"><span className="font-semibold text-primary">进展</span><InlineCitations text={brief.ai_card.progress} validCitationIds={brief.ai_card.source_report_ids} /></p>}
                    {brief.ai_card.blocker && <p className="grid grid-cols-[2.75rem_minmax(0,1fr)] gap-2.5 text-foreground/85"><span className="font-semibold text-warning">卡点</span><InlineCitations text={brief.ai_card.blocker} validCitationIds={brief.ai_card.source_report_ids} /></p>}
                  </div>
                  {brief.ai_card.suggested_action && <p className="mt-3 grid grid-cols-[2.75rem_minmax(0,1fr)] gap-2.5 rounded-xl bg-muted/70 px-3.5 py-2.5 text-sm leading-7 text-foreground"><span className="font-semibold text-primary">建议</span><InlineCitations text={brief.ai_card.suggested_action} validCitationIds={brief.ai_card.source_report_ids} /></p>}
                  <div className="mt-auto pt-4"><EvidenceLink ids={brief.ai_card.source_report_ids} range={range} size="sm" /></div>
                </>
              ) : isSupported(brief.ai_summary, departmentSources(brief)) ? (
                <p className="mt-3 text-sm leading-7 text-foreground/85"><InlineCitations text={brief.ai_summary ?? ""} /></p>
              ) : <p className="mt-2 text-muted-foreground">本部门暂无有来源支持的结论。</p>}
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

function interventions(payload: MonthlyPayload) {
  return (payload.interventions?.items ?? [])
    .filter((item) => isSupported(item.ai_text, item.source_report_ids) && item.must_decide?.trim())
    .slice(0, 3);
}

export function MonthlyInterventions({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  const items = interventions(payload);
  return (
    <Section title="需要管理层介入">
      {!items.length && (
        <p className="text-sm leading-7 text-muted-foreground">
          {companyConclusionCurrent(payload)
            ? "本期暂无已明确的管理层决策；其他待核查线索见下方。"
            : "月度结论尚未就绪，暂不能判断需要拍板的事。"}
        </p>
      )}
      <ol className="space-y-3">
        {items.map((item, index) => (
          <li key={item.title} className="flex items-start gap-3 text-sm leading-7">
            <span className="w-5 shrink-0 font-semibold text-destructive">{index + 1}.</span>
            <div className="min-w-0 flex-1">
              <span className="font-semibold text-foreground">{item.title}：</span>
              <InlineCitations text={item.ai_text ?? ""} validCitationIds={item.source_report_ids} />
              <p className="text-destructive">需决定：{item.must_decide}</p>
            </div>
            <EvidenceLink ids={item.source_report_ids} range={range} />
          </li>
        ))}
      </ol>
    </Section>
  );
}

type Issue = OperatingStuckItem | OperatingAnomaly | OperatingSharedIssue | OperatingIntervention;

function companyConclusionCurrent(payload: MonthlyPayload) {
  return payload.summary.analysis_ready === true && payload.summary.status === "success" &&
    !payload.summary.stale && Boolean(payload.narrative);
}

function issueNames(item: Issue, payload: MonthlyPayload) {
  const names = new Set([
    item.title,
    ...("aliases" in item ? item.aliases : []),
    ...("corroboration" in item ? item.corroboration.map((entry) => entry.label) : []),
  ]);
  // 后端已给出同一问题的不同叫法。共有来源周报不能证明两件事相同。
  for (const shared of payload.shared_issues ?? []) {
    if ([shared.title, ...shared.aliases].some((name) => names.has(name))) {
      names.add(shared.title);
      shared.aliases.forEach((name) => names.add(name));
    }
  }
  return names;
}

function sameIssue(item: Issue, other: Issue, payload: MonthlyPayload) {
  if ("blocker_id" in item && "blocker_id" in other &&
    item.blocker_id != null && item.blocker_id === other.blocker_id) return true;
  const names = issueNames(item, payload);
  return Array.from(issueNames(other, payload)).some((name) => names.has(name));
}

function remainingIssues<T extends Issue>(items: T[], shown: Issue[], payload: MonthlyPayload): T[] {
  const accepted: T[] = [];
  // 已写成结论的内容优先；没有月度文案的事实仍可作为待核查线索查看。
  const ordered = [...items].sort((a, b) => Number(Boolean(b.ai_text?.trim())) - Number(Boolean(a.ai_text?.trim())));
  for (const item of ordered) {
    if (!item.source_report_ids.length) continue;
    if ([...shown, ...accepted].some((other) => sameIssue(item, other, payload))) continue;
    accepted.push(item);
  }
  return accepted;
}

function sharedIssues(payload: MonthlyPayload) {
  return remainingIssues(payload.shared_issues ?? [], interventions(payload), payload);
}

function stuckItems(payload: MonthlyPayload) {
  return remainingIssues(payload.stuck?.items ?? [], [...interventions(payload), ...sharedIssues(payload)], payload);
}

function issueEmptyMessage(payload: MonthlyPayload, candidates: Issue[], category: string) {
  if (candidates.some((item) => item.source_report_ids.length)) {
    return "相关问题已列入上方分类，此处不重复展开。";
  }
  return companyConclusionCurrent(payload)
    ? `本期未识别到有来源支持的${category}线索。`
    : "本期材料或月度结论尚未就绪，暂不能判断这一类问题。";
}

function MonthlyIssueList({ title, items, range, limit, emptyMessage }: {
  title: string; items: Issue[]; range: Range; limit: number; emptyMessage: string;
}) {
  function rows(entries: Issue[]) {
    return entries.map((item) => {
      const aiText = item.ai_text?.trim();
      // 卡点 detail 是已有的原文片段。其余候选只展示标题与来源，避免拼装经营判断。
      const excerpt = !aiText && "kind" in item && item.kind === "blocker" && "detail" in item
        ? item.detail.trim() : "";
      return (
        <li key={item.title} className="flex flex-wrap items-start gap-x-3 gap-y-1 text-sm leading-7">
          <div className="min-w-0 flex-1 basis-48">
            <span className="font-semibold text-foreground">{item.title}{aiText ? "：" : ""}</span>
            {aiText ? <InlineCitations text={aiText} validCitationIds={item.source_report_ids} /> : (
              <>
                <span className="ml-2 whitespace-nowrap text-xs text-muted-foreground">待核查</span>
                {excerpt && <p className="mt-1 text-foreground/75">周报记录：{excerpt}</p>}
              </>
            )}
          </div>
          <EvidenceLink ids={item.source_report_ids} range={range} />
        </li>
      );
    });
  }
  return (
    <Section title={title}>
      {items.some((item) => !item.ai_text?.trim()) && (
        <p className="mb-3 text-xs leading-6 text-muted-foreground">
          标为“待核查”的线索有周报来源，尚未形成独立月度结论，请结合原文核查。
        </p>
      )}
      {!items.length && <p className="text-sm leading-7 text-muted-foreground">{emptyMessage}</p>}
      <ul className="space-y-3">
        {rows(items.slice(0, limit))}
      </ul>
      {items.length > limit && (
        <details className="mt-3 border-t border-border/70 pt-3">
          <summary className="cursor-pointer text-[13px] font-medium text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            查看其余 {items.length - limit} 条
          </summary>
          <ul className="mt-3 space-y-3">{rows(items.slice(limit))}</ul>
        </details>
      )}
    </Section>
  );
}

export function MonthlySharedIssues({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  return <MonthlyIssueList title="公司级共性问题" items={sharedIssues(payload)} range={range} limit={3}
    emptyMessage={issueEmptyMessage(payload, payload.shared_issues ?? [], "公司级共性问题")} />;
}

export function MonthlyStuck({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  return <MonthlyIssueList title="哪些事情卡住了" items={stuckItems(payload)} range={range} limit={4}
    emptyMessage={issueEmptyMessage(payload, payload.stuck?.items ?? [], "卡点")} />;
}

export function MonthlyAnomalies({ payload, range }: { payload: MonthlyPayload; range: Range }) {
  const items = remainingIssues(payload.anomalies?.items ?? [],
    [...interventions(payload), ...sharedIssues(payload), ...stuckItems(payload)], payload);
  return <MonthlyIssueList title="有哪些需要核查的异常" items={items} range={range} limit={3}
    emptyMessage={issueEmptyMessage(payload, payload.anomalies?.items ?? [], "异常")} />;
}
