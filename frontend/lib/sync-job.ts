import type { SyncJobItem } from "@/lib/types";

export function describeReportJob(job: SyncJobItem): string {
  if (job.status === "pending") return "周报抓取正在排队。";
  if (job.status === "running") return "周报抓取仍在后台执行。";

  // 旧任务未存新增细分；但成功处理为 0 时，可以确定新增也为 0。
  const createdCount = job.created_count ?? (job.success_count === 0 ? 0 : null);
  const createdWeeklyCount = job.created_weekly_count ?? (job.success_count === 0 ? 0 : null);
  const result =
    createdCount === null || createdWeeklyCount === null
      ? `成功处理 ${job.success_count} 篇（旧任务未记录其中新增周报数）`
      : [
          `新增周报 ${createdWeeklyCount} 篇`,
          ...(createdCount > createdWeeklyCount
            ? [`新增其他汇报 ${createdCount - createdWeeklyCount} 篇`]
            : []),
          `重新处理 ${Math.max(0, job.success_count - createdCount)} 篇`,
        ].join("、");
  const status =
    job.status === "success" ? "已完成" : job.status === "partial" ? "部分完成" : "失败";
  const counts = `${result}，跳过 ${job.skipped_count} 篇，失败 ${job.failed_count} 篇`;
  const error = job.error_message ? `；原因：${job.error_message}` : "";
  const empty = job.status === "success" &&
    job.success_count + job.skipped_count + job.failed_count === 0
    ? "；本次未获取到符合条件的记录，请核对企微数据与模板过滤"
    : "";
  return `${status}：${counts}${error}${empty}。`;
}
