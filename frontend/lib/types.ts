/** 后端 API 返回结构定义（与 backend/services/view_service 序列化结果对齐）。 */

export interface Period {
  start: string;
  end: string;
}

/** 登录授权三态：null=未设置（继承上级）/ true=允许 / false=显式禁止。 */
export type LoginSwitch = boolean | null;
export type LoginRole = "member" | "admin" | "dept_admin";

export interface DepartmentItem {
  id: number;
  external_id: string | null;
  name: string;
  parent_id: number | null;
  status: string;
  source: string;
  /** 登录授权开关（部门树按子树生效） */
  login_enabled?: LoginSwitch;
  login_role?: LoginRole;
  employee_count: number;
  submitted: number;
  unsubmitted: number;
  reports_count?: number;
  work_item_total: number;
  work_item_done: number;
  work_item_in_progress: number;
  /** 已完成统一分析周报的内容完整度参考分；没有样本时为 null。 */
  average_score?: number | null;
  blocker_total?: number;
  department_status?: "正常" | "关注" | "注意";
  analysis_coverage?: AnalysisCoverage;
  /** 免提交周报的部门：不进统计基数，前端要标注而不是记成「全员未交」 */
  exempt?: boolean;
}

export interface RemindTarget {
  id: number;
  name: string;
  userid: string | null;
  department_name: string | null;
  /** 该员工该周期最近一次成功催交时间；null = 从未催过 */
  last_reminded_at: string | null;
}

export interface RemindPreview {
  dry_run: boolean;
  period: Period;
  target_count: number;
  targets: RemindTarget[];
  skipped: Array<{ id: number; name: string; reason: string }>;
  card: { title: string; description: string; url: string; btn_text: string };
  sent: boolean;
  error: string | null;
  /**
   * 本次投递的结论，前端据此选提示语气，不要靠 sent + error 反推：
   *   no_targets = 没有可发对象（全已提交 / 都在冷却期 / 全部免提交）
   *   failed     = 请求没被企微受理（网络 / 鉴权 / 权限），重试有意义
   *   sent       = 请求被受理；是否人人收到看 result.delivered_count
   */
  outcome?: "no_targets" | "failed" | "sent";
  result?: {
    errcode: number | null;
    errmsg: string | null;
    invaliduser: string;
    /** 真实送达人数；errcode=0 只代表请求被接受，不等于所有人都收到 */
    delivered_count?: number;
    undelivered?: Array<{ id: number; name: string; reason: string }>;
  };
}

export interface ReminderSettings {
  auto_enabled: boolean;
  weekday: number;
  weekday_label: string;
  hour: number;
  /** 周期归属天数：周期开始后前 N 天内提交仍归上一周期（0=不回溯，最大 7） */
  attribution_days: number;
  /** 该取值的人话说明，由后端生成，前端不重算规则 */
  attribution_label: string;
  period_rule: "week" | "month" | "custom_week";
  /** 催交卡片文案（运行期配置，改完下一次发送即生效） */
  card_title: string;
  card_body_zh: string;
  card_body_en: string;
  card_url: string;
  /**
   * 跟进沟通中「负责人回复了」要通知到的企微 userid（逗号分隔）。
   * 参与过会话的企微管理员会被自动纳入；这一项是补充，主要给
   * 用访问密码登录、没有员工身份的管理员指定接收账号。
   */
  action_notify_admin_userids: string;
  next_run_at: string | null;
  current_period: Period;
  current_target_count: number;
  last_run_at: string | null;
  last_status: "running" | "success" | "failed" | "skipped" | "partial" | null;
  last_error: string | null;
  last_target_count: number | null;
}

export interface EmployeeItem {
  id: number;
  external_id: string | null;
  name: string;
  department_id: number | null;
  department_name: string | null;
  position: string | null;
  status: string;
  status_label: string;
  source: string;
  last_seen_at: string | null;
  /** true=个人免提交周报，不进统计基数与催交名单。 */
  report_exempt: boolean;
  /** 个人级登录授权开关；用于跨部门领导「追加」权限（部门授权不受影响）。 */
  login_enabled?: boolean;
  login_role?: LoginRole;
  last_report_at?: string | null;
  reports_total?: number;
  has_submitted?: boolean;
}

/** GET /api/auth/me —— 当前会话。 */
export interface SessionInfo {
  authenticated: boolean;
  name?: string;
  role?: LoginRole;
  is_admin?: boolean;
  source?: "password" | "wecom";
  employee_id?: number | null;
  /** 会话失效时的原因（如授权被撤销、员工离职）。 */
  reason?: string;
  message?: string;
}

/** GET /api/config —— 登录页也要读，含可用的登录方式。 */
export interface PublicConfig {
  period_rule: string;
  period_rule_label: string;
  timezone_offset_hours: number;
  due_weekday: number;
  due_hour: number;
  wecom_login_enabled: boolean;
  password_login_enabled: boolean;
}

/** 部门授权树的单个部门节点（含继承后的有效结论）。 */
export interface AccessDepartmentNode {
  id: number;
  name: string;
  parent_id: number | null;
  external_id: string | null;
  status: string;
  employee_count: number;
  subtree_employee_count: number;
  /** 本级显式设置：null=继承上级 */
  setting: LoginSwitch;
  role: LoginRole;
  /** 逐级继承后的最终结论；null 表示链上没有任何显式设置（拒绝）。 */
  effective: {
    allowed: boolean;
    role: LoginRole | null;
    source: "department";
    source_id: number;
    source_name: string;
  } | null;
}

export interface AccessEmployeeNode {
  id: number;
  name: string;
  external_id: string | null;
  position: string | null;
  department_id: number | null;
  setting: boolean;
  denied?: boolean;
  role: LoginRole;
}

export interface AccessTree {
  departments: AccessDepartmentNode[];
  employees: AccessEmployeeNode[];
  authorized_employee_ids: number[];
  admin_employee_ids: number[];
  enabled: boolean;
}

export interface AccessPrerequisite {
  key: string;
  label: string;
  ok: boolean;
  hint: string;
}

export interface AccessOverview {
  enabled: boolean;
  corp_configured: boolean;
  agent_configured: boolean;
  agent_id: string | null;
  redirect_uri: string | null;
  frontend_url: string | null;
  denied_mode: "show" | "redirect";
  authorized_count: number;
  admin_count: number;
  active_employee_total: number;
  department_total: number;
  department_configured_total: number;
  prerequisites: AccessPrerequisite[];
  callback_path: string;
  /** 实际会下发给企微的地址（输入归一化后 / 未配置则按访问域名推导）。 */
  effective_callback_url: string;
}

export interface ReportItem {
  id: number;
  employee_id: number | null;
  employee_name: string | null;
  department_name: string | null;
  external_id: string | null;
  report_type: string;
  title: string | null;
  /** 本地按正文内容推导的语言分类，供“仅英文周报”筛选。 */
  content_language: "english" | "chinese" | "other" | "unknown";
  period_start: string | null;
  period_end: string | null;
  submitted_at: string | null;
  template_name: string | null;
  source: string;
  raw_record_id: number | null;
  work_item_count: number;
  ai_status: "not_processed" | "processed" | "failed";
  /** 最近一次大模型重复检测结论；null = 还没检测过 */
  duplicate: DuplicateInfo | null;
  quality?: QualityInfo | null;
}

export interface DuplicateInfo {
  is_duplicate: boolean;
  score: number;
  reason: string | null;
  checked_at: string | null;
}

export interface QualityInfo {
  is_thin: boolean;
  score: number;
  reason: string | null;
  checked_at: string | null;
}

export interface ReportListResponse {
  items: ReportItem[];
  total: number;
  page: number;
  page_size: number;
  period: Period;
}

export interface SyncJobItem {
  id: number;
  source: string;
  type: string;
  status: "pending" | "running" | "success" | "partial" | "failed";
  start_time: string | null;
  end_time: string | null;
  success_count: number;
  /** 旧任务没有单独统计新增数，因此可能为 null。 */
  created_count: number | null;
  created_weekly_count: number | null;
  failed_count: number;
  skipped_count: number;
  triggered_by: string;
  error: Array<{ external_id?: string | null; code?: string; message?: string }> | null;
  error_message: string | null;
  window: string | null;
}

