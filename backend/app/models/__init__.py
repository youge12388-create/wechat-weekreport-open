"""数据模型。

需求第 2.2 节：底层不得与「周报」强绑定。
因此这里只定义 **通用实体**，不含任何企业微信专有字段：
企微字段一律留在 RawRecord.raw_payload 中，
需要用于查询的少量派生字段（如 template_name）才提升到业务表。
"""

from app.models.ai_analysis import AIAnalysis
from app.models.ai_embedding import AIEmbedding
from app.models.agent_memory import AgentMemory
from app.models.agent_feedback import AgentFeedback
from app.models.analysis_queue import AnalysisQueue
from app.models.blocker import Blocker
from app.models.blocker_summary_queue import BlockerSummaryQueue
from app.models.dashboard_feedback import DashboardFeedback
from app.models.department import Department
from app.models.employee import Employee
from app.models.enums import (
    AIAnalysisStatus,
    ActionNotifyStatus,
    AnalysisType,
    BlockerStatus,
    AnalysisQueueStatus,
    DataSource,
    EmployeeStatus,
    EntityType,
    ManagementActionAuthor,
    ManagementActionPriority,
    ManagementActionStatus,
    NotificationAudience,
    OperatingSummaryType,
    ProcessStatus,
    ReminderChannel,
    ReminderRunStatus,
    ReminderStatus,
    ReportType,
    SyncJobStatus,
    SyncType,
    TaskStatus,
)
from app.models.raw_record import RawRecord
from app.models.reminder_log import ReminderLog
from app.models.reminder_run import ReminderRun
from app.models.management_action import (
    ManagementAction,
    ManagementActionEvent,
    ManagementActionMessage,
)
from app.models.report_item import ReportItem
from app.models.report_score import ReportScore
from app.models.report_translation import ReportTranslation
from app.models.period_summary_queue import PeriodSummaryQueue
from app.models.operating_summary_queue import COMPANY_SCOPE_ID, OperatingSummaryQueue
from app.models.system_setting import SystemSetting
from app.models.sync_job import SyncJob
from app.models.task import Task
from app.models.task_report_link import TaskReportLink
from app.models.weekly_report import WeeklyReport
from app.models.work_item import WorkItem

__all__ = [
    "AIAnalysis",
    "AIEmbedding",
    "AgentMemory",
    "AgentFeedback",
    "AIAnalysisStatus",
    "ActionNotifyStatus",
    "AnalysisQueue",
    "AnalysisQueueStatus",
    "AnalysisType",
    "Blocker",
    "BlockerStatus",
    "BlockerSummaryQueue",
    "DashboardFeedback",
    "ManagementAction",
    "ManagementActionAuthor",
    "ManagementActionEvent",
    "ManagementActionMessage",
    "ManagementActionPriority",
    "ManagementActionStatus",
    "NotificationAudience",
    "OperatingSummaryQueue",
    "OperatingSummaryType",
    "COMPANY_SCOPE_ID",
    "DataSource",
    "Department",
    "Employee",
    "EmployeeStatus",
    "EntityType",
    "ProcessStatus",
    "RawRecord",
    "ReminderChannel",
    "ReminderLog",
    "ReminderRun",
    "ReminderRunStatus",
    "ReminderStatus",
    "ReportItem",
    "ReportScore",
    "ReportTranslation",
    "PeriodSummaryQueue",
    "ReportType",
    "SyncJob",
    "SyncJobStatus",
    "SyncType",
    "SystemSetting",
    "Task",
    "TaskReportLink",
    "TaskStatus",
    "WeeklyReport",
    "WorkItem",
]
