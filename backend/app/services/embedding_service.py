"""语义检索（向量）服务层。

职责：把「哪些周报有向量、怎么算相似度、怎么补缺失向量」封装起来，
检索工具（agent_tools）只拿结果，不碰 embedding 客户端细节。

分层约束（与项目一致）：本服务只写 ``ai_embeddings`` 派生表，不碰业务表；
调用失败一律**降级**（返回不可用原因），绝不阻断关键词检索主路径。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.embedding import EmbeddingClient, get_embedding_client
from app.core.logging import get_logger
from app.models.ai_embedding import AIEmbedding
from app.models.weekly_report import WeeklyReport

logger = get_logger(__name__)

# 单次回填最多处理多少篇：脚本/worker 调用方的保险丝，防止首次全量回填
# 在未配置额度时把 embedding 余额一次打穿。
BACKFILL_MAX_REPORTS = 300


def embedding_source_hash(report: WeeklyReport) -> str:
    """向量的内容指纹：标题+正文变了才值得重算。"""
    material = f"{report.title or ''}\n{report.content or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """余弦相似度；零向量返回 0。纯 Python 实现——千篇量级毫秒级，不引 numpy。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def load_vectors(
    session: Session, report_ids: list[int]
) -> dict[int, tuple[str, list[float]]]:
    """批量取一组周报的向量；返回 {report_id: (model, vector)}，没有向量的不出现。"""
    if not report_ids:
        return {}
    rows = session.execute(
        select(AIEmbedding.report_id, AIEmbedding.model, AIEmbedding.vector).where(
            AIEmbedding.report_id.in_(report_ids)
        )
    ).all()
    result: dict[int, tuple[str, list[float]]] = {}
    for report_id, model, vector in rows:
        if isinstance(vector, list) and vector:
            result[int(report_id)] = (str(model), [float(value) for value in vector])
    return result


def ensure_report_embeddings(
    session: Session,
    reports: list[WeeklyReport],
    *,
    client: EmbeddingClient | None = None,
    max_reports: int = BACKFILL_MAX_REPORTS,
) -> dict[str, Any]:
    """为缺失或源已变更的周报补向量；返回结果摘要，失败不抛异常。

    只在语义检索配置齐备时才做任何事；未配置/白名单外一律返回 ``enabled=False``，
    调用方（脚本、worker）据此跳过而不是报错。
    """
    client = client or get_embedding_client()
    if not client.configured():
        return {"enabled": False, "reason": client.unavailable_reason(), "embedded": 0, "skipped": 0, "failed": 0}
    model = client.settings.ai_embedding_model
    if not reports:
        return {"enabled": True, "embedded": 0, "skipped": 0, "failed": 0}

    report_ids = [report.id for report in reports]
    rows = session.execute(
        select(AIEmbedding.report_id, AIEmbedding.source_hash).where(
            AIEmbedding.report_id.in_(report_ids)
        )
    ).all()
    existing = {int(report_id): str(source_hash) for report_id, source_hash in rows}

    pending: list[WeeklyReport] = []
    for report in reports[:max_reports]:
        if existing.get(report.id) == embedding_source_hash(report):
            continue
        text = f"{report.title or ''}\n{report.content or ''}".strip()
        if not text:
            continue
        pending.append(report)

    embedded = 0
    failed = 0
    try:
        vectors = client.embed_texts(
            [f"{report.title or ''}\n{report.content or ''}" for report in pending]
        )
    except Exception as exc:  # noqa: BLE001 回填失败必须降级，不能炸掉调用方
        logger.warning("语义向量回填失败（%s 篇待补）：%s", len(pending), exc)
        return {"enabled": True, "embedded": 0, "skipped": 0, "failed": len(pending), "error": str(exc)[:300]}

    for report, vector in zip(pending, vectors):
        try:
            row = session.scalars(
                select(AIEmbedding).where(AIEmbedding.report_id == report.id)
            ).first()
            if row is None:
                row = AIEmbedding(report_id=report.id)
                session.add(row)
            row.model = model
            row.dim = len(vector)
            row.vector = vector
            row.source_hash = embedding_source_hash(report)
            session.flush()
            embedded += 1
        except Exception as exc:  # noqa: BLE001 单篇失败不影响其他篇
            failed += 1
            logger.warning("周报 %s 向量写库失败：%s", report.id, exc)
    session.commit()
    return {
        "enabled": True,
        "embedded": embedded,
        "skipped": len(reports) - len(pending),
        "failed": failed,
    }
