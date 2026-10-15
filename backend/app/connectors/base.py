"""Connector 抽象层（需求第 2.2 节 / 第 24 节第 7 条）。

职责边界（严格遵守）：
    Connector 只负责「把外部系统的数据原样取回来」，
    不做业务映射、不写数据库、不调用 AI。

    Data Source -> Connector -> RawPayload -> Processor -> 业务表

新增数据源（OA / 审批 / 微盘 / 财务 …）时：
    1. 在 app/connectors/<name>/ 下实现对应 Protocol；
    2. 增加一个 Processor 把 RawPayload 映射为业务实体；
    现有周报逻辑不需要任何修改。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from app.core.time_utils import utcnow


class ConnectorError(Exception):
    """Connector 基础异常。"""

    retryable = False


class ConnectorAuthError(ConnectorError):
    """凭据无效或过期（可通过刷新 token 解决）。"""

    retryable = True


class ConnectorPermissionError(ConnectorError):
    """应用未被授权访问该数据，重试无效，需要管理员在后台配置。

    典型：企业微信 301065 无汇报应用数据拉取权限。
    """

    retryable = False


class ConnectorRateLimitError(ConnectorError):
    """触发接口频率限制。"""

    retryable = True


class ConnectorUpstreamError(ConnectorError):
    """上游系统繁忙或 5xx。"""

    retryable = True


class ConnectorResponseError(ConnectorError):
    """参数错误 / 返回结构异常，重试无效。"""

    retryable = False


@dataclass(slots=True)
class RawPayload:
    """Connector 的统一输出。

    payload 保持外部系统返回的 **原始结构**，不做任何字段裁剪，
    保证后续可回溯（需求第 24 节第 10 条：原始数据不可被覆盖）。
    """

    source: str
    source_type: str
    external_id: str
    payload: dict
    fetched_at: datetime = field(default_factory=utcnow)


class OrganizationSource(ABC):
    """能提供组织架构的数据源。"""

    @abstractmethod
    def list_departments(self) -> list[RawPayload]:
        """返回部门原始列表。"""

    @abstractmethod
    def list_employees(self) -> list[RawPayload]:
        """返回员工原始列表。"""


class ReportSource(ABC):
    """能提供周期性汇报（周报/日报/月报）的数据源。"""

    @abstractmethod
    def iter_report_ids(
        self, start: datetime, end: datetime, **kwargs: object
    ) -> Iterator[str]:
        """分页产出汇报记录 ID。

        这里只翻页不取详情：列表接口的翻页游标一旦中断无法续传，
        因此列表失败属于 **批次级** 失败；详情失败属于 **单条级** 失败，
        由 Service 逐条 try/except，保证单条脏数据不影响整批（需求第 6.4 节）。
        """

    @abstractmethod
    def get_report_detail(self, external_id: str) -> RawPayload:
        """按 ID 获取单条汇报原始详情。"""


class BaseConnector(ABC):
    @property
    @abstractmethod
    def source(self) -> str:
        """数据源标识，写入 RawRecord.source。"""

    @abstractmethod
    def health_check(self) -> dict:
        """连通性与权限自检，供探针脚本使用。"""
