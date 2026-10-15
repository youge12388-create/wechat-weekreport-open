"""企业微信「汇报」正文解析。

核心风险点（务必读完再改）：

企业微信的汇报正文有 **两条互不相通的通道**，取决于员工用的是哪种模板：

  1. 自定义汇报表单  -> info.apply_data.contents[]
     结构随模板变化，控件类型有 17 种（Text / Textarea / Table / File …
     详见官方文档 93394 附录）。**不能写死字段路径**。

  2. 企微原生「汇报」模板 -> info.sys_journal_data
     内容为 **富文本**，且官方文档明确说明：
     「"汇报"模板是一个特殊模板，其表单不在 apply_data 中返回」。
     只解析 apply_data 会整片漏数据。

因此两条通道都必须处理，且都为空时必须显式告警，而不是静默写入空正文。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.time_utils import from_timestamp
from app.processors.text_cleaner import clean_text

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BLOCK = re.compile(r"<(br|/p|/div|/li|/tr)[^>]*>", re.IGNORECASE)

# 资源需求是企业当前周报模板中的业务字段。它可能出现在中文模板，也可能
# 出现在英文模板；解析时只认字段标签，不从整篇正文猜测，避免把普通句子中的
# ``resource`` 误当成卡点。
_RESOURCE_DEMAND_LABEL = re.compile(
    r"(?:^|\n)\s*(?:资源需求|resource\s+requirements?|resources?\s+(?:needed|required)|support\s+needed)\s*[:：]\s*",
    re.IGNORECASE,
)
_NEXT_FIELD_LABEL = re.compile(
    r"(?:^|\n)\s*(?:下周计划|下周工作计划|next\s+week(?:'s)?\s+plan|other\s+items?|其他事项|备注|comments?)\s*[:：]\s*",
    re.IGNORECASE,
)

# 企微表单中常见的「没有资源需求」写法。只有整段内容命中这些哨兵值时才
# 视为空；例如「暂无特殊资源需求，建议增加……」仍然保留为真实候选。
_EMPTY_RESOURCE_DEMANDS = {
    "无",
    "暂无",
    "没有",
    "无需求",
    "没有需求",
    "无特殊资源需求",
    "暂无资源需求",
    "暂无特殊资源需求",
    "无重大资源需求",
    "暂无重大资源需求",
    "没有重大资源需求",
    "无重大资源限制",
    "暂无重大资源限制",
    "没有重大资源限制",
    "/",
    "-",
    "—",
    "na",
    "none",
    "nil",
    "no",
    "n/a",
    "none at the moment",
    "not applicable",
    "not needed",
    "not required now",
    "no special resource requirements",
    "no special resource requirements at this stage",
    "no major resource constraints",
    "no major resource constraints at this stage",
    "no major resource requirements",
    "currently there are no major resource constraints",
    "currently there are no major resource constraints at this stage",
    "currently there are no major resource requirements",
    "no additional resources required at present",
    "no additional resources required at present. continue current work as planned.",
    "currently there are no special resource requirements. the existing work can continue as planned.",
    # 「目前暂无 / 目前没有」是「暂无 / 没有」的常见变体。它们必须走**精确匹配**
    # 而不是子串：子串会把「目前没有笔记本电脑，需要公司配备」这种真需求一起吃掉。
    "目前暂无",
    "目前没有",
    "目前暂无资源需求",
    "目前没有资源需求",
    # 字段名被原样回显（员工把栏目标题当内容交了）。同样是精确匹配 ——
    # 写成 "resource requirements: need a laptop" 时不是占位，应保留。
    "resource requirement",
    "resource requirements",
    "requirements",
    "requirement",
}


@dataclass(slots=True)
class ParsedJournal:
    """一份汇报解析后的结果。

    content_raw  : 从接口提取出的未清洗正文（RawRecord.raw_payload 才是真正的原始存档）
    content      : 清洗后的正文，供展示与 AI 使用
    """

    external_id: str
    content_raw: str = ""
    content: str = ""
    template_id: str | None = None
    template_name: str | None = None
    report_type: str = "other"
    report_time: datetime | None = None
    submitter_userid: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


def strip_html(text: str) -> str:
    """富文本转纯文本：块级标签先转换行，再剥离其余标签并反转义。"""
    if not text:
        return ""
    text = _HTML_BLOCK.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    return html.unescape(text).strip()


_EMPTY_DEMAND_PUNCTUATION = re.compile(r"[\s\.,，。!！?？;；:：、]+")
# 「Discuss later」「暂无需要公司协调解决的问题」「未明确」都不是需求 —— 是员工没填或
# 明确说不需要。大小写与标点归一后比较，避免 ``Discuss Later`` / ``discuss later.`` 漏过。
_EMPTY_DEMAND_PHRASES = (
    "discusslater",
    # 「以后再议」在库里写成过 We can discuss this later / discussed later 等多种形式。
    # 子串匹配下 ``discussthislater`` 能覆盖前者，单独列出是为了让规则意图可读 ——
    # 原来只写了 ``discusslater``，于是「连续 5 周未解决：资源需求：We can discuss
    # this later」占掉了首屏一个名额（实测）。
    "discussthislater",
    "todiscuss",
    "willdiscuss",
    "nocomment",
    "资源需求未明确",
    "需求未明确",
    "未明确",
)
# 明确表达「不需要支持」的句式。刻意要求与「暂/无/不需要」同现，
# 否则会把「暂缺设备，需要协调」这种真需求误判成空。
_EMPTY_DEMAND_NEGATIONS = (
    "暂不需要",
    "暂时不需要",
    "暂无需要",
    "暂无需",
    "暂无其他",
    "无其他",
    "不需要公司",
    "不需要额外",
    "暂无额外",
    "无额外需求",
    "暂无特殊",
    "无特殊需求",
    "无需公司",
    "无需协调",
    "无需额外",
    "无特别需求",
    "未提出需求",
    "noadditional",
    "nospecialrequirement",
    "noadditionalresource",
    "noneed",
    "nosupportneeded",
    # 「没有需求」的英文变体。与上面的中文表同一作用：它们都不是「公司在被什么拖慢」，
    # 而是员工在资源需求栏留了占位。实测漏网过 No resources required / None at this
    # moment / No major issues at the moment，故按同样粒度补齐。
    "noresourcesrequired",
    "noresourcerequired",
    "resourcesnotrequired",
    "noadditionalsupport",
    "nonerequired",
    "nonerequested",
    "nothingrequired",
    "nothingatthismoment",
    "noneatthismoment",
    "nomajorissues",
    "noissuesatthemoment",
    "noparticularrequirement",
    "notapplicable",
)

# 转折词：先否定后补充的句式里，后半句才是真需求。
# 「None, but it would be helpful to have an office space」若只按前半句判空，
# 就会把一条真实诉求整条丢掉。命中转折词时一律不判空 —— 宁可让一条「不需要」
# 留在结论区被人工忽略一次，也不能静默丢掉真需求。
_DEMAND_PIVOT_WORDS = (
    "but",
    "however",
    "although",
    "though",
    "但",
    "不过",
    "然而",
    "除了",
    "另外",
    "同时",
    # 「希望…」是诉求的表达，不是状态描述。实测「目前暂无特殊资源需求，希望后续能够
    # 持续获得院校资料、业务培训资料」的后半句是真需求，漏掉会把一条实际诉求整条丢弃。
    "希望",
    "恳请",
    "建议提供",
)


def is_empty_resource_demand(value: str | None) -> bool:
    """判断一段「资源需求」文本是否等于「没有需求」。

    与 :func:`extract_resource_demand` 共用同一份哨兵表：那边判断「填的是不是空」，
    这边判断「已经存下来的文本是不是空」。经营看板要靠它把
    「资源需求：Discuss later」「目前暂时不需要公司提供额外支持」这类记录排除出
    「公司在被什么拖慢」——否则卡点排序会被这类噪音占满。

    **这是所有「持续问题 / 长期卡点 / 瓶颈」的准入条件**，不是可有可无的清洗：
    库里能被确定性解析器稳定跨周关联的，恰恰只有资源需求类事项（AI 给的 task_key
    每次都不一样），所以「连续 N 周未解决」这个信号能捡到的几乎全是资源需求。
    判空漏一条，首屏就多一条占位废话（实测漏过 We can discuss this later）。
    """
    text = re.sub(r"\s+", " ", (value or "").strip()).casefold()
    if not text:
        return True
    # 事项标题会带「资源需求：」前缀，比较前先剥掉。
    text = re.sub(r"^资源需求\s*[:：]\s*", "", text)
    compact = _EMPTY_DEMAND_PUNCTUATION.sub("", text)
    if not compact:
        return True
    # 转折词保护放在否定词之前：「None, but it would be helpful to have an office
    # space」这类先否定后补充的句式，后半句才是真需求。
    if any(pivot in compact for pivot in _DEMAND_PIVOT_WORDS):
        return False
    if compact in {
        _EMPTY_DEMAND_PUNCTUATION.sub("", item.casefold())
        for item in _EMPTY_RESOURCE_DEMANDS
    }:
        return True
    return any(
        phrase in compact
        for phrase in (*_EMPTY_DEMAND_PHRASES, *_EMPTY_DEMAND_NEGATIONS)
    )


def extract_resource_demand(content: str | None) -> str:
    """从已解析正文中提取「资源需求」字段的值。

    资源需求是确定的表单字段，不依赖 AI 才能成为卡点候选。返回值不包含
    字段名；空值、``无``、``暂无``、``NA`` 等明确的无需求填写，以及
    ``Discuss later``、``暂无需要公司协调解决的问题`` 这类占位回答返回空串。
    判空统一走 :func:`is_empty_resource_demand` 的宽口径（同一份哨兵表 +
    拖延/否定短语 + 转折词保护）：卡点物化与经营看板必须用同一套标准，
    否则看板排掉的空话会原样漏进处理中心（实测「Discuss later」连续 11 周
    被重复填写后还触发了跨周升级，占掉待处理首位）。
    ``content`` 可能含富文本，因此先转纯文本再按下一个表单字段截断。
    """
    text = clean_text(strip_html(content or ""))
    if not text:
        return ""
    match = _RESOURCE_DEMAND_LABEL.search(text)
    if match is None:
        return ""

    value_start = match.end()
    next_field = _NEXT_FIELD_LABEL.search(text, value_start)
    value = text[value_start : next_field.start() if next_field else None]
    value = clean_text(value).strip(" \t\r\n:：")
    if not value:
        return ""
    return "" if is_empty_resource_demand(value) else value


def detect_report_type(template_name: str | None, settings: Settings | None = None) -> str:
    """按汇报表单名称推断汇报类型。

    关键字可配置（Settings.report_type_keywords），
    避免把分类规则硬编码进业务代码（需求第 9 节同款要求）。
    """
    settings = settings or get_settings()
    if not template_name:
        return settings.report_type_fallback
    for report_type, keywords in settings.report_type_keywords.items():
        for keyword in keywords:
            if keyword and keyword in template_name:
                return report_type
    return settings.report_type_fallback


def _title_text(title: Any) -> str:
    """控件名称可能是多语言数组：[{'text': '工作内容'}, ...]。"""
    if isinstance(title, list):
        parts = [
            item.get("text", "")
            for item in title
            if isinstance(item, dict) and item.get("text")
        ]
        return "".join(parts)
    if isinstance(title, str):
        return title
    return ""


def _render_control(control: dict, value: dict, attachments: list[dict]) -> str | None:
    """把单个控件渲染成「标题：值」文本。返回 None 表示该控件不产生文本。"""
    name = _title_text(control.get("title")).strip()
    control_type = (control.get("control") or "").lower()

    if control_type in {"text", "textarea"}:
        text = value.get("text")
        return _join(name, text)

    if control_type == "number":
        return _join(name, value.get("new_number"))

    if control_type == "money":
        return _join(name, value.get("new_money"))

    if control_type == "formula":
        formula = value.get("formula") or {}
        return _join(name, formula.get("value"))

    if control_type == "date":
        date_value = value.get("date") or {}
        ts = from_timestamp(date_value.get("s_timestamp"))
        return _join(name, ts.date().isoformat() if ts else None)

    if control_type == "daterange":
        begin = from_timestamp((value.get("date_range") or {}).get("new_begin"))
        end = from_timestamp((value.get("date_range") or {}).get("new_end"))
        if begin and end:
            return _join(name, f"{begin.date().isoformat()} ~ {end.date().isoformat()}")
        return _join(name, None)

    if control_type == "selector":
        selector = value.get("selector") or {}
        options = selector.get("options") or []
        labels: list[str] = []
        for option in options:
            for item in option.get("value") or []:
                if isinstance(item, dict) and item.get("text"):
                    labels.append(item["text"])
        return _join(name, "、".join(labels))

    if control_type == "contact":
        members = [m.get("userid") for m in value.get("members") or [] if m.get("userid")]
        if members:
            return _join(name, "、".join(members))
        departments = [
            d.get("openapi_id")
            for d in value.get("departments") or []
            if d.get("openapi_id")
        ]
        return _join(name, "、".join(departments))

    if control_type == "location":
        location = value.get("location") or {}
        return _join(name, location.get("title") or location.get("address"))

    if control_type == "file":
        for file_item in value.get("files") or []:
            if file_item.get("file_id"):
                attachments.append({"type": "file", "file_id": file_item["file_id"]})
        return _join(name, f"[附件 {len(value.get('files') or [])} 个]")

    if control_type == "wedrivefile":
        for file_item in value.get("wedrive_files") or []:
            if file_item.get("fileid"):
                attachments.append(
                    {"type": "wedrive", "fileid": file_item.get("fileid")}
                )
        return None

    if control_type == "doc":
        for doc in value.get("docs") or []:
            if doc.get("doc_url"):
                attachments.append({"type": "doc", "doc_url": doc["doc_url"]})
        return None

    if control_type == "table":
        # 明细：逐行渲染子控件
        rows: list[str] = []
        for child in value.get("children") or []:
            for row in child.get("list") or []:
                cells: list[str] = []
                for sub in row:
                    rendered = _render_control(
                        sub, sub.get("value") or {}, attachments
                    )
                    if rendered:
                        cells.append(rendered)
                if cells:
                    rows.append("；".join(cells))
        if not rows:
            return None
        header = f"{name}：" if name else ""
        return header + "\n" + "\n".join(rows)

    if control_type == "tips":
        return None

    # 未识别控件：保留原始值，避免静默丢数据
    for key in ("text", "new_number", "new_money"):
        if value.get(key):
            return _join(name, value[key])
    return None


def _join(name: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{name}：{value}" if name else str(value)


def render_apply_data(apply_data: dict | None, attachments: list[dict]) -> str:
    """把 apply_data 渲染为可读文本。结构随模板变化，逐控件容错处理。"""
    if not apply_data:
        return ""
    lines: list[str] = []
    for control in apply_data.get("contents") or []:
        if not isinstance(control, dict):
            continue
        try:
            rendered = _render_control(
                control, control.get("value") or {}, attachments
            )
        except Exception:  # noqa: BLE001  单控件异常不影响整份周报
            rendered = None
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def parse_journal_info(
    info: dict, external_id: str, settings: Settings | None = None
) -> ParsedJournal:
    """解析 get_record_detail 返回的 info 对象。"""
    settings = settings or get_settings()
    attachments: list[dict] = []
    warnings: list[str] = []

    template_name = info.get("template_name")
    template_id = info.get("template_id")
    report_time = from_timestamp(info.get("report_time"))
    submitter = info.get("submitter") or {}
    submitter_userid = submitter.get("userid") if isinstance(submitter, dict) else None

    parsed = ParsedJournal(
        external_id=external_id,
        template_id=template_id,
        template_name=template_name,
        report_type=detect_report_type(template_name, settings),
        report_time=report_time,
        submitter_userid=submitter_userid,
    )

    # ---- 通道一：原生汇报模板的富文本 ----
    sys_raw = (info.get("sys_journal_data") or "").strip()
    # ---- 通道二：自定义模板的表单数据 ----
    apply_raw = render_apply_data(info.get("apply_data"), attachments)

    parts = [part for part in (sys_raw, apply_raw) if part]
    parsed.content_raw = "\n\n".join(parts)

    cleaned_parts = [strip_html(part) for part in parts]
    parsed.content = clean_text("\n\n".join(cleaned_parts))
    parsed.attachments = attachments

    if not sys_raw and not apply_raw:
        warnings.append("empty_content: 两条正文通道均为空，需人工核对汇报表单配置")
    if report_time is None:
        warnings.append("missing_report_time: 汇报时间缺失，周期无法推导")
    if not submitter_userid:
        warnings.append("missing_submitter: 缺少提交人 userid，无法关联员工")
    if attachments:
        warnings.append(
            f"attachments_not_downloaded: {len(attachments)} 个附件仅记录元信息，"
            f"V1 不下载文件（media_id 有时效）"
        )
    parsed.warnings = warnings
    return parsed
