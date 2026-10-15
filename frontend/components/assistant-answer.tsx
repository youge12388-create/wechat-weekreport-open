"use client";

import React from "react";
import Link from "next/link";
import { cn } from "@/lib/cn";
import type { SearchSource } from "@/lib/types";

type ParsedBlock =
  | { type: "heading"; level: number; text: string }
  | { type: "paragraph"; text: string }
  | { type: "unordered-list"; items: string[] }
  | { type: "ordered-list"; items: string[] }
  | { type: "blockquote"; text: string }
  | { type: "code"; text: string }
  | { type: "rule" };

const BLOCK_START =
  /^(?:#{1,6}\s+|```|>\s?|[-*+]\s+|\d+[.)]\s+|-{3,}\s*$)/;

function isBlank(line: string): boolean {
  return line.trim().length === 0;
}

function parseBlocks(markdown: string): ParsedBlock[] {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ParsedBlock[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index] ?? "";
    if (isBlank(line)) {
      index += 1;
      continue;
    }

    if (line.trim().startsWith("```")) {
      index += 1;
      const code: string[] = [];
      while (index < lines.length && !(lines[index] ?? "").trim().startsWith("```")) {
        code.push(lines[index] ?? "");
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push({ type: "code", text: code.join("\n") });
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        text: heading[2].trim(),
      });
      index += 1;
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index] ?? "")) {
        items.push((lines[index] ?? "").replace(/^\s*[-*+]\s+/, "").trim());
        index += 1;
      }
      blocks.push({ type: "unordered-list", items });
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index] ?? "")) {
        items.push((lines[index] ?? "").replace(/^\s*\d+[.)]\s+/, "").trim());
        index += 1;
      }
      blocks.push({ type: "ordered-list", items });
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index] ?? "")) {
        quote.push((lines[index] ?? "").replace(/^\s*>\s?/, "").trim());
        index += 1;
      }
      blocks.push({ type: "blockquote", text: quote.join("\n") });
      continue;
    }

    if (/^\s*-{3,}\s*$/.test(line)) {
      blocks.push({ type: "rule" });
      index += 1;
      continue;
    }

    const paragraph: string[] = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      !isBlank(lines[index] ?? "") &&
      !BLOCK_START.test((lines[index] ?? "").trim())
    ) {
      paragraph.push((lines[index] ?? "").trim());
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraph.join(" ") });
  }

  return blocks;
}

