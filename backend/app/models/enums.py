"""枚举常量。

使用字符串常量而非 sa.Enum，原因：
SQLite 下 sa.Enum 会生成 CHECK 约束，后续扩展枚举值需要改写约束，
迁移成本高；字符串列 + 应用层常量在 PG / SQLite 行为一致。
"""


class DataSource:
    """数据来源系统。"""

    WECOM = "wecom"
    OA = "oa"
    MANUAL = "manual"
    # 简道云历史周报（2026 第 5~22 周）一次性导入；仅历史建档，
    # 不参与企微同步链路，导入的离职员工不会被 employee_sync 误标记。
    JIANDAOYUN = "jiandaoyun"


class RawSourceType:
    """原始数据业务类型（RawRecord.source_type）。

    V1 只有 weekly_report；后续扩展 approval / document / oa_task /
    message / application 时在此追加即可，无需改动表结构。
    """

    WEEKLY_REPORT = "weekly_report"
    APPROVAL = "approval"
    DOCUMENT = "document"
    OA_TASK = "oa_task"
    MESSAGE = "message"
    APPLICATION = "application"


class ProcessStatus:
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EntityType:
    WEEKLY_REPORT = "weekly_report"
    EMPLOYEE = "employee"
    DEPARTMENT = "department"
    WORK_ITEM = "work_item"


class AnalysisType:
    SUMMARY = "summary"
    EXTRACT_WORK_ITEMS = "extract_work_items"
    SIMILARITY = "similarity"
    # 每篇周报一次调用产出的整套结构化分析（摘要/事项/卡点/计划/风险/评分）
    REPORT_ANALYSIS = "report_analysis"


class OperatingSummaryType:
    """经营看板结构化总结的作用域类型。

    三种类型的输出 Schema、提示词和缓存键都不同；混成一个值会让「上周」「本月」
    「部门」共用一条队列记录，重算任一个都会把另外两个标记成过期。
    """

    WEEKLY_COMPANY = "weekly_company"
    MONTHLY_COMPANY = "monthly_company"
    MONTHLY_DEPARTMENTS = "monthly_departments"
    DEPARTMENT = "department"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.WEEKLY_COMPANY, cls.MONTHLY_COMPANY, cls.MONTHLY_DEPARTMENTS, cls.DEPARTMENT}


class AIAnalysisStatus:
    SUCCESS = "success"
    FAILED = "failed"


