#!/usr/bin/env python
"""企微通知发送自测脚本（不碰数据库，不触发催交流程）。

用途：验证 message/send 链路是否打通（凭据、AgentId、可信 IP、收件人 userid）。
只发给你指定的 userid，默认是「预览模式」，不加 --send 绝不真发。

运行（在项目根目录）：
    # 预览：只打印将发送的卡片，不调用发送接口
    python scripts/test_wecom_notify.py --touser example_user

    # 真发：给指定人发一条测试卡片
    python scripts/test_wecom_notify.py --touser example_user --send

判读：
    errcode=0            -> 链路通，收件人企微会收到「周报提醒」卡片
    errcode=60020        -> 本机出口 IP 不在应用可信 IP 白名单（错误信息里有 from ip: x.x.x.x）
    errcode=81013        -> userid 无效（invaliduser 字段会列出）
    缺少 WECOM_AGENT_ID  -> .env 未配置，发送应用消息必须要它
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.connectors import base as cb  # noqa: E402
from app.connectors.wecom.client import WeComClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402

TEST_TITLE = "【测试】周报提醒链路自测"
TEST_DESCRIPTION = (
    '<div class="normal">这是一条系统联调测试消息。</div>'
    '<div class="highlight">收到即代表企微通知链路正常，可忽略。</div>'
)


def main() -> int:
    parser = argparse.ArgumentParser(description="企微通知发送自测")
    parser.add_argument("--touser", required=True, help="收件人企微 userid（管理后台成员详情的「账号」）")
    parser.add_argument("--send", action="store_true", help="真发；不加只预览")
    parser.add_argument("--url", default=None, help="卡片跳转地址，默认取 REMIND_CARD_URL")
    args = parser.parse_args()

    setup_logging("WARNING")
    settings = get_settings()

    if not settings.wecom_corp_id or not settings.wecom_corp_secret:
        print("错误：.env 未配置 WECOM_CORP_ID / WECOM_CORP_SECRET。", file=sys.stderr)
        return 2
    if not settings.wecom_agent_id:
        print(
            "错误：.env 未配置 WECOM_AGENT_ID，发送应用消息必须要它。\n"
            "管理后台 -> 应用管理 -> 自建应用详情 -> AgentId（一串数字），\n"
            "写入项目根目录 .env：WECOM_AGENT_ID=数字",
            file=sys.stderr,
        )
        return 2

    url = args.url or settings.remind_card_url
    print(f"收件人: {args.touser}")
    print(f"卡片标题: {TEST_TITLE}")
    print(f"跳转地址: {url}")

    if not args.send:
        print("\n[预览模式] 未发送。确认无误后加 --send 真发。")
        return 0

    try:
        with WeComClient() as client:
            result = client.send_text_card(
                user_ids=[args.touser],
                title=TEST_TITLE,
                description=TEST_DESCRIPTION,
                url=url,
                btn_text="知道了",
            )
    except cb.ConnectorError as exc:
        print(f"\n发送失败：{exc}", file=sys.stderr)
        return 1

    errcode = result.get("errcode")
    print(f"\nerrcode={errcode} errmsg={result.get('errmsg')}")
    invalid = result.get("invaliduser", "")
    if invalid:
        print(f"无效 userid：{invalid}（这些人不属于应用可见范围或账号不对）")
        return 1
    if errcode == 0:
        print("结论：链路正常，请到企微里查收卡片。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