/** 错误分类结果：不依赖具体 SyncJob，探针和同步任务共用同一套整理逻辑。 */
export interface SyncErrorClassification {
  code: string | null;
  vendor_code: string | null;
  kind:
    | "permission"
    | "auth"
    | "rate_limit"
    | "upstream"
    | "response"
    | "unknown";
  title: string;
  summary: string;
  hint: string | null;
  raw: string;
  egress_ip: string | null;
  step?: string | null;
}

/** 挂到某条同步任务上的错误详情，比分类结果多了「谁、什么时候」失败。 */
export interface SyncErrorDetail extends SyncErrorClassification {
  type: string;
  job_id: number;
  at: string | null;
}

export interface SyncTypeStatus {
  type: string;
  active: boolean;
  active_status: "pending" | "running" | null;
  last_success_at: string | null;
  last_success_window: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  last_error_detail: SyncErrorDetail | null;
  last_error_resolved: boolean;
  next_run_at: string | null;
}

export interface WeComProbeResult {
  ok: boolean;
  steps: Array<{
    step: string;
    ok: boolean;
    detail?: string;
    error?: string;
    error_type?: string;
    elapsed_ms?: number;
  }>;
  /** 企微看到的服务器公网出口 IP，用于核对可信 IP 是否填对。 */
  egress_ip: string | null;
  /** 探针不写库，因此没有 job_id / 时间，只有分类结果。 */
  error: SyncErrorClassification | null;
  checked_at: string;
}

export interface ReportScheduleSettings {
  auto_enabled: boolean;
  auto_period_summary_enabled: boolean;
  ai_auto_enqueue_on_sync: boolean;
  weekday: number;
  weekday_label: string;
  hour: number;
  minute: number;
  next_run_at: string | null;
}

export interface SyncStatus {
  auto_enabled: boolean;
  org_interval_hours: number;
  report_schedule: ReportScheduleSettings;
  pending_count: number;
  running_count: number;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  last_error_detail: SyncErrorDetail | null;
  last_error_resolved: boolean;
  departments: SyncTypeStatus;
  employees: SyncTypeStatus;
  reports: SyncTypeStatus;
  jobs: SyncJobItem[];
}

export interface ReportDetail extends ReportItem {
  employee: {
    name: string;
    external_id: string | null;
    position: string | null;
    department_name: string | null;
  } | null;
  content: string;
  content_raw: string;
  translation: ReportTranslationState;
  ai_analyses: Array<{
    analysis_type: string;
    status: string;
    model_provider: string | null;
    model_name: string | null;
    prompt_version: string | null;
    created_at: string | null;
  }>;
  analysis: ReportAnalysis | null;
  analysis_queue: {
    status: "pending" | "running" | "success" | "failed";
    attempts: number;
    next_attempt_at: string | null;
    last_error: string | null;
  } | null;
}

export interface ReportTranslationState {
  available: boolean;
  stale: boolean;
  status: "not_requested" | "running" | "success" | "failed" | "stale" | "empty";
  content: string | null;
  source_language?: string | null;
  target_language?: string | null;
  provider?: string | null;
  updated_at?: string | null;
  error?: string | null;
  cached?: boolean;
}

export interface ReportAnalysisItem {
  title: string;
  description: string | null;
  status: string | null;
  related_project: string | null;
  task_key?: string | null;
  mentioned_people: string[];
  confidence: number | null;
  source_text: string | null;
}

export interface ReportAnalysisRisk {
  title: string;
  description: string | null;
  level: "低" | "中" | "高";
  source_text: string | null;
}

export interface ReportAnalysisResult {
  summary: string;
  score: number;
  is_thin: boolean;
  thin_reason: string;
  is_duplicate: boolean;
  duplicate_score: number;
  duplicate_reason: string;
  score_breakdown: {
    completeness: number | null;
    specificity: number | null;
    results: number | null;
    advancement: number | null;
    planning: number | null;
    problem_quality: number | null;
    duplicate: number | null;
    reason: string;
  };
  completed_items: ReportAnalysisItem[];
  ongoing_items: ReportAnalysisItem[];
  results: ReportAnalysisItem[];
  blockers: ReportAnalysisItem[];
  problems: ReportAnalysisItem[];
  dependencies: ReportAnalysisItem[];
  next_week_plans: ReportAnalysisItem[];
  risks: ReportAnalysisRisk[];
  cached?: boolean;
  raw?: string;
}

export interface ReportAnalysis {
  id: number;
  analysis_type: string;
  status: string;
  model_name: string | null;
  model_provider: string | null;
  prompt_version: string | null;
  duration_ms: number | null;
  created_at: string | null;
  result: ReportAnalysisResult;
}

export interface DashboardOverview {
  period: Period;
  employee_total: number;
  submitted: number;
  unsubmitted: number;
  submit_rate: number;
  report_total: number;
  /** 已完成统一分析周报的内容完整度参考分；没有样本时为 null。 */
  average_score: number | null;
  work_item_total: number;
  work_item_done: number;
  work_item_in_progress: number;
  work_item_pending: number;
  blocker_total: number;
  new_blockers: number;
  long_blockers: number;
  high_risk_blockers: number;
  analysis_coverage: AnalysisCoverage;
  top_items: ManagementItem[];
  sections: Record<"work_summary" | "focus_work" | "needs" | "next_plans", WeeklySection>;
  current_blockers: DashboardCurrentBlocker[];
  /** 后端已按来源、周期和风险排序的管理行动候选。 */
  action_items?: DashboardActionItem[];
  trend: TrendPoint[];
  insights: InsightItem[];
  /** 已分析周报中的异常信号，供首页本周重点使用。 */
  abnormal_reports: DashboardAbnormalReport[];
  submission_trend: SubmissionTrendPoint[];
  /** 当前周期与等长上一周期的确定性对照，用于首页管理简报。 */
  period_comparison?: DashboardPeriodComparison;
  /** 最近一次成功同步（周报）时间，用于前端提示数据新鲜度；从未同步为 null */
  last_sync_at: string | null;
  ai_period_summary: DashboardPeriodSummary;
}

/** 周期卡点 / 资源需求的 AI 总结状态（GET/POST /api/blockers/period-summary）。 */
export interface BlockerPeriodSummary {
  period: Period;
  blockers_total: number;
  processed_blockers: number;
  status: "not_generated" | "pending" | "running" | "success" | "failed" | "stale" | "empty";
  stale: boolean;
  summary: string | null;
  created_at: string | null;
  model: string | null;
  last_error: string | null;
  /** 结论里引用过的周报 ID，用于把 [#编号] 渲染成可点来源。 */
  source_report_ids: number[];
  /**
   * 结论实际覆盖了多少条卡点。小于 `blockers_total` 说明本期卡点太多、
   * 只把优先级最高的一部分交给了模型 —— 必须显式提示，不能让局部结论
   * 被当成全量结论（与周期总览的 `full_context` 同一纪律）。
   */
  covered_blockers: number | null;
  /** 是否发生了覆盖截断（对应 covered_blockers < blockers_total）。 */
  truncated: boolean;
  /** 交给模型的正文实际字符数，用于运维核对预算是否被撞到。 */
  context_chars: number | null;
}

export interface DashboardPeriodSummary {
  period: Period;
  reports_total: number;
  processed_reports: number;
  status: "not_generated" | "pending" | "running" | "success" | "failed" | "stale" | "empty";
  stale: boolean;
  summary: string | null;
  created_at: string | null;
  model: string | null;
  last_error: string | null;
  /** 生成口径：true=一次读完全部原文；false=范围过大走分批；null=还没有结论。 */
  full_context: boolean | null;
  /** 送进模型的正文总字符数（一次读全时有值）。 */
  context_chars: number | null;
  /** 结论里引用过的周报 ID，用于把 [#编号] 渲染成可点来源。 */
  source_report_ids: number[];
}