function inlineNodes(
  text: string,
  sourceById: Map<number, SearchSource>,
  validCitationIdSet: Set<number>,
  /** 管理总览里引用是「出处标记」，用中性灰即可；问答里它是可点的来源，用主色。 */
  quietCitations = false
): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // 必须与后端 CITATION_RE 保持一致的容错：真实模型会写成 [#周报365]（井号后多两个字），
  // 只认 [#365] 会让后端提取到来源、正文里却留下不可点的死文本（违反 AC-17）。
  const pattern = /(\[#(?:周报|报告|周報)?\s*(\d+)\]|\*\*([^*]+)\*\*|`([^`]+)`)/g;
  let cursor = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }

    if (match[2]) {
      const reportId = Number(match[2]);
      const source = sourceById.get(reportId);
      const known = source || validCitationIdSet.has(reportId);
      // 保留 `#`：它是「这是引用」的标记，去掉后裸数字会被读成正文内容。
      const label = `#${reportId}`;
      if (known) {
        nodes.push(
          <Link
            key={`${reportId}-${match.index}`}
            href={`/reports/${reportId}`}
            title={[
              source?.title,
              source?.employee_name,
              source?.period_start && source.period_end
                ? `${source.period_start} 至 ${source.period_end}`
                : null,
              source?.reason,
            ]
              .filter(Boolean)
              .join(" · ") || `周报 #${reportId}`}
            className={cn(
              "mx-0.5 inline-flex translate-y-[-1px] items-center rounded-control px-1.5 py-0.5 text-xs font-medium leading-none tabular-nums transition-colors",
              quietCitations
                ? "bg-muted text-muted-foreground hover:bg-primary-soft hover:text-primary"
                : "bg-primary-soft text-primary hover:bg-primary/10"
            )}
          >
            {label}
          </Link>
        );
      } else {
        nodes.push(
          <span
            key={`citation-${reportId}-${match.index}`}
            title="该引用未列入本次来源明细"
            className="mx-0.5 inline-flex translate-y-[-1px] items-center rounded-control bg-muted px-1.5 py-0.5 text-xs font-medium leading-none text-muted-foreground"
          >
            {label}
          </span>
        );
      }
    } else if (match[3]) {
      nodes.push(
        <strong key={`bold-${match.index}`} className="font-semibold text-foreground">
          {match[3]}
        </strong>
      );
    } else if (match[4]) {
      nodes.push(
        <code
          key={`code-${match.index}`}
          className="rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]"
        >
          {match[4]}
        </code>
      );
    }

    cursor = pattern.lastIndex;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function headingClass(level: number): string {
  if (level <= 2) return "mt-5 text-base font-semibold leading-6 first:mt-0";
  if (level === 3) return "mt-4 text-sm font-semibold leading-6 first:mt-0";
  return "mt-3 text-sm font-semibold leading-6 first:mt-0";
}

/**
 * 一段含 `[#周报ID]` 引用的文本，引用渲染成可点来源。
 *
 * 从 ``AssistantAnswer`` 抽出来共用：经营看板的月度简报是结构化 JSON
 * （headline / highlights / 每部门一句话），不是整篇 Markdown，套不上块解析器，
 * 但引用格式必须与全站一致 —— 这里若再写一遍正则，迟早出现「后端放宽了容错、
 * 这边仍只认 [#365]」的一侧可点一侧死文本（第 21 条记过这条纪律）。
 */
export function InlineCitations({
  text,
  validCitationIds,
  className,
}: {
  text: string;
  validCitationIds?: number[];
  className?: string;
}) {
  const validCitationIdSet = React.useMemo(
    () => new Set(validCitationIds ?? []),
    [validCitationIds]
  );
  return (
    <span className={className}>
      {inlineNodes(text, new Map(), validCitationIdSet, false)}
    </span>
  );
}

export function AssistantAnswer({
  text,
  sources = [],
  validCitationIds = [],
  /** 引用渲染成安静的中性小标记（管理总览）；默认是可点的来源链接（问答）。 */
  quietCitations = false,
  className,
}: {
  text: string;
  sources?: SearchSource[];
  validCitationIds?: number[];
  quietCitations?: boolean;
  className?: string;
}) {
  const sourceById = React.useMemo(
    () => new Map(sources.map((source) => [source.report_id, source])),
    [sources]
  );
  const validCitationIdSet = React.useMemo(
    () => new Set(validCitationIds),
    [validCitationIds]
  );
  const blocks = React.useMemo(() => parseBlocks(text), [text]);

  return (
    <div className={cn("space-y-2.5 text-sm leading-7 text-foreground/90", className)}>
      {blocks.map((block, blockIndex) => {
        if (block.type === "heading") {
          const Heading = (`h${Math.min(Math.max(block.level, 2), 4)}` as
            | "h2"
            | "h3"
            | "h4");
          return (
            <Heading key={blockIndex} className={headingClass(block.level)}>
              {inlineNodes(block.text, sourceById, validCitationIdSet, quietCitations)}
            </Heading>
          );
        }

        if (block.type === "unordered-list") {
          return (
            <ul
              key={blockIndex}
              className="ml-1 list-disc space-y-1.5 pl-5 marker:text-muted-foreground/60"
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex} className="pl-0.5">
                  {inlineNodes(item, sourceById, validCitationIdSet, quietCitations)}
                </li>
              ))}
            </ul>
          );
        }

        if (block.type === "ordered-list") {
          return (
            <ol
              key={blockIndex}
              className="ml-1 list-decimal space-y-1.5 pl-5 marker:font-medium marker:text-muted-foreground"
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex} className="pl-0.5">
                  {inlineNodes(item, sourceById, validCitationIdSet, quietCitations)}
                </li>
              ))}
            </ol>
          );
        }

        if (block.type === "blockquote") {
          return (
            <blockquote
              key={blockIndex}
              className="border-l-2 border-primary/30 bg-muted/40 px-3 py-2 text-muted-foreground"
            >
              {inlineNodes(block.text, sourceById, validCitationIdSet, quietCitations)}
            </blockquote>
          );
        }

        if (block.type === "code") {
          return (
            <pre
              key={blockIndex}
              className="overflow-x-auto rounded-control bg-ink px-3 py-2.5 font-mono text-xs leading-6 text-white"
            >
              <code>{block.text}</code>
            </pre>
          );
        }

        if (block.type === "rule") {
          return <hr key={blockIndex} className="border-border" />;
        }

        return (
          <p key={blockIndex} className="break-words">
            {inlineNodes(block.text, sourceById, validCitationIdSet, quietCitations)}
          </p>
        );
      })}
    </div>
  );
}