class AnalysisQueueStatus:
    """后台分析队列状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TaskStatus:
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class BlockerStatus:
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    # 生命周期扩展：解析提及默认只进候选层；静默降档由证据推导，不人工维护。
    CANDIDATE = "CANDIDATE"
    DORMANT = "DORMANT"
    ARCHIVED = "ARCHIVED"


class ManagementActionStatus:
    """人工管理事项状态；不会由 AI 或同步过程自动修改。"""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING = "WAITING"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.OPEN, cls.IN_PROGRESS, cls.WAITING, cls.RESOLVED, cls.CANCELLED}


class ManagementActionPriority:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.HIGH, cls.MEDIUM, cls.LOW}


class ManagementActionAuthor:
    """跟进会话中一条消息的发言人身份。

    只区分「管理侧」与「负责人侧」，**不区分子账号**：授权模型里
    任何管理员都能看到全部事项，会话没有「只看自己参与的」这层语义。
    owner 侧则恒为被指派的那名员工（接口层强制，不会出现第三方发言人）。
    """

    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.ADMIN, cls.OWNER}


class ActionNotifyStatus:
    """跟进消息的企业微信投递结果。

    NOT_ATTEMPTED 与 SKIPPED 必须分开：前者是「根本没发」（定时任务尚未处理），
    后者是「发了但被冷却拦住」—— 运维据此判断「为什么他没收到通知」时的
    处理路径完全不同。UNDELIVERED 沿用第 25 条纪律：企微返回 errcode=0
    仍可能把收件人列进 invaliduser，那不是发送失败，重试也不会让人变得可投递。
    """

    NOT_ATTEMPTED = "not_attempted"
    SENT = "sent"
    UNDELIVERED = "undelivered"
    FAILED = "failed"
    SKIPPED = "skipped"

    @classmethod
    def all(cls) -> set[str]:
        return {
            cls.NOT_ATTEMPTED,
            cls.SENT,
            cls.UNDELIVERED,
            cls.FAILED,
            cls.SKIPPED,
        }


class NotificationAudience:
    """一次投递面向哪一侧。冷却按 (事项, 受众) 分别计时。"""

    OWNER = "owner"
    ADMIN = "admin"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.OWNER, cls.ADMIN}


class ReportType:
    WEEKLY = "weekly"
    DAILY = "daily"
    MONTHLY = "monthly"
    OTHER = "other"


class EmployeeStatus:
    ACTIVE = "active"
    DISABLED = "disabled"
    PENDING = "pending"
    RESIGNED = "resigned"
    MISSING = "missing"  # 接口未返回但未确认离职（仅标记，不自动生效）


class DepartmentStatus:
    ACTIVE = "active"
    INACTIVE = "inactive"


class SyncType:
    """同步任务类型（sync_job.type）。"""

    DEPARTMENTS = "departments"
    EMPLOYEES = "employees"
    WEEKLY_REPORTS = "weekly_reports"


class SyncJobStatus:
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class WorkItemStatus:
    """需求第 8.2 节示例输出中的状态。"""

    DONE = "完成"
    IN_PROGRESS = "进行中"
    PLANNED = "计划中"
    UNCONFIRMED = "待确认"


class WorkItemCategory:
    """需求第 9 节固定枚举。

    注意：这是 V1 默认值。需求明确要求分类可配置，
    后续应在后台提供分类管理，不得散落在业务代码里做 if/else。
    """

    SYSTEM_DEV = "系统开发"
    SYSTEM_TEST = "系统测试"
    AI_APP = "AI应用"
    KNOWLEDGE_BASE = "知识库"
    DATA_MAINTENANCE = "数据维护"
    BUSINESS_SUPPORT = "业务支持"
    TRAINING = "培训"
    OPERATION = "运营"
    ADMIN = "行政"
    OTHER = "其他"

    @classmethod
    def all(cls) -> list[str]:
        return [
            cls.SYSTEM_DEV,
            cls.SYSTEM_TEST,
            cls.AI_APP,
            cls.KNOWLEDGE_BASE,
            cls.DATA_MAINTENANCE,
            cls.BUSINESS_SUPPORT,
            cls.TRAINING,
            cls.OPERATION,
            cls.ADMIN,
            cls.OTHER,
        ]


class ReminderStatus:
    SENT = "sent"
    FAILED = "failed"


class ReminderChannel:
    WECOM_TEXT_CARD = "wecom_text_card"


class ReminderRunStatus:
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    # 请求成功、有人收到、也有人被企微判定为不可投递（不在应用可见范围）。
    # 与 FAILED 分开：这不是「发送失败」，重试也不会让人变得可投递，
    # 需要管理员去调自建应用的可见范围。
    PARTIAL = "partial"


class LoginRole:
    """登录后的角色（三档）。

    - admin：可读可写，数据范围为**全公司**（同步、催交、导出、设置、AI 全量能力）
    - dept_admin（部门管理员）：可读可写，但数据范围限定为**其授权部门及其下级
      子部门**；系统设置、登录授权、经营看板、全员周期总结等公司级能力不可用
    - member：只能访问「发给我的事项」（/api/my-actions），其余路径 GET 也 403

    登录授权只决定「能不能进系统、以什么角色进」；dept_admin 的数据范围由
    `services/data_scope` 在每个请求里解析并注入查询条件，回源复核机制
    （撤销勾选立即生效）对三档角色一致。
    """

    MEMBER = "member"
    ADMIN = "admin"
    DEPT_ADMIN = "dept_admin"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.MEMBER, cls.ADMIN, cls.DEPT_ADMIN}


class LoginSource:
    """会话来源。

    password：部署级管理密码（唯一超管，企微配置坏掉时仍能进系统兜底）
    wecom   ：企业微信成员登录（网页授权或扫码）
    """

    PASSWORD = "password"
    WECOM = "wecom"

    @classmethod
    def all(cls) -> set[str]:
        return {cls.PASSWORD, cls.WECOM}


class AccessDenyReason:
    """企业微信成员被拒绝登录的原因（要能直接展示给人看）。"""

    NOT_FOUND = "not_found"  # 通讯录里没有该 userid（未同步或已删除）
    INACTIVE = "inactive"  # 员工状态不是在职
    NOT_AUTHORIZED = "not_authorized"  # 未被任何部门或个人授权
    DENIED_BY_DEPARTMENT = "denied_by_department"  # 被某个部门显式禁止
    DENIED_BY_EMPLOYEE = "denied_by_employee"  # 被个人设置明确禁止
    LOGIN_DISABLED = "login_disabled"  # 管理员关闭了企业微信登录
    NOT_MEMBER = "not_member"  # 扫码的人不是本企业成员
    INVALID_STATE = "invalid_state"  # state 缺失/被篡改/过期
    FAILED = "failed"  # 换取身份时上游失败
