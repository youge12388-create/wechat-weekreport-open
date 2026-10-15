"""管理跟进的双向沟通：落库、会话、已读与企微通知。

设计要点（与需求确认一致，改动前先读这里）：

1. **站内会话是真源，企微只是通知**。消息先落库、再尝试投递；投递失败不回滚消息，
   只在消息上留下 `notify_status` 与具体原因。员工的回复也从站内页面发出 ——
   本模块**不接受**企微的消息回调（那需要另配公网回调端点与 AES 加解密，
   且可见范围没配好的员工会静默收不到）。
2. **投递结果逐人核对**（沿用第 25 条纪律）：企微在「部分收件人不在可见范围」时
   照样返回 errcode=0，只把这些人列进 invaliduser。只看 errcode 会让
   「已通知」与「其实没人收到」在页面上长得一模一样。
3. **冷却按 (事项, 受众) 计时**：同一件事 10 分钟内不重复打扰同一侧，
   防止连发刷屏；命中冷却返回 skipped 并写清原因，不静默吞掉。
4. **通知指向哪里**：走登录入口的 `next` 参数直达事项页 ——
   收件人点卡片时若还没登录，先完成企业微信登录再落到那一页。
   未授权者会看到已有的「原因 + 找谁处理」页面，不会被丢回登录页空转。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.connectors import base as cb
from app.connectors.wecom.client import WeComClient
from app.core.config import Settings, get_settings
from app.core.time_utils import utcnow
from app.models.employee import Employee
from app.models.enums import (
    ActionNotifyStatus,
    ManagementActionAuthor,
    NotificationAudience,
)
from app.models.management_action import ManagementAction, ManagementActionMessage
from app.services import access_service
from app.services.runtime_config_service import runtime_settings

Reader = Literal["admin", "owner"]

# 企微 textcard 的字段上限（与催交卡片一致，超限由企微截断可能切坏高亮标签）
CARD_TITLE_LIMIT = 128
CARD_DESCRIPTION_LIMIT = 512
# 卡片里引用的消息预览长度：卡片是「去处理」的入口，不是会话正文的副本
MESSAGE_PREVIEW_CHARS = 140
BODY_MAX_CHARS = 5000
# 同一事项、同一受众的通知冷却
NOTIFY_COOLDOWN_MINUTES = 10
# 企微只允许撤回 24 小时内的消息（官方文档 /document/path/94867）。
# 定义在模块常量区而不是函数附近：`_within_recall_window` 与 `recall_message`
# 都要用它，放在下面会变成前向引用。
RECALL_WINDOW_HOURS = 24
ACTION_PATH = "/actions"
MY_ACTION_PATH = "/my-actions"


class NotifyResult:
    """一次投递的结构化结果，直接透出给前端与调用方。"""

    def __init__(
        self,
        *,
        status: str,
        audience: str,
        targets: list[dict[str, Any]],
        delivered: list[dict[str, Any]],
        undelivered: list[dict[str, Any]],
        error: str | None = None,
        skipped_reason: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.audience = audience
        self.targets = targets
        self.delivered = delivered
        self.undelivered = undelivered
        self.error = error
        self.skipped_reason = skipped_reason
        self.raw = raw or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "audience": self.audience,
            "target_count": len(self.targets),
            "delivered_count": len(self.delivered),
            "delivered": self.delivered,
            "undelivered": self.undelivered,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
            "raw": self.raw,
        }


# --------------------------------------------------------------------------- #
# 落库与会话
# --------------------------------------------------------------------------- #
def append_message(
    session: Session,
    action: ManagementAction,
    *,
    author_role: str,
    body: str,
    author_employee_id: int | None = None,
    author_name: str | None = None,
) -> ManagementActionMessage:
    """写入一条消息。

    发言人的消息**不标记自己那侧已读**：只标记对方 —— 否则「自己发完就看到
    未读清零」会让老板以为员工已经看过。
    """
    text = (body or "").strip()
    if not text:
        raise ValueError("消息内容不能为空")
    if len(text) > BODY_MAX_CHARS:
        raise ValueError(f"消息过长（上限 {BODY_MAX_CHARS} 字）")
    if author_role not in ManagementActionAuthor.all():
        raise ValueError("发言人身份只支持 admin / owner")

    now = utcnow()
    message = ManagementActionMessage(
        action=action,
        author_role=author_role,
        author_employee_id=author_employee_id,
        author_name=author_name,
        body=text,
        notify_status=ActionNotifyStatus.NOT_ATTEMPTED,
        # 自己那侧视为已读；对方那侧留空等对方打开。
        owner_read_at=now if author_role == ManagementActionAuthor.OWNER else None,
        admin_read_at=now if author_role == ManagementActionAuthor.ADMIN else None,
    )
    session.add(message)
    session.flush()
    return message


def message_view(message: ManagementActionMessage, view: str = "admin") -> dict[str, Any]:
    """把一条消息序列化成前端可用的结构。

    **已撤回消息的正文只对管理侧保留**：管理侧要留痕（谁在什么时候发了什么、
    又为什么撤回），员工侧则必须屏蔽 —— 撤回的意义就是「这句话不该被他看到」。
    如果员工还能在站内读到原文，撤回就只是把卡片收回去、内容照样泄露，
    那等于没撤。这里替换成占位说明而不是删字段：前端要能显示「这条已撤回」，
    否则员工会以为消息凭空消失。
    """
    recalled = message.recalled_at is not None
    body = message.body
    if recalled and view != "admin":
        body = "（对方撤回了这条通知）"
    item: dict[str, Any] = {
        "id": message.id,
        "action_id": message.action_id,
        "author_role": message.author_role,
        "author_name": message.author_name,
        "body": body,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        # 撤回状态对**双方**都要可见：员工看到「这条已被撤回」才知道
        # 之前那张卡片为什么消失了，否则会以为是自己误删。
        "recalled": recalled,
        "recalled_at": message.recalled_at.isoformat() if message.recalled_at else None,
    }
    # 投递结果只给管理侧看：员工看到「你的回复投递失败」只会困惑，
    # 而真正需要处理企微可见范围的是管理员。
    if view == "admin":
        item["notify"] = {
            "status": message.notify_status,
            "audience": message.notify_audience,
            "error": message.notify_error,
            "attempts": message.notify_attempts,
            "notified_at": message.notified_at.isoformat() if message.notified_at else None,
            # 撤回能力直接由后端判定：前端不重算 24 小时窗口、也不猜有没有 msgid，
            # 否则界面会给出「可以撤回」而实际撤不掉的按钮。
            "can_recall": (
                message.recalled_at is None
                and bool(message.notify_msgids)
                and message.notify_status == ActionNotifyStatus.SENT
                and _within_recall_window(message)
            ),
            "recall_error": message.recall_error,
        }
    return item


def _within_recall_window(message: ManagementActionMessage) -> bool:
    """是否还在企微允许的撤回时限内（24 小时）。"""
    reference = message.notified_at or message.created_at
    if reference is None:
        return False
    return utcnow() - reference <= timedelta(hours=RECALL_WINDOW_HOURS)


def thread(
    session: Session,
    action: ManagementAction,
    *,
    view: str = "admin",
) -> list[dict[str, Any]]:
    """按时间列出会话消息。

    只返回这一件事的消息 —— 员工视角下「别的跟进事项」本身就不该可见，
    因此这里没有也不该有跨事项的会话流。
    """
    messages = session.scalars(
        select(ManagementActionMessage)
        .where(ManagementActionMessage.action_id == action.id)
        .order_by(ManagementActionMessage.created_at, ManagementActionMessage.id)
    ).all()
    return [message_view(message, view) for message in messages]

def last_message(session: Session, action_id: int) -> dict[str, Any] | None:
    return last_messages(session, [action_id]).get(action_id)


def last_messages(session: Session, action_ids: list[int]) -> dict[int, dict[str, Any]]:
    """按事项批量取「最后一条消息」。

    列表页要给每条事项显示最新摘要，逐个调用 `last_message` 就是 N 次查询
    （`serialize_action` 一次、列表 N 次）。这里一次取回全部，让列表的查询数
    与事项数无关 —— 与第 22 条要求的口径一致：查询次数不该随业务行数线性增长。
    """
    if not action_ids:
        return {}
    rows = session.scalars(
        select(ManagementActionMessage)
        .where(ManagementActionMessage.action_id.in_(action_ids))
        .order_by(
            ManagementActionMessage.action_id,
            ManagementActionMessage.created_at.desc(),
            ManagementActionMessage.id.desc(),
        )
    ).all()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        # 已按 (action_id, 时间倒序) 排序，每个 action 的第一条就是最新那条
        if row.action_id in result:
            continue
        body = row.body or ""
        result[row.action_id] = {
            "id": row.id,
            "author_role": row.author_role,
            "author_name": row.author_name,
            "excerpt": body if len(body) <= MESSAGE_PREVIEW_CHARS else body[:MESSAGE_PREVIEW_CHARS] + "…",
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    return result


def message_counts(session: Session, action_ids: list[int]) -> dict[int, int]:
    if not action_ids:
        return {}
    rows = session.execute(
        select(
            ManagementActionMessage.action_id,
            func.count(ManagementActionMessage.id),
        )
        .where(ManagementActionMessage.action_id.in_(action_ids))
        .group_by(ManagementActionMessage.action_id)
    )
    return {action_id: int(count) for action_id, count in rows}


def unread_counts(
    session: Session,
    action_ids: list[int],
    *,
    reader: Reader,
) -> dict[int, int]:
    """未读 = 「对方发的」且「我这侧还没读」的消息数。

    把「自己发的」排除在外是必要的：否则老板发完消息就会看到自己那侧显示未读，
    与「等对方回复」的语义正好相反。
    """
    if not action_ids:
        return {}
    other = (
        ManagementActionAuthor.OWNER
        if reader == "admin"
        else ManagementActionAuthor.ADMIN
    )
    read_column = (
        ManagementActionMessage.admin_read_at
        if reader == "admin"
        else ManagementActionMessage.owner_read_at
    )
    rows = session.execute(
        select(
            ManagementActionMessage.action_id,
            func.count(ManagementActionMessage.id),
        )
        .where(
            ManagementActionMessage.action_id.in_(action_ids),
            ManagementActionMessage.author_role == other,
            read_column.is_(None),
        )
        .group_by(ManagementActionMessage.action_id)
    )
    return {action_id: int(count) for action_id, count in rows}


def unread_action_replies(session: Session) -> list[dict[str, Any]]:
    """哪些跟进事项有「负责人发的、管理侧还没读」的消息。

    给消息中心用。返回按事项聚合的结果（含条数、最近时间、标题），
    让上层能说清「哪几件事有人在等我」，而不是只给一个总数。
    """
    rows = session.execute(
        select(
            ManagementAction.id,
            ManagementAction.title,
            ManagementAction.status,
            func.count(ManagementActionMessage.id),
            func.max(ManagementActionMessage.created_at),
        )
        .select_from(ManagementActionMessage)
        .join(ManagementAction, ManagementAction.id == ManagementActionMessage.action_id)
        .where(
            ManagementActionMessage.author_role == ManagementActionAuthor.OWNER,
            ManagementActionMessage.admin_read_at.is_(None),
            # 已结束的事项不再提示：它们不需要老板再回话，
            # 让它们挂在消息中心只会让红点消不掉。
            ManagementAction.status.notin_(["RESOLVED", "CANCELLED"]),
        )
        .group_by(ManagementAction.id, ManagementAction.title, ManagementAction.status)
        .order_by(func.max(ManagementActionMessage.created_at).desc())
    )
    return [
        {
            "id": action_id,
            "title": title,
            "status": status,
            "count": int(count),
            "last_at": last_at.isoformat() if isinstance(last_at, datetime) else None,
        }
        for action_id, title, status, count, last_at in rows
    ]


def total_unread(
    session: Session, *, reader: Reader, employee_id: int | None = None
) -> int:
    """全局未读数（消息中心与侧栏红点用）。

    `reader="owner"` **必须**传 `employee_id`：负责人只能看到指派给自己的事项，
    不限定所有者就会把「别的同事有多少条未读」算进他的红点里 —— 数字对不上，
    点进去还找不到那些事项。
    """
    other = (
        ManagementActionAuthor.OWNER
        if reader == "admin"
        else ManagementActionAuthor.ADMIN
    )
    read_column = (
        ManagementActionMessage.admin_read_at
        if reader == "admin"
        else ManagementActionMessage.owner_read_at
    )
    query = (
        select(func.count(ManagementActionMessage.id))
        .select_from(ManagementActionMessage)
        .join(ManagementAction, ManagementAction.id == ManagementActionMessage.action_id)
        .where(
            ManagementActionMessage.author_role == other,
            read_column.is_(None),
            # 已结束的事项不再计入：它们不需要任何人再回话。
            ManagementAction.status.notin_(["RESOLVED", "CANCELLED"]),
        )
    )
    if reader == "owner":
        if employee_id is None:
            return 0
        query = query.where(ManagementAction.owner_employee_id == employee_id)
    return int(session.scalar(query) or 0)


def mark_read(session: Session, action: ManagementAction, *, reader: Reader) -> int:
    """把对方发来的消息标记为已读，返回更新条数。

    **不放进 GET 里**：列表/详情页会轮询，若 GET 有副作用，
    未读会在用户根本没看内容的情况下被清空。
    """
    now = utcnow()
    other = (
        ManagementActionAuthor.OWNER
        if reader == "admin"
        else ManagementActionAuthor.ADMIN
    )
    column_name = "admin_read_at" if reader == "admin" else "owner_read_at"
    messages = session.scalars(
        select(ManagementActionMessage).where(
            ManagementActionMessage.action_id == action.id,
            ManagementActionMessage.author_role == other,
            getattr(ManagementActionMessage, column_name).is_(None),
        )
    ).all()
    for message in messages:
        setattr(message, column_name, now)
    if messages:
        session.flush()
    return len(messages)


def post_message(
    session: Session,
    action: ManagementAction,
    *,
    author_role: str,
    body: str,
    author_employee_id: int | None,
    author_name: str | None,
    notify_peer: bool = True,
    client_factory: Any = None,
) -> tuple[ManagementActionMessage, NotifyResult | None]:
    """落库一条消息，并按需通知**对方**。

    两个关键约定：

    1. **消息落库是真源，投递是辅助**。投递失败不回滚消息 —— 否则
       「企微临时报错」会让员工刚写的内容直接消失。
    2. **只通知对方**：管理侧发的消息推给负责人，负责人发的推给管理侧。
       推给自己那一侧没有意义，还会让操作者收到自己刚写的文字。

    参数名刻意叫 `notify_peer` 而不是 `notify`：后者会遮蔽本模块的
    `notify()` 函数，`notify(...)` 就变成「调用一个布尔值」。
    """
    author = author_role
    if author not in ManagementActionAuthor.all():
        raise ValueError("发言人身份只支持 admin / owner")
    message = append_message(
        session,
        action,
        author_role=author,
        body=body,
        author_employee_id=author_employee_id,
        author_name=author_name,
    )
    # 先 flush 拿到 message.id（冷却判断要排除自己这条）
    session.flush()
    if not notify_peer:
        return message, None

    audience = (
        NotificationAudience.OWNER
        if author == ManagementActionAuthor.ADMIN
        else NotificationAudience.ADMIN
    )
    result = notify(
        session,
        action,
        message,
        audience=audience,
        operator_employee_id=author_employee_id,
        client_factory=client_factory,
    )
    return message, result


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
def _notification_base_url(settings: Settings) -> str:
    """通知链接的前缀。

    优先 `wecom_login_frontend_url`（生产显式配置的前端域名），
    否则回退 `remind_card_url`（本地开发指向 3000 端口的前端）。
    """
    configured = (settings.wecom_login_frontend_url or "").strip()
    if configured:
        return configured.rstrip("/")
    return (settings.remind_card_url or "").rstrip("/")


def build_notify_url(settings: Settings, action_id: int, *, audience: str) -> str:
    """直达事项页的链接。

    走 `/api/auth/wecom/start` 而不是直接给页面地址：收件人点开时通常还没登录，
    直接给页面会先被 401 弹回登录页，回来还得自己再找一遍这件事。
    `next` 由后端 `_safe_next` 兜底（只接受站内相对路径），这里不做二次校验。
    """
    base = _notification_base_url(settings)
    target = (
        f"{MY_ACTION_PATH}/{action_id}"
        if audience == NotificationAudience.OWNER
        else f"{ACTION_PATH}/{action_id}"
    )
    return f"{base}/api/auth/wecom/start?mode=auto&next={target}"


def _is_loopback_url(value: str) -> bool:
    """通知地址是否只在本机可达。

    这是「卡片发出去了、对方却点不开」的根因：`remind_card_url` 的开发默认值
    是 `http://127.0.0.1:3000`，而收件人是在**他自己的手机**上点这张卡片 ——
    127.0.0.1 指向那台手机，永远打不开。就绪检查不拦这一条的话，界面会显示
    「可以通知」，实际是一条死链。
    """
    from urllib.parse import urlparse

    host = (urlparse(value).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"}


def notify_readiness(
    session: Session, action: ManagementAction, *, settings: Settings | None = None
) -> dict[str, Any]:
    """投递前检查负责人这侧是否具备接收条件。

    每条原因都带 `severity` 与可执行的下一步：

    - `blocking`：这条不解决，对方根本收不到或用不了（没 userid、未授权、
      卡片链接指向本机等）。
    - `warning`：能投递，但效果会打折（例如未配额外的管理员接收人）。

    条目里带 `severity` 而不是全塞进一个布尔，是因为「收不到」与「收到但点不开」
    要采取的动作完全不同 —— 前者去调企微可见范围，后者去配公网地址。
    前端据此分别用红/琥珀展示，不再笼统写「对方收不到通知」。
    """
    settings = settings or runtime_settings(session)
    reasons: list[dict[str, str]] = []

    def add(code: str, message: str, severity: str = "blocking") -> None:
        reasons.append({"code": code, "message": message, "severity": severity})

    owner = session.get(Employee, action.owner_employee_id) if action.owner_employee_id else None
    if owner is None:
        add("no_owner", "该事项还没有指定负责人，无法通知。")
    elif not (owner.external_id or "").strip():
        add("no_userid", f"{owner.name} 没有企业微信账号（userid），无法投递。")

    if not settings.wecom_login_enabled:
        # warning 而不是 blocking：卡片**能送到**（message/send 不依赖登录开关），
        # 坏的是「点开之后进不来」。混为一谈会让界面显示「对方收不到通知」，
        # 而他其实收到了 —— 这正是本模块要消灭的误导。
        add(
            "login_disabled",
            "未开启企业微信登录，卡片能送到，但收件人点开后无法进入系统。去「系统设置 → 登录与授权」开启。",
            severity="warning",
        )
    base = _notification_base_url(settings)
    if not base:
        add(
            "no_url",
            "未配置通知地址，卡片链接会指不到前端。请配置 WECOM_LOGIN_FRONTEND_URL 或 REMIND_CARD_URL。",
        )
    elif _is_loopback_url(base):
        # 本地开发最常见的坑，也最容易被误读成「功能坏了」：卡片确实送到了，
        # 坏的是链接 —— 收件人在自己手机上点 127.0.0.1，打开的是他自己的手机。
        add(
            "unreachable_url",
            f"通知地址是本机地址（{base}）：卡片能送到，但收件人在自己手机上点开会打不开、"
            "也就无法回复。要让对方点进来，请把 WECOM_LOGIN_FRONTEND_URL 配成他手机能访问的"
            "公网地址，并在企业微信后台把它加进该应用的「可信域名」。",
            severity="warning",
        )
    if owner is not None:
        decision = access_service.decide_access(session, owner)
        if not decision.allowed:
            add(
                "not_authorized",
                f"{owner.name} 尚未获得登录授权（{decision.detail or decision.reason}），"
                "卡片能送到，但他点开后进不来、无法回复。去「系统设置 → 登录与授权」按部门开启。",
                severity="warning",
            )

    # can_notify 只看**阻断级**原因：链接不可达这类问题不影响「卡片能不能送到」，
    # 把它算进去会让人以为消息发不出去，从而不敢发 —— 而实际对方是能收到卡片的。
    blocking = [item for item in reasons if item.get("severity") != "warning"]
    return {
        "can_notify": not blocking,
        "reasons": reasons,
        "blocking": blocking,
    }


def resolve_notify_targets(
    session: Session,
    action: ManagementAction,
    *,
    audience: str,
    exclude_employee_id: int | None = None,
    settings: Settings | None = None,
) -> tuple[list[Employee], list[dict[str, Any]]]:
    """算出这次要投给谁，以及谁被跳过、为什么。

    owner 侧：负责人本人。
    admin 侧：**本会话里出现过的企微管理员** + 设置页配置的接收人。
    刻意不做「把所有 admin 都推一遍」—— 那会按组织规模放大打扰；
    谁参与过这件事，谁才收它的回复。
    """
    settings = settings or runtime_settings(session)
    targets: list[Employee] = []
    skipped: list[dict[str, Any]] = []

    if audience == NotificationAudience.OWNER:
        owner = (
            session.get(Employee, action.owner_employee_id)
            if action.owner_employee_id
            else None
        )
        if owner is None:
            return [], [{"id": None, "name": None, "reason": "该事项没有负责人"}]
        if not (owner.external_id or "").strip():
            return [], [{"id": owner.id, "name": owner.name, "reason": "没有企业微信账号（userid）"}]
        return [owner], []

    if audience != NotificationAudience.ADMIN:
        raise ValueError("audience 只支持 owner / admin")

    candidates: list[Employee] = []
    seen: set[int] = set()

    # ① 参与过本会话的企微管理员
    participant_ids = session.scalars(
        select(ManagementActionMessage.author_employee_id)
        .where(
            ManagementActionMessage.action_id == action.id,
            ManagementActionMessage.author_role == ManagementActionAuthor.ADMIN,
            ManagementActionMessage.author_employee_id.is_not(None),
        )
        .distinct()
    ).all()
    for employee in (
        session.get(Employee, employee_id) for employee_id in participant_ids
    ):
        if employee is None or employee.id in seen:
            continue
        seen.add(employee.id)
        candidates.append(employee)

    # ② 设置页显式配置的接收人（访问密码登录的管理员没有企微身份，靠这里兜底）
    configured = [
        value.strip()
        for value in (settings.action_notify_admin_userids or "").split(",")
        if value.strip()
    ]
    for userid in configured:
        employee = access_service.find_employee_by_userid(session, userid)
        if employee is None:
            skipped.append(
                {
                    "id": None,
                    "name": userid,
                    "reason": "配置的通知接收人在通讯录里找不到，请核对 userid",
                }
            )
            continue
        if employee.id in seen:
            continue
        seen.add(employee.id)
        candidates.append(employee)

    for employee in candidates:
        if exclude_employee_id is not None and employee.id == exclude_employee_id:
            skipped.append(
                {"id": employee.id, "name": employee.name, "reason": "是本次操作者本人，无需通知"}
            )
            continue
        if not (employee.external_id or "").strip():
            skipped.append(
                {"id": employee.id, "name": employee.name, "reason": "没有企业微信账号（userid）"}
            )
            continue
        targets.append(employee)

    if not targets and not skipped:
        skipped.append(
            {
                "id": None,
                "name": None,
                "reason": "没有可通知的管理员：先由一位企微登录的管理员参与本会话，"
                "或在「系统设置 → 周报规则」配置通知接收人。",
            }
        )
    return targets, skipped


def build_message_card(
    action: ManagementAction,
    message: ManagementActionMessage,
    *,
    audience: str,
    settings: Settings,
) -> dict[str, str]:
    """构造企微文本卡片。

    正文刻意**不含内部备注、也不含第三人姓名**（沿用催交卡片的做法）：
    卡片是逐人投递的，写进去的每个字都会进对方的聊天窗口。
    """
    body = (message.body or "").strip()
    preview = body if len(body) <= MESSAGE_PREVIEW_CHARS else body[:MESSAGE_PREVIEW_CHARS] + "…"
    who = message.author_name or ("负责人" if audience == NotificationAudience.ADMIN else "管理者")
    title = f"事项沟通 · {action.title}"
    subject = "有新的回复，请查看" if audience == NotificationAudience.ADMIN else "负责人回复了事项"
    if audience == NotificationAudience.OWNER:
        subject = "有事项需要你跟进"
    lines = [
        f"{who}：{preview}",
        subject,
    ]
    if action.due_date:
        lines.insert(1, f"计划完成日：{action.due_date.isoformat()}")

    html = "".join(
        f'<div class="{"highlight" if index == len(lines) - 1 else "normal"}">'
        f"{escape(line, quote=False)}</div>"
        for index, line in enumerate(lines)
    )
    if len(html) > CARD_DESCRIPTION_LIMIT:
        shortened = html[: CARD_DESCRIPTION_LIMIT - 1]
        last_open = shortened.rfind("<div")
        last_close = shortened.rfind("</div>")
        if last_open > last_close:
            shortened = shortened[:last_open]
        html = shortened.rstrip() + "…"

    return {
        "title": title[:CARD_TITLE_LIMIT],
        "description": html,
        "url": build_notify_url(settings, action.id, audience=audience),
        "btn_text": "去看详情",
    }


def _cooldown_hit(
    session: Session,
    action: ManagementAction,
    *,
    audience: str,
    message_id: int,
) -> datetime | None:
    """同一事项、同一受众在冷却期内已成功通知过则返回那次时间。

    按 `ManagementActionMessage` 记录判断而不是内存状态：多进程 Worker 与
    API 进程共享同一个库，内存计数在重启后归零、在多实例下各算各的。
    """
    cutoff = utcnow() - timedelta(minutes=NOTIFY_COOLDOWN_MINUTES)
    row = session.scalars(
        select(ManagementActionMessage)
        .where(
            ManagementActionMessage.action_id == action.id,
            ManagementActionMessage.id != message_id,
            ManagementActionMessage.notify_audience == audience,
            ManagementActionMessage.notify_status == ActionNotifyStatus.SENT,
            ManagementActionMessage.notified_at.is_not(None),
            ManagementActionMessage.notified_at >= cutoff,
        )
        .order_by(ManagementActionMessage.notified_at.desc())
    ).first()
    return row.notified_at if row is not None else None


def notify(
    session: Session,
    action: ManagementAction,
    message: ManagementActionMessage,
    *,
    audience: str,
    operator_employee_id: int | None = None,
    force: bool = False,
    settings: Settings | None = None,
    client_factory: Any = None,
) -> NotifyResult:
    """投递一条消息到指定受众，并把结果写回该消息。

    `force=True` 跳过冷却（管理员在页面上点「重新通知」时用）——
    冷却挡的是自动连发，挡不住人明确的重发意图。
    """
    if audience not in NotificationAudience.all():
        raise ValueError("audience 只支持 owner / admin")
    # 已撤回的消息不允许再推送：那会让已经收回去的卡片重新出现在对方窗口里，
    # 而站内还标着「已撤回」—— 两个界面互相矛盾。
    if message.recalled_at is not None:
        reason = "这条消息已撤回，不能再推送。要补充说明请发一条新消息。"
        return NotifyResult(
            status=ActionNotifyStatus.SKIPPED,
            audience=audience,
            targets=[],
            delivered=[],
            undelivered=[],
            skipped_reason=reason,
        )
    settings = settings or runtime_settings(session)
    targets, skipped = resolve_notify_targets(
        session,
        action,
        audience=audience,
        exclude_employee_id=operator_employee_id,
        settings=settings,
    )

    if not force:
        hit = _cooldown_hit(session, action, audience=audience, message_id=message.id)
        if hit is not None:
            reason = (
                f"{NOTIFY_COOLDOWN_MINUTES} 分钟内已通知过，未重复发送"
                f"（上次 {hit.isoformat()}）"
            )
            message.notify_status = ActionNotifyStatus.SKIPPED
            message.notify_audience = audience
            message.notify_error = reason
            session.flush()
            return NotifyResult(
                status=ActionNotifyStatus.SKIPPED,
                audience=audience,
                targets=[{"id": item.id, "name": item.name} for item in targets],
                delivered=[],
                undelivered=[],
                skipped_reason=reason,
            )

    if not targets:
        reason = skipped[0]["reason"] if skipped else "没有可投递的收件人"
        message.notify_status = ActionNotifyStatus.SKIPPED
        message.notify_audience = audience
        message.notify_error = reason
        message.notify_attempts = (message.notify_attempts or 0) + 1
        message.notified_at = utcnow()
        session.flush()
        return NotifyResult(
            status=ActionNotifyStatus.SKIPPED,
            audience=audience,
            targets=[],
            delivered=[],
            undelivered=skipped,
            skipped_reason=reason,
        )

    card = build_message_card(action, message, audience=audience, settings=settings)
    factory = client_factory or WeComClient
    try:
        with factory() as client:
            result = client.send_text_card(
                user_ids=[item.external_id or "" for item in targets],
                title=card["title"],
                description=card["description"],
                url=card["url"],
                btn_text=card["btn_text"],
            )
    except cb.ConnectorError as exc:
        message.notify_status = ActionNotifyStatus.FAILED
        message.notify_audience = audience
        message.notify_attempts = (message.notify_attempts or 0) + 1
        message.notified_at = utcnow()
        message.notify_error = str(exc)[:500]
        session.flush()
        return NotifyResult(
            status=ActionNotifyStatus.FAILED,
            audience=audience,
            targets=[{"id": item.id, "name": item.name} for item in targets],
            delivered=[],
            undelivered=[],
            error=str(exc),
            raw={"error": str(exc)},
        )

    invalid = {
        value.strip().lower()
        for value in str(result.get("invaliduser") or "").split("|")
        if value.strip()
    }
    delivered: list[dict[str, Any]] = []
    undelivered: list[dict[str, Any]] = []
    for employee in targets:
        userid = (employee.external_id or "").lower()
        if userid and userid in invalid:
            undelivered.append(
                {
                    "id": employee.id,
                    "name": employee.name,
                    "reason": "企微未投递（不在应用可见范围或通讯录中）。"
                    "去企业微信管理后台 → 该自建应用 → 可见范围调整。",
                }
            )
            continue
        delivered.append({"id": employee.id, "name": employee.name})

    # errcode=0 只代表请求被受理；是否人人收到看 invaliduser（第 25 条）。
    if delivered:
        status = ActionNotifyStatus.SENT
    else:
        status = ActionNotifyStatus.UNDELIVERED
    message.notify_status = status
    message.notify_audience = audience
    message.notify_attempts = (message.notify_attempts or 0) + 1
    message.notified_at = utcnow()
    problem = [item["reason"] for item in undelivered]
    message.notify_error = "；".join(problem) if problem else None
    # 保存 msgid —— 撤回的唯一凭据，且只在发送当次的响应里才有。
    # 追加而不是覆盖：同一条消息可能被「重新推送」多次，聊天窗口里就有多张卡片，
    # 撤回时要把它们全部收回（见 recall_message）。
    new_msgid = str(result.get("msgid") or "").strip()
    if new_msgid:
        existing = list(message.notify_msgids or [])
        if new_msgid not in existing:
            existing.append(new_msgid)
        message.notify_msgids = existing
    session.flush()
    return NotifyResult(
        status=status,
        audience=audience,
        targets=[{"id": item.id, "name": item.name} for item in targets],
        delivered=delivered,
        undelivered=undelivered + skipped,
        raw={
            "errcode": result.get("errcode"),
            "errmsg": result.get("errmsg"),
            "invaliduser": result.get("invaliduser", ""),
            "msgid": new_msgid,
        },
    )


# --------------------------------------------------------------------------- #
# 撤回
# --------------------------------------------------------------------------- #
class RecallResult:
    """一次撤回尝试的结果。"""

    def __init__(
        self,
        *,
        recalled: bool,
        reason: str | None = None,
        msgid_count: int = 0,
        failed: list[dict[str, str]] | None = None,
    ) -> None:
        self.recalled = recalled
        self.reason = reason
        self.msgid_count = msgid_count
        self.failed = failed or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "recalled": self.recalled,
            "reason": self.reason,
            "msgid_count": self.msgid_count,
            "failed": self.failed,
        }


def recall_message(
    session: Session,
    message: ManagementActionMessage,
    *,
    recalled_by_role: str,
    client_factory: Any = None,
) -> RecallResult:
    """撤回这条消息已经推送出去的企业微信卡片。

    三个必须守住的点：

    1. **撤回站内与撤回企微是一件事的两半**。先调企微，成功后才标记本地
       `recalled_at`；否则会出现「站内显示已撤回、对方手机里那张卡片还在」——
       比不撤回更糟，因为用户以为自己撤回成功了。
    2. **撤回是终态**：已撤回的消息不允许再次撤回（幂等返回），也不该被
       「重新推送」复活 —— 那会让已经收回去的卡片重新出现在对方窗口里。
    3. **企微撤不回来的那部分要说清楚**：微信插件端的数据不支持撤回，
       24 小时之外也撤不回来。这些不是「程序出错」，必须如实标注。
    """
    if message.recalled_at is not None:
        return RecallResult(
            recalled=True,
            reason="这条消息此前已撤回。",
            msgid_count=len(message.notify_msgids or []),
        )

    msgids = [str(item) for item in (message.notify_msgids or []) if item]
    if not msgids:
        # 没有 msgid 意味着它从未成功投递过（或投递于本功能上线之前）——
        # 企微侧本来就没有可撤的东西，这不算失败。
        reason = (
            "这条消息没有推送记录，企微侧无需撤回。"
            if message.notify_status != ActionNotifyStatus.SENT
            else "这条消息在功能升级前推送过，没有保存撤回凭据，无法撤回。"
        )
        message.recall_error = None
        message.recalled_at = utcnow()
        message.recalled_by_role = recalled_by_role
        session.flush()
        return RecallResult(recalled=False, reason=reason, msgid_count=0)

    # 24 小时窗口：用最后一次投递时间判断（重发过的以最近一次为准）。
    reference = message.notified_at or message.created_at
    if reference is not None and utcnow() - reference > timedelta(hours=RECALL_WINDOW_HOURS):
        hours = (utcnow() - reference).total_seconds() / 3600
        reason = (
            f"超过企业微信的撤回时限（仅支持 {RECALL_WINDOW_HOURS} 小时内撤回，"
            f"这条已发出约 {hours:.0f} 小时）。卡片无法收回，请在会话里补一条更正说明。"
        )
        message.recall_error = reason
        session.flush()
        return RecallResult(recalled=False, reason=reason, msgid_count=len(msgids))

    factory = client_factory or WeComClient
    failed: list[dict[str, str]] = []
    ok_count = 0
    try:
        with factory() as client:
            for msgid in msgids:
                try:
                    client.recall_message(msgid)
                    ok_count += 1
                except cb.ConnectorError as exc:
                    failed.append({"msgid": msgid, "reason": str(exc)[:300]})
    except cb.ConnectorError as exc:
        reason = f"撤回请求未送达企业微信：{str(exc)[:300]}"
        message.recall_error = reason
        session.flush()
        return RecallResult(
            recalled=False, reason=reason, msgid_count=len(msgids), failed=failed
        )

    if failed and ok_count == 0:
        reason = "撤回失败：" + "；".join(item["reason"] for item in failed)
        message.recall_error = reason
        session.flush()
        return RecallResult(
            recalled=False, reason=reason, msgid_count=len(msgids), failed=failed
        )

    # 只要有一张卡片被收回，就标记为已撤回；部分失败的具体原因照样留在
    # recall_error 里，避免「显示撤回成功、其实还有一张没收回来」。
    message.recalled_at = utcnow()
    message.recalled_by_role = recalled_by_role
    message.recall_error = (
        "；".join(item["reason"] for item in failed) if failed else None
    )
    session.flush()
    reason = None
    if failed:
        reason = f"已撤回 {ok_count} 条，另有 {len(failed)} 条未成功：{message.recall_error}"
    return RecallResult(
        recalled=True, reason=reason, msgid_count=len(msgids), failed=failed
    )