/**
 * 首页轮询用的轻量状态（`GET /api/dashboard/status`）。
 *
 * 首页在「本期还在等 AI 分析」或「周期总结生成中」时每 4 秒刷新一次；它只需要
 * 这两个会变的字段，不该为此重拉整个 DashboardOverview（那会连趋势、异常、
 * 洞察一起重算）。字段口径与 overview 里的同名项一致。
 */
export interface DashboardStatus {
  period: Period;
  analysis_coverage: AnalysisCoverage;
  ai_period_summary: DashboardPeriodSummary;
}

export interface DashboardAbnormalReport {
  id: number;
  employee_id: number | null;
  employee_name: string | null;
  department_name: string | null;
  title: string | null;
  submitted_at: string | null;
  reasons: string[];
  href: string;
}

export interface SubmissionTrendPoint {
  period_start: string;
  period_end: string;
  submitted: number;
  unsubmitted: number;
  report_total: number;
  average_score: number | null;
}

export interface DashboardActionItem {
  kind: string;
  title: string;
  summary: string;
  risk_level: "低" | "中" | "高";
  duration_weeks: number;
  departments: string[];
  source_count: number;
  target_href: string;
  source_type?: ManagementActionSourceType;
  source_id?: number | null;
  source_task_id?: number | null;
  source_key?: string | null;
  source_report_ids?: number[];
}

export interface DashboardPeriodComparison {
  previous_period: Period;
  current: {
    completed: number;
    in_progress: number;
    blockers: number;
    high_risk_blockers: number;
  };
  previous: {
    completed: number;
    in_progress: number;
    blockers: number;
    high_risk_blockers: number;
  };
  deltas: {
    completed: number;
    in_progress: number;
    blockers: number;
    high_risk_blockers: number;
  };
}

export interface WeeklySection {
  count: number;
  items: Array<{
    title: string;
    description: string | null;
    status: string | null;
    related_project: string | null;
    sources: EvidenceSource[];
  }>;
}

export interface DashboardCurrentBlocker {
  id: number;
  title: string;
  description: string | null;
  risk_level: "低" | "中" | "高";
  duration_weeks: number;
  occurrence_count: number;
  sources: EvidenceSource[];
}

export interface AnalysisCoverage {
  reports_total: number;
  analyzed_reports: number;
  rate: number;
}

export interface EvidenceSource {
  report_id: number;
  title: string | null;
  employee_id: number | null;
  employee_name: string | null;
  department_name: string | null;
  period_start: string | null;
  period_end: string | null;
  source_text: string;
}

export interface ManagementItem {
  title: string;
  description: string | null;
  related_project: string | null;
  item_type: string;
  sources: EvidenceSource[];
}

export interface InsightItem {
  kind: string;
  title: string;
  conclusion: string;
  /** 长期卡点洞察关联的卡点详情；其他洞察没有这个字段。 */
  blocker_id?: number;
  risk_level: "低" | "中" | "高";
  evidence_count: number;
  duration_weeks?: number;
  departments: string[];
  employees: string[];
  period: { start: string | null; end: string | null };
  reason: string;
  sources: EvidenceSource[];
}

export interface TrendPoint {
  period_start: string;
  completed: number;
  ongoing: number;
  blockers: number;
  long_blockers: number;
  quality_score: number | null;
  analyzed_reports: number;
}

export interface EmployeeAnalysis {
  period: Period;
  coverage: AnalysisCoverage;
  trend: TrendPoint[];
  summary: string;
  current_blockers: Array<{ task_id: number; title: string; status: string; duration_weeks: number; sources: EvidenceSource[] }>;
  long_tasks: Array<{ task_id: number; title: string; status: string; duration_weeks: number; sources: EvidenceSource[] }>;
}

export interface DepartmentAnalysis {
  period: Period;
  coverage: AnalysisCoverage;
  counts: { total: number; completed: number; ongoing: number; blockers: number; by_type: Record<string, number> };
  trend: TrendPoint[];
  status: "正常" | "关注" | "注意";
  blockers: Array<{ id: number; title: string; risk_level: "低" | "中" | "高"; duration_weeks: number; sources: EvidenceSource[] }>;
  summary: string;
}

export interface BlockerItem {
  id: number;
  task_id: number;
  title: string;
  description: string | null;
  /** 生命周期：候选（单次提及）→ 正式 OPEN → 休眠 → 归档；RESOLVED 为人工/任务完成。 */
  status: "OPEN" | "RESOLVED" | "CANDIDATE" | "DORMANT" | "ARCHIVED";
  risk_level: "低" | "中" | "高";
  first_seen_date: string | null;
  last_seen_date: string | null;
  duration_weeks: number;
  occurrence_count: number;
  related_project: string | null;
  task_status: string;
  /** 原文资源需求直出时，后端可标记来源字段和确认状态；旧接口缺省时保持兼容。 */
  source_kind?: string | null;
  source_field?: string | null;
  confirmation_status?: "pending" | "confirmed" | "resolved" | string | null;
  pending_confirmation?: boolean;
  sources: Array<{
    report_id: number;
    title: string | null;
    employee_name: string | null;
    period_start: string | null;
    period_end: string | null;
    source_text: string;
  }>;
}

export type ManagementActionSourceType = "blocker" | "insight";
export type ManagementActionPriority = "low" | "medium" | "high";
export type ManagementActionStatus = "OPEN" | "IN_PROGRESS" | "WAITING" | "RESOLVED" | "CANCELLED";

export interface ManagementActionEvent {
  id: number;
  event_type: string;
  from_status: ManagementActionStatus | null;
  to_status: ManagementActionStatus | null;
  note: string | null;
  created_at: string | null;
}

/** 管理者从卡点或洞察发起、可持续回看的跟进事项。 */
export interface ManagementAction {
  id: number;
  source_type: ManagementActionSourceType;
  source_id: number | null;
  source_key: string | null;
  title: string;
  description: string | null;
  priority: ManagementActionPriority;
  status: ManagementActionStatus;
  owner_employee_id: number | null;
  owner_name: string | null;
  due_date: string | null;
  next_step: string | null;
  note: string | null;
  source_snapshot: Record<string, unknown> | null;
  source_stale: boolean;
  source_href: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** 会话统计：列表据此显示「有几条消息、几条没读」。 */
  message_count?: number;
  unread_count?: number;
  last_message?: ActionMessagePreview | null;
  events: ManagementActionEvent[];
}

/** 列表里用的最后一条消息摘要（不含正文全文）。 */
export interface ActionMessagePreview {
  id: number;
  author_role: ManagementActionAuthorRole;
  author_name: string | null;
  excerpt: string;
  created_at: string | null;
}

export type ManagementActionAuthorRole = "admin" | "owner";

export type ActionNotifyStatus =
  | "not_attempted"
  | "sent"
  | "undelivered"
  | "failed"
  | "skipped";

export interface ActionMessage {
  id: number;
  action_id: number;
  author_role: ManagementActionAuthorRole;
  author_name: string | null;
  body: string;
  created_at: string | null;
  /** 该消息推送出去的企业微信卡片是否已撤回。 */
  recalled?: boolean;
  recalled_at?: string | null;
  /** 只有管理侧拿得到投递结果；负责人侧不返回该字段。 */
  notify?: {
    status: ActionNotifyStatus;
    audience: NotificationAudience | null;
    error: string | null;
    attempts: number;
    notified_at: string | null;
    /** 前端不自己算 24 小时窗口，直接用它决定「撤回」按钮是否可点。 */
    can_recall?: boolean;
    recall_error?: string | null;
  };
}

export type NotificationAudience = "owner" | "admin";

/** 一次撤回尝试的结果。 */
export interface ActionMessageRecall {
  recalled: boolean;
  reason: string | null;
  msgid_count: number;
  failed: Array<{ msgid: string; reason: string }>;
}

