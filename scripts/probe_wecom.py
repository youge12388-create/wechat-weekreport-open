#!/usr/bin/env python
"""企业微信连通性与权限探针。

用途：在写任何同步代码之前，先确认「凭据是否有效 + 汇报接口是否有权限」。

运行：
    python scripts/probe_wecom.py

判读：
    steps[0] get_access_token 失败 -> CorpID / Secret 不对
    steps[1] department/list 失败 -> 应用可见范围未包含组织架构
    steps[2] journal 失败且含 301065 -> 应用未加入「汇报 - 可调用接口的应用」
                                       或服务器 IP 未配置为可信 IP
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.connectors.wecom.client import WeComClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402


def _extract_ip(error: str) -> str | None:
    """从企微错误信息里提取出口公网 IP。"""
    match = re.search(r"from ip:\s*([\d.]+)", error)
    return match.group(1) if match else None


def main() -> int:
    setup_logging("WARNING")
    settings = get_settings()

    if not settings.wecom_corp_id or not settings.wecom_corp_secret:
        print("未配置企业微信凭据。", file=sys.stderr)
        print("请在项目根目录创建 .env（参考 .env.example）：", file=sys.stderr)
        print("  WECOM_CORP_ID=...", file=sys.stderr)
        print("  WECOM_CORP_SECRET=...", file=sys.stderr)
        return 2

    print(f"CorpID: {settings.wecom_corp_id[:6]}...（已隐藏）")
    print(f"BaseURL: {settings.wecom_base_url}")
    print("")

    with WeComClient() as client:
        result = client.health_check()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("")

    if result["ok"]:
        print("结论：凭据有效，通讯录与汇报接口均可访问。")
        return 0

    failed = next((s for s in result["steps"] if not s["ok"]), None)
    if failed:
        error = str(failed.get("error", ""))
        print(f"结论：{failed['step']} 未通过。\n错误：{error}")

        egress_ip = _extract_ip(error)
        if "60020" in error:
            print("\n当前公网出口 IP：" + (egress_ip or "（未识别，见错误信息 from ip）"))
            print("处置步骤：")
            print("  1. 企业微信管理后台 -> 应用管理 -> 该自建应用 -> 开发者接口")
            print("  2. 「企业可信IP」中填入上面的出口 IP 并保存")
            print("     （若仍报错，再到「我的企业 -> 企业信息 -> 企业可信IP」同样添加一次）")
            print("  3. 注意：家庭宽带出口 IP 会变动，生产环境建议用固定公网 IP 的服务器")
            print("  4. 重新运行本探针")
        elif "48002" in error or "301065" in error:
            print("\n当前错误含义：接口未对当前自建应用授权（api forbidden）。")
            print("处置步骤：")
            print("  1. 企业微信管理后台 -> 应用管理 -> 找到官方「汇报」应用（不是你的自建应用）")
            print("  2. 进入「汇报」应用 -> 高级设置 / 接口权限 -> 「可调用接口的应用」")
            print("  3. 把持有该 Secret 的自建应用加入白名单并保存")
            print("  4. 确认该自建应用已在「企业可信IP」中加入出口 IP " + (egress_ip or "（见错误信息 from ip）"))
            print("  5. 重新运行本探针")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
