"use client";

import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Card, ErrorState, LoadingBlock } from "@/components/ui";
import { formatDateTime } from "@/lib/format";
import type { SyncStatus, SyncTypeStatus } from "@/lib/types";
import { SyncErrorNotice } from "@/components/sync-error-notice";

/** 同步总览：进入「同步与抓取」先看到「现在有没有问题」，再往下才是配置项。 */
export function SyncOverviewCard({
  data,
  loading,
  error,
  onRetry,
}: {
  data: SyncStatus | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  if (loading && !data) {
    return (
      <Card>
        <LoadingBlock rows={2} />
      </Card>
    );
  }
  if (error && !data) {
    return (
      <Card>
        <div className="p-5">
          <ErrorState message={error} onRetry={onRetry} />
        </div>
      </Card>
    );
  }
  if (!data) return null;

  const detail = data.last_error_detail;
  const healthy = !detail;
  const types: { key: string; label: string; status: SyncTypeStatus }[] = [
    { key: "reports", label: "周报", status: data.reports },
    { key: "departments", label: "部门", status: data.departments },
    { key: "employees", label: "员工", status: data.employees },
  ];
  const busy = data.pending_count > 0 || data.running_count > 0;

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
        <div className="flex min-w-0 items-center gap-2.5">
          {healthy ? (
            <CheckCircle2 className="h-5 w-5 shrink-0 text-success" />
          ) : (
            <AlertTriangle className="h-5 w-5 shrink-0 text-destructive" />
          )}
          <div className="min-w-0">
            <p className="text-sm font-semibold">
              {healthy ? "同步运行正常" : "同步存在未恢复的失败"}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {busy
                ? `队列 ${data.pending_count} 待执行 / ${data.running_count} 执行中`
                : `上次成功 ${formatDateTime(data.last_success_at)}`}
              {data.last_error_resolved && " · 历史失败已恢复"}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {types.map((item) => (
            <span
              key={item.key}
              className="inline-flex items-center gap-1.5 rounded-pill bg-muted/60 px-2.5 py-1 text-xs text-muted-foreground"
              title={`上次成功：${formatDateTime(item.status.last_success_at)}`}
            >
              <span
                className={
                  item.status.last_error_detail
                    ? "h-1.5 w-1.5 rounded-full bg-destructive"
                    : item.status.active
                      ? "h-1.5 w-1.5 rounded-full bg-warning"
                      : "h-1.5 w-1.5 rounded-full bg-success"
                }
              />
              {item.label}
              {item.status.active && "同步中"}
            </span>
          ))}
        </div>
      </div>
      <SyncErrorNotice variant="block" detail={detail} />
    </Card>
  );
}