/** 一次企业微信投递的结果，逐人列出谁收到、谁没收到。 */
export interface ActionMessageDelivery {
  status: ActionNotifyStatus;
  audience: NotificationAudience;
  target_count: number;
  delivered_count: number;
  delivered: Array<{ id: number; name: string }>;
  undelivered: Array<{ id: number | null; name: string | null; reason: string }>;
  error: string | null;
  skipped_reason: string | null;
  raw: Record<string, unknown>;
}

/** 投递前提检查：「他能不能收到」以及缺什么。
 *
 * `severity` 区分两类完全不同的原因（必须分开处理）：
 * - `blocking`：卡片发不出去（没有 userid 等），`can_notify=false`；
 * - `warning`：卡片能送到，但收件人点开用不了（地址是本机、未授权、未开登录），
 *   `can_notify` 仍为 true —— 此时不该说「对方收不到通知」。
 */
export interface ActionNotifyReadiness {
  can_notify: boolean;
  reasons: Array<{ code: string; message: string; severity?: "blocking" | "warning" }>;
}

export interface ActionMessageThread {
  items: ActionMessage[];
  unread_count: number;
  owner_name: string | null;
}

export interface ManagementActionListResponse {
  items: ManagementAction[];
  total: number;
  /** 未读回复总数（负责人侧与管理员侧各自口径）。 */
  unread_total?: number;
}

export interface ManagementActionCreate {
  source_type: ManagementActionSourceType;
  source_id?: number;
  source_key?: string;
  start: string;
  end: string;
}

export interface ManagementActionUpdate {
  status?: ManagementActionStatus;
  owner_employee_id?: number | null;
  due_date?: string | null;
  next_step?: string | null;
  note?: string | null;
  title?: string;
  priority?: ManagementActionPriority;
}

export interface DashboardDepartments {
  period: Period;
  items: DepartmentItem[];
}

export interface Paged<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface SearchSource {
  report_id: number;
  title?: string | null;
  employee_id?: number | null;
  employee_name?: string | null;
  department?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  /** 回答里是否真的写了 [#ID] 引用；false 表示只是读过、没引用 */
  cited?: boolean;
  reason?: string;
}

/** Agent 检索过程中的一步（后端 agent_service 的 step 事件）。 */
export interface AgentStep {
  step: number;
  tool: string;
  arguments: Record<string, unknown>;
  summary: string;
  elapsed_ms: number;
}

/** 范围被自动放宽的记录：原范围没命中时工具会往更宽的档位退一级。 */
export interface AgentEscalation {
  from: string;
  to: string;
  reason: string;
}

/** Agent 一轮问答的最终结果（done 事件的 payload）。 */
export interface AgentPayload {
  analysis_id?: number;
  question: string;
  conversation_id?: string | null;
  department_id?: number | null;
  department_name?: string | null;
  employee_id?: number | null;
  employee_name?: string | null;
  scope: "period" | "all";
  period: Period;
  resolved_scope?: {
    department?: string | null;
    employee?: string | null;
    time_source?: "question" | "page" | string;
  };
  answer: string;
  sources: SearchSource[];
  steps: AgentStep[];
  escalations: AgentEscalation[];
  searched_scopes: string[];
  read_count: number;
  /** 读了但被字符预算截断的周报 ID：非空表示没有读到全文 */
  truncated_report_ids?: number[];
  read_truncated?: boolean;
  /** 小范围问题直接把范围内全文交给模型，未走关键词检索 */
  full_context?: {
    enabled: boolean;
    report_count: number;
    chars: number;
    reason: string | null;
    keyword_coverage?: AgentKeywordCoverage;
  };
  batch_read?: {
    enabled: boolean;
    report_count: number;
    batch_count: number;
    batch_chars?: number;
    concurrency?: number;
    elapsed_ms?: number;
  };
  keyword_coverage?: AgentKeywordCoverage;
  coverage_status?: "complete" | "translation_gap" | string;
  coverage_gaps?: string[];
  evidence_type?: "weekly_reports" | "structured_data" | string;
  usage?: {
    model_call_count: number;
    prompt_tokens: number | null;
    completion_tokens: number | null;
    prompt_cache_hit_tokens: number | null;
    prompt_cache_miss_tokens: number | null;
  };
  cited_count: number;
  valid_citation_ids: number[];
  /** 回答里是否出现了有效引用；false 时来源只代表「读过」而非「引用」 */
  sources_cited?: boolean;
  max_steps: number;
  model: string | null;
  prompt_version: string;
}

export interface AgentKeywordCoverage {
  candidate_reports: number | null;
  matched_reports: number | null;
  translation_gaps: number;
  complete: boolean;
}

export interface AgentClarificationOption {
  label: string;
  employee_id?: number;
  department_id?: number;
}

export interface AgentClarification {
  reason: string;
  message: string;
  options: AgentClarificationOption[];
  question?: string;
}

export type AgentEvent =
  | { type: "delta"; text: string }
  | { type: "progress"; message?: string }
  | { type: "discard" }
  | ({ type: "step" } & AgentStep)
  | ({ type: "clarify" } & AgentClarification)
  | { type: "done"; payload: AgentPayload }
  | { type: "error"; message: string; steps?: AgentStep[]; analysis_id?: number };

export interface SearchResponse {
  question: string;
  conversation_id?: string | null;
  /** period=只看指定周期；all=不限周期检索全部历史周报 */
  scope: "period" | "all";
  period: Period;
  candidate_ids: number[];
  /** 过滤后命中的总篇数（截断前的分母） */
  matched_total: number;
  /** 本次真正喂给模型的篇数 */
  read_count: number;
  truncated: boolean;
  /** 关键词没命中、已退化为按时间取最近若干篇 */
  fallback: boolean;
  answer: string;
  sources: SearchSource[];
  not_found: boolean;
  model: string | null;
  prompt_version: string;
}

export interface AiHistoryItem {
  id: number;
  analysis_id?: number;
  /** search=一次 RAG 检索；agent_search=Agent 多轮工具检索 */
  kind?: "search" | "agent_search" | string;
  question: string | null;
  conversation_id?: string | null;
  department_id?: number | null;
  employee_id?: number | null;
  employee_name?: string | null;
  answer: string | null;
  not_found: boolean | null;
  scope?: "period" | "all" | null;
  period?: Period;
  candidate_ids?: number[];
  matched_total?: number | null;
  read_count?: number | null;
  full_context?: AgentPayload["full_context"];
  batch_read?: AgentPayload["batch_read"];
  truncated_report_ids?: number[];
  truncated?: boolean;
  fallback?: boolean;
  steps?: AgentStep[];
  escalations?: AgentEscalation[];
  sources?: SearchSource[];
  valid_citation_ids?: number[];
  resolved_scope?: AgentPayload["resolved_scope"];
  keyword_coverage?: AgentKeywordCoverage;
  coverage_status?: AgentPayload["coverage_status"];
  coverage_gaps?: string[];
  evidence_type?: AgentPayload["evidence_type"];
  usage?: AgentPayload["usage"];
  feedback?: { category: "scope" | "fact" | "evidence" | "omission" | "other"; reason: string | null; created_at: string | null } | null;
  prompt_version?: string | null;
  created_at: string | null;
  model: string | null;
  status: string;
  /** 同一 conversation_id 下按时间正序排列的完整问答。 */
  turns?: AiHistoryItem[];
}

export interface AgentMemoryItem {
  id: number;
  key: string;
  category: "company_rule" | "glossary" | "preference" | "project";
  content: string;
  source: "manual" | "conversation" | string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

/** 消息中心：每条消息都带稳定 id 与发生时间，前端据此做已读记忆。 */
export interface NotificationItem {
  id: string;
  kind: string;
  level: "error" | "warning" | "info";
  title: string;
  summary: string;
  hint: string | null;
  /** 原始报文（企微报错等），只在用户主动展开时展示。 */
  raw: string | null;
  at: string | null;
  href: string | null;
  action_label: string | null;
  meta: Record<string, unknown>;
}

export interface NotificationFeed {
  period: Period;
  generated_at: string;
  total: number;
  counts: Record<"error" | "warning" | "info", number>;
  items: NotificationItem[];
}

/* -------------------------------------------------------------------------- */
/* 经营看板（GET /api/operating-dashboard）                                    */
/* -------------------------------------------------------------------------- */

/**
 * 主题与成果的关系**由后端按 Task 关联给出**，前端不做下标配对或标题归并。
 * 没有可靠成果时 `has_reliable_outcomes=false`，此时应改用上下两列，
 * 不画误导性的流向线。
 */
export interface OperatingTheme {
  task_id: number;
  title: string;
  status: string;
  duration_weeks: number;
  related_project: string | null;
  first_seen: string | null;
  last_seen: string | null;
  /** 提到该主题的人数。只表达信息覆盖，不是投入工时。 */
  people_count: number;
  report_count: number;
  departments: string[];
  outcomes: OperatingOutcome[];
  evidence: OperatingEvidence[];
  source_report_ids: number[];
  has_reliable_outcomes: boolean;
  /**
   * 公司级主线：被多人或多篇确认过。由**后端**判定，前端不再自己重算
   * （两处各算一套迟早出现「界面说公司级、接口说个人」）。
   * 单人事只是某个人的待办，不该和公司主线并排列出。
   */
  is_company_level: boolean;
}

export interface OperatingOutcome {
  title: string;
  description: string | null;
  status: string | null;
  report_id: number;
  occurred_on: string | null;
}

export interface OperatingEvidence {
  report_id: number;
  employee_name: string | null;
  department_name: string | null;
  item_type: string;
  source_text: string;
  occurred_on: string | null;
}

export interface OperatingSource {
  report_id: number;
  title?: string | null;
  employee_id?: number | null;
  employee_name?: string | null;
  department_name?: string | null;
  excerpt?: string;
  source_text?: string;
  period_start?: string | null;
  period_end?: string | null;
}

/**
 * 关键发现。`can_track` 由后端根据「能否映射到真实 blocker/insight」判定；
 * `track` 是可原样回传给创建接口的来源字段 —— 前端不自己拼 source_key，
 * 否则服务端换键格式时按钮会变成必然 400 的死按钮。
 */
export interface OperatingDiscovery {
  kind: "progress" | "blocker" | "anomaly" | "collaboration";
  kind_label: string;
  source_kind: string;
  title: string | null;
  conclusion: string | null;
  reason: string | null;
  risk_level: string | null;
  evidence_count: number | null;
  departments: string[];
  employees: string[];
  period: Period | null;
  duration_weeks: number | null;
  sources: OperatingSource[];
  source_key: string | null;
  can_track: boolean;
  track: {
    source_type: string;
    source_id: number | null;
    source_key: string;
    href: string | null;
  } | null;
}

export interface OperatingAttention {
  kind: string;
  title: string;
  summary: string;
  risk_level: string;
  duration_weeks: number;
  departments: string[];
  source_count: number;
  target_href: string;
  source_type: string;
  source_id: number | null;
  source_task_id: number | null;
  source_key: string;
  source_report_ids: number[];
  sources: OperatingSource[];
}

export interface OperatingCoverage {
  reports_total: number;
  analyzed_reports: number;
  rate: number;
  /** 分析未跑完：页面必须提示「结论可能不完整」。 */
  partial: boolean;
  missing_analysis: number;
}

/** 经营看板结构化总结状态。result 是版本化 JSON，按 summary_type 渲染。 */
export interface OperatingSummaryState {
  summary_type: "weekly_company" | "monthly_company" | "monthly_departments" | "department";
  period: Period;
  department_id: number | null;
  reports_total: number;
  /** Monthly view: current successful single-report analyses for the same report set. */
  analysis_ready?: boolean;
  analysis_total?: number;
  analysis_completed?: number;
  analysis_failed?: number;
  analysis_processing?: number;
  analysis_waiting?: number;
  status: "not_generated" | "pending" | "running" | "success" | "failed" | "stale" | "empty";
  stale: boolean;
  result: Record<string, unknown> | null;
  created_at: string | null;
  model: string | null;
  last_error: string | null;
  source_report_ids: number[];
  queue_id?: number;
  /** 队列最近一次入队或领取时间（后端 UTC naive），用于区分正常等待与久等。 */
  queue_updated_at?: string | null;
  /** 这版结论是按哪版提示词写的（页面据此说明它的来路）。 */
  prompt_version?: string;
  /**
   * **生成时**覆盖的篇数。与 `reports_total`（当前篇数）一比就知道这份结论之后
   * 又入库了多少篇 —— `stale` 时页面用它说明「结论有多旧」，而不是只说一句
   * 「事实已变化」。旧结论没有这个字段时为 null。
   */
  generated_report_count?: number | null;
  department_cards?: {
    status: "not_generated" | "pending" | "running" | "success" | "failed";
    ready_to_generate: number;
    last_error: string | null;
    items: Array<{
      department_id: number;
      status: "current" | "waiting_analysis" | "not_generated" | "failed";
      analysis_total: number;
      analysis_completed: number;
      analysis_failed: number;
      card: OperatingDepartmentBrief["ai_card"] | null;
    }>;
  };
}

export interface OperatingMonthlyMonth {
  month: string;
  start: string;
  end: string;
  label: string;
  short_label: string;
  report_total: number;
  analyzed_reports: number;
  /** 该月分析覆盖率 0~1。低于门槛的月份不参与「变化」比较。 */
  coverage: number;
}

/** 公司主线（问题①）：按业务对象聚合，不是 Task。 */
export interface OperatingMainline {
  name: string;
  item_count: number;
  report_count: number;
  employee_count: number;
  departments: string[];
  department_count: number;
  months: string[];
  month_count: number;
  is_cross_department: boolean;
  is_cross_month: boolean;
  /** 「跨部门 · 跨月」这类档位文字，由后端拼好。 */
  scope_label: string;
  progress_count: number;
  issue_count: number;
  /**
   * **模型读完成周报写的那句话**（方案 A：模型说话、代码给数）。
   * 缺席表示模型没写到这一条或还没生成过简报 —— 页面此时退回代码算的字段，
   * 两者不同时出现（同一件事说两遍是这一页冗长的主要来源）。
   */
  ai_text?: string;
  latest: {
    title: string | null;
    status: string | null;
    item_type: string;
    report_id: number;
    month: string | null;
    department_name: string | null;
    employee_name: string | null;
  } | null;
  source_report_ids: number[];
}

export interface OperatingMainlines {
  items: OperatingMainline[];
  /** 跨部门主线总数（公司级主线只收跨部门的）。 */
  total: number;
  cross_department_count: number;
  cross_month_count: number;
  /** 单部门主线条数 —— 它们不在这一块显示，归「按部门」一节。 */
  single_department_total: number;
  min_items: number;
  basis: string;
}

/** 变化类型：新增 / 明显推进 / 停滞 / 已完成 / 恶化 / 持续推进。 */
export type OperatingChangeKind =
  | "new"
  | "progress"
  | "stalled"
  | "completed"
  | "worsening"
  | "ongoing";

export interface OperatingChange {
  kind: OperatingChangeKind;
  kind_label: string;
  /** 语气值，与 ui.tsx 的 Badge tone 对齐；前端只渲染不映射。 */
  tone: string;
  title: string;
  detail: string;
  /** 模型写的「这个月它实际动了什么」；见 `OperatingMainline.ai_text`。 */
  ai_text?: string;
  month: string;
  departments: string[];
  progress_count: number;
  issue_count: number;
  ongoing_count: number;
  baseline_month: string | null;
  baseline_progress: number | null;
  baseline_issues: number | null;
  /** 该类型需要基线才能成立（新增 / 明显推进 / 恶化）。 */
  needs_baseline: boolean;
  source_report_ids: number[];
}

export interface OperatingMonthlyChanges {
  items: OperatingChange[];
  total: number;
  month: string | null;
  month_label: string | null;
  coverage: number;
  baseline: {
    month: string | null;
    comparable: boolean;
    reason: string | null;
    current_coverage?: number;
    previous_coverage?: number;
    /** 被排除的月份及原因，页面要如实列出。 */
    skipped: Array<{ month: string; reason: string; coverage: number }>;
  };
  /** 基线月有证据、本月没有已分析证据的对象：只报数，不判「已完成」。 */
  silent: { count: number; names: string[] };
  unattributed_items: number;
  routine_items: number;
  thin_items: number;
  basis: string;
}

/** 卡住了（问题③）：卡点或「多条线都在等它」的依赖对象。 */
export interface OperatingStuckItem {
  kind: "blocker" | "dependency";
  title: string;
  /** 周报原文片段或结构化事实描述，可点来源周报核对。 */
  detail: string;
  /** 模型写的「谁在等它、等到什么程度」；见 `OperatingMainline.ai_text`。 */
  ai_text?: string;
  duration_weeks: number | null;
  occurrence_count: number | null;
  risk_level: string | null;
  departments: string[];
  department_count: number;
  months: string[];
  month_count: number;
  first_seen?: string | null;
  last_seen?: string | null;
  evidence_count: number;
  /** 只有卡点来源才有；可跳到卡点详情。 */
  blocker_id: number | null;
  source_report_ids: number[];
  /**
   * 这一条已作为「公司级共性问题」单独展开。
   * 页面把它压成一行指针（不重复部门列表与计数），但**计数照实**：
   * 卡头写的是全部条数，不是剩下那几条。
   */
  is_shared?: boolean;
}

export interface OperatingStuck {
  items: OperatingStuckItem[];
  total: number;
  min_weeks: number;
  /** 只有一篇周报、无部门证据，因而未列入的条数（如实报数，不静默丢弃）。 */
  thin_blockers: number;
  basis: string;
}

/** 异常（问题④）：平时翻周报不容易发现的那几类。 */
export interface OperatingAnomaly {
  kind: "repeated_issue" | "department_growth" | "duplicate_effort" | "status_conflict";
  kind_label: string;
  tone: string;
  title: string;
  detail: string;
  metrics: Array<{ label: string; value: number }>;
  people_count: number;
  departments: string[];
  months: string[];
  evidence_count: number;
  samples: Array<{
    title?: string | null;
    department_name?: string | null;
    employee_name?: string | null;
    report_id?: number | null;
    occurred_on?: string | null;
  }>;
  /** 模型写的「这个模式意味着什么」；见 `OperatingMainline.ai_text`。 */
  ai_text?: string;
  /** 仅 department_growth：逐期计数与周期标签。 */
  series?: number[];
  periods?: string[];
  source_report_ids: number[];
  /** 已归入「公司级共性问题」（同 OperatingStuckItem.is_shared）。 */
  is_shared?: boolean;
}

export interface OperatingAnomalies {
  items: OperatingAnomaly[];
  total: number;
  min_people: number;
  basis: string;
}

/** 需要管理层介入（问题⑤）。每条的五个字段都由后端算好，前端不拼文案。 */
export interface OperatingIntervention {
  kind: string;
  title: string;
  reason: string;
  impact: string;
  owner: string;
  duration: string;
  risk_level: string | null;
  blocker_id: number | null;
  evidence_count: number;
  rank: number;
  /** 模型写的「老板要拍什么板」；见 `OperatingMainline.ai_text`。 */
  ai_text?: string;
  suggested_action: string;
  /** rule = 代码给的规则动作；ai = 模型按事实写的动作。 */
  action_source: "rule" | "ai";
  ai_reason?: string | null;
  must_decide?: string | null;
  /** 同一批证据的另一个视角（按对方是谁 / 按问题是什么），不重复占一行。 */
  corroboration: Array<{ kind: string; label: string; detail: string }>;
  source_report_ids: number[];
}

export interface OperatingInterventions {
  items: OperatingIntervention[];
  total: number;
  basis: string;
}

export interface OperatingDepartmentCard {
  id: number;
  name: string;
  employee_count: number;
  exempt: boolean;
  open_blockers: number;
  high_risk_blockers: number;
  theme_count: number;
  top_theme: OperatingTheme | null;
  /** 该部门本周期的周报分析覆盖（免提交部门为全 0，前端显示「—」）。 */
  report_coverage: OperatingCoverage;
}

export interface OperatingDepartmentDetail {
  id: number;
  name: string;
  employee_count: number;
  exempt: boolean;
  open_blockers: number;
  high_risk_blockers: number;
  themes: OperatingTheme[];
  outcomes: OperatingOutcome[];
}

/**
 * 看板视图标识。**后端仍保留 `department`**（接口与总结链路都在用），
 * 但前端页面已不再展示该视图；这里保留它是因为这是 API 的真实取值域。
 */
export type OperatingViewKey = "overview" | "weekly" | "monthly" | "department";

export interface OperatingDashboardPayload {
  /** 响应自证身份：切视图期间前端靠它判断这份数据属不属于当前视图。 */
  view: OperatingViewKey;
  period: Period;
  coverage: OperatingCoverage;
  summary: OperatingSummaryState;
  attention: OperatingAttention[];
  themes?: OperatingTheme[];
  /** 其中属于「公司级主线」（多人或多篇确认）的条数。 */
  company_theme_count?: number;
  discoveries?: OperatingDiscovery[];
  months?: OperatingMonthlyMonth[];
  /** 每月视图的五块结论，与页面上的五个问题一一对应。 */
  mainlines?: OperatingMainlines;
  changes?: OperatingMonthlyChanges;
  stuck?: OperatingStuck;
  anomalies?: OperatingAnomalies;
  interventions?: OperatingInterventions;
  /** 公司级共性问题：被 ≥3 个部门同时涉及的条目，从部门卡片里抽出来单独列。 */
  shared_issues?: OperatingSharedIssue[];
  /**
   * 部门简报：每个部门一张卡（问题①~⑤ 在部门这一侧的切片）。
   * **不要与 `departments` 混用** —— 后者是已裁掉的部门视图的形状
   * （`OperatingDepartmentCard[]`），两者同名不同形，混用会让页面拿错数据崩掉。
   */
  department_briefs?: OperatingDepartmentBrief[];
  /**
   * AI 写的月度简报（`summary_type=monthly_company` 的 result）。
   *
   * 缺席时页面不展示月度经营判断；只有逐篇分析完成、且当前周期简报成功时才展示。
   * 数字和证据仍取自结构化事实，不由模型改写。
   */
  narrative?: OperatingMonthlyNarrative;
  selected?: OperatingDepartmentDetail | null;
  collaboration?: OperatingDiscovery[];
  /**
   * 已有的人工效度判定（只读回显）。后端对**所有视图**统一附带
   * （路由在缓存合并之后附加，不参与任何计算），缺失表示这个周期还没人判过。
   */
  feedback?: OperatingFeedbackIndex;
}

/** AI 月度简报。见后端 `merge_monthly_narrative`（只附不改，事实仍由代码算）。 */
export interface OperatingMonthlyNarrative {
  headline: string;
  change_summary?: string;
  mainline_summary?: string;
  mainline_names?: string[];
  /** 依据范围的一句话（覆盖多少篇、其中分析过多少篇），没有时为空。 */
  coverage_note?: string | null;
  highlights: Array<{
    title: string;
    detail: string;
    kind: "progress" | "risk" | "decision";
    source_report_ids: number[];
  }>;
  /** 拿到 AI 一句话摘要的部门数（其余部门卡仍用代码算的结论句）。 */
  department_summaries?: number;
  generated_at?: string | null;
  model?: string | null;
}

/** 公司级共性问题的一条。同一件事被多部门同时面对，需要公司层面的动作。 */
export interface OperatingSharedIssue {
  title: string;
  /** 同一件事在别的分类口径下的叫法（如「院校侧反馈与确认」）。 */
  aliases: string[];
  /** 模型写的「为什么公司层面得有人出面」；见 `OperatingMainline.ai_text`。 */
  ai_text?: string;
  departments: string[];
  department_count: number;
  /** 它在几类清单里出现过：卡点 / 被依赖 / 反复出现 / 管理层介入。 */
  kinds: string[];
  duration_weeks: number | null;
  evidence_count: number;
  people_count: number;
  blocker_id: number | null;
  /** 若这条已在管理层清单里，带上它的名次与建议动作。 */
  rank: number | null;
  suggested_action: string | null;
  action_source: "rule" | "ai" | null;
  source_report_ids: number[];
}

/** 部门简报里「卡点 / 在等谁」的条目。 */
export interface OperatingBriefBlocker {
  title: string;
  detail: string;
  risk_level: string | null;
  duration_weeks: number | null;
  evidence_count: number;
  blocker_id: number | null;
  source_report_ids: number[];
}

/** 部门简报：一个部门自己为主动推的事 + 在等的事（公司级共性问题不在这里展开）。 */
export interface OperatingDepartmentBrief {
  rank: number;
  name: string;
  /** 后端算好的一句话结论（计数口径），页面原样渲染。 */
  conclusion: string;
  /**
   * AI 写的一句话部门小结（在忙什么、卡在哪、要不要老板出手）。
   * 只在生成过月度简报、且模型给出的部门名与本部门对得上时才有 —— 缺席时页面
   * 用 `conclusion`，不要自己拼句。
   */
  ai_summary?: string;
  /** 本部门证据支持的结构化结论；缺失时兼容旧版一句话简报。 */
  ai_card?: {
    headline: string;
    progress: string;
    blocker: string;
    suggested_action: string;
    source_report_ids: number[];
  };
  ai_status?: {
    status: "current" | "waiting_analysis" | "not_generated" | "failed";
    analysis_total: number;
    analysis_completed: number;
    analysis_failed: number;
  };
  /**
   * 本部门参与的未关闭卡点数（Blocker 表 OPEN 且窗口内有证据，按证据行员工部门
   * 归属）——与卡点处理中心 `filter=open` + 周期 + 部门筛选完全同口径，
   * 点徽标跳过去看到的列表条数就是这个数，每条可溯源到卡点详情与来源周报。
   * 2026-09-28 替换旧的 open_issues（主线问题行求和：不看解决状态、重复提及
   * 重复计、跨部门整桶归属，数字虚高且不可追溯）。
   */
  open_blockers: number;
  /** 部门 id（用于跳转卡点页按部门筛选；无证据行的部门为 null）。 */
  department_id: number | null;
  /** 与本部门相关的公司级共性问题件数（不展开，只为解释为何下面没有更多条目）。 */
  shared_issue_count: number;
  mainlines: Array<{
    title: string;
    scope_label: string;
    progress_count: number;
    issue_count: number;
    report_count: number;
    latest: {
      title: string | null;
      status: string | null;
      item_type: string;
      report_id: number;
      month: string | null;
      department_name: string | null;
      employee_name: string | null;
    } | null;
    source_report_ids: number[];
    /** 只有这个部门在推（跨部门主线的相反面）—— 专属的排在前面。 */
    is_solo: boolean;
    /** 除本部门外还有几个部门在推同一条。 */
    other_department_count: number;
  }>;
  mainline_total: number;
  /** 其中只有本部门在推的条数。 */
  solo_mainline_count: number;
  blockers: OperatingBriefBlocker[];
  waiting: OperatingBriefBlocker[];
  changes: Array<{
    title: string;
    kind: string;
    kind_label: string;
    tone: string;
    issue_count: number;
    progress_count: number;
    source_report_ids: number[];
  }>;
  anomalies: Array<{
    title: string;
    kind_label: string;
    tone: string;
    people_count: number;
    evidence_count: number;
    source_report_ids: number[];
  }>;
  interventions: Array<{
    rank: number;
    title: string;
    impact: string;
    suggested_action: string;
    action_source: "rule" | "ai";
    source_report_ids: number[];
  }>;
}

/** 月度总结的 result（summary_type=monthly_company）。 */
export interface OperatingMonthlySummaryResult {
  executive_summary?: string;
  /**
   * 需要管理层介入的建议动作。**只补动作，不带事实**：
   * 原因 / 影响 / 责任方 / 持续时间由代码算，前端从 `interventions` 读。
   */
  management_attention?: Array<{
    title: string;
    reason?: string;
    impact?: string;
    owner?: string;
    duration?: string;
    suggested_action?: string;
    /** 需要管理层拍板的那个问题。 */
    must_decide?: string;
    source_report_ids: number[];
  }>;
}

/** 周度总结的 result（summary_type=weekly_company）。 */
export interface OperatingWeeklySummaryResult {
  headline?: string;
  themes?: Array<{ title: string; facts: string[]; stage: string; source_report_ids: number[] }>;
  attention?: Array<{
    title: string;
    reason: string;
    suggested_action: string;
    source_report_ids: number[];
  }>;
}

/** 部门总结的 result（summary_type=department）。 */
export interface OperatingDepartmentSummaryResult {
  summary?: string;
  focus?: Array<{ title: string; facts: string[]; stage: string; source_report_ids: number[] }>;
  blockers?: Array<{ title: string; risk_level: string; reason: string; source_report_ids: number[] }>;
  dependencies?: Array<{
    title: string;
    counterpart: string;
    reason: string;
    source_report_ids: number[];
  }>;
}

/* -------------------------------------------------------------------------- */
/* 经营看板 · 综合视图（view=overview）                                        */
/* -------------------------------------------------------------------------- */

/** 概要指标卡。delta.available=false 表示上期没有可比数据，前端不能显示数字。 */
export interface OperatingMetricDelta {
  available: boolean;
  value?: number;
  text: string;
  tone: string;
  /** 两侧覆盖率。不可比时用来说明「差在哪」，可比时作为数字的可信度依据。 */
  current_coverage?: number;
  previous_coverage?: number;
}

/** 指标的计算基数：这个数字是基于多少篇已分析周报算出来的。 */
export interface OperatingMetricBasis {
  analyzed_reports: number;
  reports_total: number;
  coverage: number;
}

export interface OperatingMetric {
  key: string;
  label: string;
  value: number;
  unit: string;
  sub: string;
  basis?: OperatingMetricBasis;
  delta: OperatingMetricDelta;
}

/** 今天值得关注的一条经营信号。数字全部由后端算出，模型只做一句话压缩。 */
export interface OperatingSignal {
  signal_type: string;
  /** 稳定键（signal_type:业务对象名）。展示标题里的数字随数据变化，AI 结论按这个键贴回。 */
  signal_key?: string;
  title: string;
  conclusion: string;
  /** 结论来源：ai = 周度总结的逐条分析；rules = 规则拼的计数句（AI 未覆盖时兜底）。 */
  conclusion_source?: "ai" | "rules";
  label: string;
  tone: string;
  impact_workstreams: number;
  impact_departments: number;
  duration_periods: number;
  growing: boolean;
  evidence_count: number;
  attention_score: number;
  sources: OperatingEvidence[];
  source_report_ids: number[];
  /** 只在后端能匹配到唯一、真实的卡点或洞察来源时提供。 */
  can_track?: boolean;
  track?: {
    source_type: "blocker" | "insight";
    source_id: number | null;
    source_key: string;
    href: string | null;
  } | null;
}

/** 瓶颈的每条来源事项；用于抽屉里展开核对。 */
/** 结论的来源事项；用于抽屉里展开核对。
 *
 * 字段大多可选：不同结论的来源来自不同投影（依赖/卡点来自事项行，带 id 与
 * description；停滞问题来自 `insight_service.task_sources`，是周报投影）。
 * 唯一共同点是 `report_id`，它也是前端唯一必须能跳转的字段。
 */
export interface OperatingBottleneckSource {
  id?: number;
  title?: string | null;
  description?: string | null;
  status?: string | null;
  item_type?: string;
  risk_level?: string | null;
  project?: string | null;
  report_id: number;
  employee_name?: string | null;
  department_name?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  occurred_on?: string | null;
  /** 原文片段；事项行叫 source_text，周报投影叫 excerpt，两者都读。 */
  source_text?: string;
  excerpt?: string;
}

export interface OperatingBottleneck {
  rank?: number;
  target: string;
  /** 卡片正文：AI 总结生成后由模型逐条分析；缺席时前端退回规则计数串。 */
  conclusion?: string;
  conclusion_source?: "ai" | "rules";
  /** 本期被同一被依赖对象挡住的工作数（业务对象去重）。 */
  workstream_count: number;
  department_count: number;
  employee_count: number;
  evidence_count: number;
  severity: string;
  severity_tone: string;
  matched_keywords: string[];
  departments: string[];
  employees: string[];
  sources: OperatingBottleneckSource[];
  source_report_ids: number[];
  unattributed_count: number;
}

export interface OperatingFocusCategory {
  label: string;
  /** 参与人次：同一人多条事项只计一次。是人员参与度，不是工时占比。 */
  people_entries: number;
  share: number;
  employee_count: number;
  department_count: number;
  report_count: number;
  matched_keywords: string[];
  top_projects: string[];
}

export interface OperatingFocusDistribution {
  total_entries: number;
  categories: OperatingFocusCategory[];
  basis: string;
}

export interface OperatingDuplicate {
  project: string;
  department_count: number;
  employee_count: number;
  departments: string[];
  employees: string[];
  evidence_count: number;
  sources: OperatingBottleneckSource[];
  source_report_ids: number[];
  /** AI 写的分析（周度总结 item_notes）；缺席时页面退回规则描述。 */
  conclusion?: string;
  conclusion_source?: "ai" | "rules";
}

/** 状态不一致的判定依据：两侧原文，供人工核对这个判定成不成立。 */
export interface OperatingConflictEvidence {
  side: "finished" | "open";
  employee_name: string;
  title: string;
  report_id: number;
  period_start?: string | null;
  period_end?: string | null;
  occurred_on?: string | null;
}

export interface OperatingConflict {
  name: string;
  title: string;
  finished_by: string[];
  open_by: string[];
  departments: string[];
  source_report_ids: number[];
  /** 参与判定的原文条目（两侧各若干条）。缺省时表示后端未提供。 */
  evidence?: OperatingConflictEvidence[];
  /** AI 写的分析（周度总结 item_notes）；缺席时页面退回规则描述。 */
  conclusion?: string;
  conclusion_source?: "ai" | "rules";
}

export interface OperatingFrictions {
  duplicates: OperatingDuplicate[];
  conflicts: OperatingConflict[];
  counts: {
    duplicate_effort: number;
    status_conflict: number;
  };
  /** 典型案例：重复推进的第一条，用于卡片底部举例。 */
  case: OperatingDuplicate | null;
}

export interface OperatingProjectActivity {
  period_end: string;
  /** null = 该周期没有结构化分析数据，页面显示「—」而不是 0。 */
  report_count: number | null;
  has_data: boolean;
}

export interface OperatingProject {
  name: string;
  status_label: string;
  tone: string;
  /** 后端按已算出的事实拼的「说人话」结论（状态 + 参与面 + 逐周趋势）。
   *  可选：旧后端进程 / 旧缓存可能没有这个字段，页面需自行兜底。
   *  AI 总结生成后路由层会用模型写的 project_notes 覆盖它。 */
  conclusion?: string;
  /** 结论来源：ai = 大模型分析（周度总结 project_notes），rules = 规则归纳兜底。 */
  conclusion_source?: "ai" | "rules";
  duration_weeks: number;
  employee_count: number;
  department_count: number;
  departments: string[];
  report_count: number;
  source_report_ids: number[];
  evidence: Array<{
    title: string | null;
    status: string | null;
    item_type: string;
    report_id: number;
    employee_name: string | null;
    department_name: string | null;
  }>;
  activity: OperatingProjectActivity[];
}

export interface OperatingProjectProgress {
  items: OperatingProject[];
  periods: string[];
}

export interface OperatingTrendSeries {
  title: string;
  counts: number[];
  periods: string[];
  direction: string;
  tone: string;
  total_evidence: number;
  first_count: number;
  last_count: number;
  /** 各周期的分析覆盖率（方向按占比判，不按绝对篇数）。 */
  coverage?: number[];
  /** 各周期的已分析篇数（counts 的分母）。 */
  denominators?: number[];
  /** AI 写的解读（周度总结 item_notes）；缺席时页面只显示表格不显示解读。 */
  conclusion?: string;
  conclusion_source?: "ai" | "rules";
}

/** 因分析覆盖不足被排除出趋势比较的周期。 */
export interface OperatingTrendSkipped {
  period: string;
  coverage: number;
  analyzed: number;
  total: number;
}

export interface OperatingTrends {
  periods: string[];
  series: OperatingTrendSeries[];
  insufficient: string[];
  analyzed_periods?: string[];
  skipped_periods?: OperatingTrendSkipped[];
  reason?: string;
  basis: string;
}

export interface OperatingWorkstreamSummary {
  total: number;
  objects_total: number;
  min_items: number;
  top: Array<{ name: string; item_count: number }>;
  /** 总览指标详情用；每条工作主线附带可回看的周报来源。 */
  items: Array<{ name: string; item_count: number; source_report_ids: number[] }>;
}

export interface OperatingActivityPoint {
  period_end: string;
  label: string;
  reports: number;
  analyzed: number;
}

/** 一条结论的人工判定结果。 */
export interface OperatingFeedbackEntry {
  verdict: "correct" | "wrong" | "unclear";
  label: string;
  reason: string | null;
  reviewer: string;
  updated_at?: string | null;
}

/** 已有判定，按结论指纹索引。同一结论可被多人判定，内层按判定人分组。 */
export type OperatingFeedbackIndex = Record<string, Record<string, OperatingFeedbackEntry>>;

/** 可判定的模块。与后端 `FEEDBACK_MODULES` 一一对应。 */
export type OperatingFeedbackModule =
  | "attention"
  | "bottleneck"
  | "duplicate"
  | "conflict"
  | "project";

/** 图片版单页看板的响应。`themes` 只是补充，`attention` 才是首屏主角。 */
export interface OperatingOverviewPayload {
  view: "overview";
  period: { start: string; end: string };
  period_label: string;
  coverage: OperatingCoverage;
  metrics: OperatingMetric[];
  activity: { periods: string[]; points: OperatingActivityPoint[]; basis: string };
  attention: {
    /** 首屏展示的前几条关注信号。 */
    items: OperatingSignal[];
    /** 总览指标详情用；包含所有计入 total 的关注信号。 */
    all_items: OperatingSignal[];
    total: number;
  };
  workstreams: OperatingWorkstreamSummary;
  themes: OperatingTheme[];
  focus: OperatingFocusDistribution;
  bottlenecks: { items: OperatingBottleneck[]; total: number; unattributed_count: number };
  frictions: OperatingFrictions;
  projects: OperatingProjectProgress;
  trends: OperatingTrends;
  attention_items: OperatingAttention[];
  /** 已缓存的 AI 周度总结（含过期标记）。不触发模型调用。 */
  summary: OperatingSummaryState;
  /** 已有的人工效度判定（只读回显）。缺失表示这个周期还没人判过。 */
  feedback?: OperatingFeedbackIndex;
  /** 上下文字段，与 /api/dashboard 口径一致。 */
  last_sync_at?: string | null;
}

/** 准确率统计的单项（按模块或整体）。 */
export interface OperatingFeedbackRate {
  correct: number;
  wrong: number;
  unclear: number;
  decided: number;
  total: number;
  /** 样本不足时为 null —— 一条判对的结论算出 100% 会误导人信任它。 */
  accuracy: number | null;
  enough_sample: boolean;
  min_sample: number;
}

export interface OperatingFeedbackSummary {
  period: { start: string | null; end: string | null };
  modules: Record<string, OperatingFeedbackRate>;
  overall: OperatingFeedbackRate;
  verdict_labels: Record<string, string>;
}
