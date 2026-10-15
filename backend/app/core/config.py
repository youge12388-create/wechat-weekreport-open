"""应用配置。

所有外部系统凭据只能通过环境变量 / .env 注入，禁止硬编码。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 汇报周期规则：禁止在业务代码里硬编码「ISO 周」假设（AGENTS.md 7.1）。
# - week        ：自然周（周一~周日），等同原 ISO 周推导
# - month       ：自然月
# - custom_week ：自定义起始星期（report_custom_week_start 指定，0=周一）
REPORT_PERIOD_RULES = ("week", "month", "custom_week")
HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"

BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 汇报类型识别关键字。
# 需求第 9 节要求「分类字段必须可配置，不允许硬编码在业务代码中」，
# 汇报类型识别同理：不同企业的汇报表单名称不同，此处为默认值，可用环境变量覆盖。
DEFAULT_REPORT_TYPE_KEYWORDS: dict[str, list[str]] = {
    "weekly": ["周报", "本周", "周工作"],
    "daily": ["日报", "今日", "日工作"],
    "monthly": ["月报", "月度", "本月"],
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".." / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    log_level: str = "INFO"

    # ---- Database ----
    # SQLite 阶段默认本地文件；切换 PostgreSQL 时改为
    # postgresql+psycopg2://user:pass@host:5432/dbname
    db_url: str = Field(default="sqlite:///./data/app.db")
    db_echo: bool = False

    # ---- WeCom ----
    wecom_corp_id: str = ""
    wecom_corp_secret: str = ""
    # 自建应用 AgentId（仅用于日志与排查，汇报接口调用不强依赖）
    wecom_agent_id: str = ""
    wecom_base_url: str = "https://qyapi.weixin.qq.com"
    wecom_http_timeout: float = 15.0
    wecom_http_retries: int = 3
    # 只同步指定汇报表单（template_id），留空表示不过滤。
    # 企业微信后台：汇报应用 -> 内容设置页 -> 点击汇报名称 获取。
    wecom_report_template_ids: list[str] = Field(default_factory=list)

    # ---- 同步 ----
    # 拉取周报时的默认回溯天数
    wecom_sync_lookback_days: int = 30
    # 单批次最大处理条数，防止一次误操作拉取过大数据量
    sync_max_records_per_run: int = 5000
    # 后台自动同步：本地默认关闭；Linux 服务器通过环境变量开启。
    sync_auto_enabled: bool = False
    # 部门/员工每天校准一次。
    sync_org_interval_hours: int = Field(default=24, ge=1, le=168)
    # 周报每周固定时间拉取；工作日使用 0=周一 ... 6=周日，时刻使用业务时区。
    sync_reports_weekday: int = Field(default=4, ge=0, le=6)
    # 周五催交截止时间为 21:00，自动抓取必须安排在截止后，才能收到晚交周报。
    sync_reports_hour: int = Field(default=21, ge=0, le=23)
    sync_reports_minute: int = Field(default=5, ge=0, le=59)
    # 抓取发现新增或变更周报后，自动生成对应周期的全员 AI 总结。
    sync_auto_period_summary_enabled: bool = True
    # 抓取到新增/内容变化的周报后，自动把单篇周报加入 AI 分析队列。
    # 默认关闭：分析只在管理员于周报详情页手动点击时进行。
    ai_auto_enqueue_on_sync: bool = False
    # 增量同步向前重叠的天数：覆盖迟到提交和上游内容修改，靠幂等去重。
    sync_overlap_days: int = Field(default=3, ge=1, le=30)

    @field_validator("db_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        """把 sqlite 相对路径解析为基于 backend/ 的绝对路径。

        否则 CWD 一变（CLI / alembic / pytest 各不相同）就会指向不同的库文件。
        """
        prefix = "sqlite:///"
        if value.startswith(prefix):
            path_part = value[len(prefix) :]
            if path_part.startswith(":memory:"):
                return value
            abs_path = (BACKEND_ROOT / path_part).resolve()
            return f"{prefix}{abs_path.as_posix()}"
        return value

    # ---- 汇报类型识别（可被环境变量覆盖为 JSON）----
    report_type_keywords: dict[str, list[str]] = Field(
        default_factory=lambda: dict(DEFAULT_REPORT_TYPE_KEYWORDS)
    )
    # 无法识别时使用的兜底类型
    report_type_fallback: str = "other"

    # ---- 统计口径（业务规则，均可用环境变量覆盖）----
    # 免提交周报的部门：按部门名称**精确匹配**，不含子部门。
    # 这些部门的人不计入「在职应提交」基数，也不进未提交名单；
    # 但她们已经提交过的周报仍然可以查询（只影响统计，不影响存档）。
    report_exempt_departments: list[str] = Field(default_factory=list)
    # 周报提交截止时间：每周第几个工作日的几点（weekday 周一=0，周五=4）。
    # 未到该时点时默认周期取上一周 —— 本周还没到交周报的时候，
    # 盯着本周看只会看到「全员未交」的假警报。
    report_due_weekday: int = 4
    report_due_hour: int = 21
    # 业务时区与 UTC 的偏移小时数（中国=8）。库里存 UTC naive，业务按本地时间判定。
    report_timezone_offset_hours: int = 8
    # 周期归属天数：周期开始后的前 N 天内提交，仍归到**上一个**周期。
    # 周报通常周五到周日晚间交，但那是习惯不是截止；有人周一才写完交上来，
    # 那一篇写的是上周的工作。若按提交日所在自然周归类，它会落进本周 ——
    # 本周被凭空算成「已提交」，周五的催交名单也会漏掉这个人（而他本周确实还没交）。
    # 这是**归类口径**：周一交的那篇本来就属于上一周期。
    # 默认 1 = 周一提交算上一周期；0 = 不做回溯，按提交当天算。
    report_period_attribution_days: int = Field(default=1, ge=0, le=7)

    # ---- 企微催交提醒 ----
    # 催交卡片文案。默认中英双语：库里既有中文周报也有英文周报（实测约 22%
    # 正文几乎不含汉字），只发中文等于对这批人发了一张看不懂的卡片。
    # 支持 {period}（如 2026-09-07 ~ 2026-09-13）/ {start} / {end} 占位符；
    # 每个语言块的最后一段会渲染成企微卡片的高亮行。
    # 这些值都可在「系统设置 → 周报规则」里改，改完下一次发送即生效。
    remind_card_title: str = "周报提醒 · Weekly Report"
    remind_card_body_zh: str = (
        "本期周报还没提交。\n统计周期：{period}\n请于今天内提交，谢谢！"
    )
    remind_card_body_en: str = (
        "Your weekly report has not been submitted yet.\n"
        "Period: {period}\n"
        "Please submit it today. Texample_user you!"
    )
    # 卡片点击跳转地址。部署到内网后改成同事能访问的域名，否则点了打不开。
    remind_card_url: str = "http://127.0.0.1:3000"
    # 自动催交：默认关闭，必须由后台或环境变量显式开启。
    # 工作日使用 0=周一 ... 6=周日；时刻使用业务时区。
    remind_auto_enabled: bool = False
    remind_auto_weekday: int = Field(default=4, ge=0, le=6)
    remind_auto_hour: int = Field(default=21, ge=0, le=23)
    # 跟进沟通里「负责人回复了」要通知到的企微 userid，逗号分隔。
    # 存在的理由：用访问密码登录的管理员没有员工身份，系统不知道他的企微账号，
    # 若不显式配置，他发出的会话就收不到任何回复通知。参与过会话的企微管理员
    # 会被自动纳入收件人（见 management_action_message_service.resolve_notify_targets），
    # 这里是补充、不是唯一来源。留空时发送结果会明确说明「没有可通知的管理员」。
    action_notify_admin_userids: str = ""

    # ---- 大模型（AI 周报检索 / 重复检测）----
    # 只依赖 OpenAI 兼容的 chat/completions 协议；换厂商只改 base_url + model。
    ai_provider: Literal["deepseek", "fake"] = "deepseek"
    ai_api_key: str = ""  # env: AI_API_KEY（DeepSeek 控制台生成）
    ai_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"
    # 允许作为出站 AI 请求目标的主机白名单。必须是**部署侧**配置（环境变量），
    # 刻意不放运行时设置：它保护的正是「已保存密钥被发到哪台主机」，
    # 若能在设置页随手改，这道闸门就形同虚设。见 core/url_guard.py。
    ai_allowed_base_hosts: list[str] = Field(default_factory=lambda: ["api.deepseek.com"])
    ai_temperature: float = 0.2  # 检索问答要的是稳定复述，不是创作
    ai_timeout: float = 60.0
    ai_max_retries: int = 2
    # 重试间隔：指数退避 base * 2^(N-1)，并叠加抖动，上限 ai_retry_max_seconds。
    # 必须有等待：毫秒内连打三次 429 只会拿到同样的 429，重试预算白烧。
    ai_retry_base_seconds: float = Field(default=1.5, ge=0.1)
    ai_retry_max_seconds: float = Field(default=20.0, ge=1.0)
    # 单次回复的最大 token 数（0=不限制）。不设上限时，超长结构化输出可能被上游
    # 从中间截断，导致 JSON 解析失败并按次数重试（每次都计费）。
    # 默认 8192：统一分析要求模型一次输出 摘要+评分明细+8 类事项数组（每条带
    # source_text 原文引用），事项多的周报实测可超 2000 token；旧默认 2048 会让
    # 上游按 finish_reason=length 截断 JSON，表现为「未找到 JSON 对象」的迷惑报错。
    # 8192 是 deepseek-chat 的输出上限，不会越界。
    ai_max_output_tokens: int = Field(default=8192, ge=0)
    # 流式请求是否显式索取 usage。OpenAI 兼容接口默认**不在流里返回 usage**，
    # 不索取就永远拿不到 token 数（Agent 是最贵的路径，用量账本会显示 0）。
    # 个别兼容实现不认该字段时置 false 关闭。
    ai_stream_include_usage: bool = True
    # 单次请求允许喂给模型的最大字符数：周报正文可能很长，不设上限会把成本打飞，
    # 也可能直接超模型上下文。超出部分按截断处理并在结果里标注。
    ai_max_input_chars: int = 12000
    # 检索问答：最多取多少篇周报作为候选喂给模型
    ai_search_max_reports: int = 30
    # Agent 的「全量上下文」阈值：范围内正文总字符数不超过它，就把全部原文一次性
    # 交给模型，不做关键词检索。理由是检索会漏同义表达，也命中不到非中文周报
    # （实测库里 22% 是英文），而单周期全文实测仅约 3 万字符，装得下。
    # 超限（如「全部历史」或近 4 周）才退回工具检索 + 分页。
    ai_agent_full_context_chars: int = 60000
    # Agent 整个会话（全量上下文 + 历史轮次 + 所有工具结果）的**总输入字符预算**。
    # 为什么必须有：messages 会随每一轮工具调用增长，而**每一轮都要把全部
    # messages 重新发给模型**，所以成本随轮数近似平方增长；工具单项上限又很宽
    # （卡点/任务各 100 条、未提交名单 200 人），几步就能把上下文撑爆，
    # 直接撞模型上下文上限导致整轮问答 400 失败。
    # 必须 > ai_agent_full_context_chars（否则全量上下文路径自己就被砍）。
    # 超限时对工具结果按剩余额度截断并**如实标注**，绝不静默丢弃。
    ai_agent_max_context_chars: int = 90000
    # 重复检测：与同一员工最近几篇周报做比对
    ai_duplicate_lookback: int = 3
    # 批量体检：单次最多扫多少篇。防止有人手滑点一下，就把一个月预算烧穿。
    ai_scan_max_reports: int = 50
    # 管理概览（本周概览）：范围内周报正文总字符数不超过此值时，**一次把全部原文
    # 交给模型**，不做分批 map-reduce。分批会切断跨篇关联（同一事项在多部门出现、
    # 卡点与下周计划互相呼应），而这正是管理层要看的；单周期实测约 3~4 万字符，装得下。
    # 超限才回退到分批汇总，并在结果里如实标注「分批」而不是假装读全。
    ai_period_summary_full_context_chars: int = 60000
    # 分批回退路径的单次请求字符预算；所有分段都会进入模型，禁止静默截断。
    ai_period_summary_chunk_chars: int = 8000
    # Agent 全历史直读时使用更大的批次，并以有界并发缩短等待时间。
    # 32k 字符为提示词和模型输出保留余量；并发默认 4，仍受上限约束。
    ai_agent_batch_chars: int = Field(default=32000, ge=4000, le=60000)
    ai_agent_batch_concurrency: int = Field(default=4, ge=1, le=6)
    # 周期卡点总结的单次请求字符预算。这条路径**没有分批回退**（卡点总结的价值
    # 就在跨卡点关联，分批会把关联切断），所以超限的代价是整轮失败而不是变慢：
    # 上游拒绝后队列会按重试次数反复重发同一份超长 payload，每次都计费。
    # 实测 60 个卡点、每个两条依据即达 8.7 万字符，故必须设上限，超限时按优先级
    # 取前 N 条并在结论里如实标注「仅总结前 N 条」。
    ai_blocker_summary_context_chars: int = Field(default=60000, ge=2000)

    # ---- 语义检索（向量）----
    # OpenAI 兼容 /embeddings 端点。三项（base_url/api_key/model）齐备才启用；
    # 任一缺失时语义检索自动关闭，检索退回关键词模式，绝不因此报错。
    # 向量以 JSON 存 SQLite/PG（项目约定禁用 JSONB 专有操作符），余弦在 Python
    # 侧计算——周报量级（千篇以内）全量内存比对毫秒级完成，不需要向量数据库。
    ai_embedding_base_url: str = ""
    ai_embedding_api_key: str = ""
    ai_embedding_model: str = ""
    # 语义检索允许的出站主机白名单：与 chat 出站白名单同一安全边界（部署侧配置），
    # embedding 服务商通常与 chat 不是同一台主机，独立配置避免混管。
    ai_embedding_allowed_hosts: list[str] = Field(default_factory=list)
    # 单次 embedding 请求最多多少条文本：第三方服务普遍限制 batch 大小。
    ai_embedding_batch_size: int = Field(default=16, ge=1, le=128)
    # 语义排序参与比对的周报上限：超范围（全部历史 > 数千篇）时截断并如实标注。
    ai_embedding_max_reports: int = Field(default=800, ge=1)

    # ---- 百度大模型文本翻译 ----
    # 这些环境变量作为首次部署的兜底值。管理员后续可在设置页安全替换，
    # 页面写入的是加密后的数据库配置，不会回传明文。
    baidu_translate_app_id: str = ""
    baidu_translate_api_key: str = ""
    baidu_translate_secret_key: str = ""
    baidu_translate_base_url: str = "https://fanyi-api.baidu.com"
    baidu_translate_timeout: float = 30.0

    # 设置页落库凭据的加密根密钥。生产必须通过环境变量提供 Fernet key；
    # 非生产环境若留空，则使用 config_key_file 指定的本地密钥文件（首次自动生成）。
    config_encryption_key: str = ""
    # 非生产环境的加密根密钥文件。之所以不派生自 SESSION_SECRET：
    # ① 派生值等于把「公开的默认 session_secret」当密钥，能读库就能解密；
    # ② 轮换 SESSION_SECRET 会让所有已存密钥解不开（表现为 409 无法解密）。
    # 自动生成的文件是每台机器唯一且稳定的，两个问题都不存在。
    config_key_file: str = "data/.config_secret.key"

    # ---- 后台分析 Worker（原则：AI 分析不占网页请求线程）----
    # 并发数默认 5（2C4G 建议 5~10，可在 .env 覆盖）。
    #
    # 这是**进程内线程并发**：领取任务用 CAS（条件 UPDATE）+ 行锁双保险，
    # 所以把它调大、或再起一个 Worker 进程都不会重复分析同一篇。
    # 但要注意两个上限：① 模型上游的速率限制（429 会由 Provider 层退避重试）；
    # ② 线程共享一个 SQLite 库，写并发受 SQLite 自身限制 ——
    #    AI 结果写入是逐篇提交的短事务，实测压力不大；若后续换 PostgreSQL，
    #    这里可以放心继续调高。
    ai_worker_concurrency: int = Field(default=5, ge=1, le=20)
    # Worker 空闲轮询间隔（秒）
    ai_worker_poll_seconds: float = Field(default=3.0, ge=0.5)
    # 历史分析投影（backfill_cached_analyses）的执行间隔。它是数据修复动作，
    # 不该每轮轮询都跑；置 0 表示只在 Worker 启动时跑一次。
    ai_backfill_interval_seconds: int = Field(default=3600, ge=0)
    # 单篇分析最大尝试次数（网络/限流由模型 Provider 层先重试）
    ai_analysis_max_attempts: int = Field(default=3, ge=1, le=10)
    # 失败退避基准秒数：第 N 次失败后约 base * 2^(N-1) 秒再试
    ai_analysis_retry_base_seconds: float = Field(default=60.0, ge=1.0)
    # 任务租约：进程崩溃/被杀会让任务行永远停在 RUNNING，而幂等入队把
    # 「同类型已有 RUNNING」当作活跃任务直接返回 —— 于是同步永久停摆。
    # Worker 启动时无条件回收所有 RUNNING（此刻旧进程必然已不在）；
    # 运行期只回收超过该时长的，防止单个线程卡死拖住整类任务。
    # 必须大于一次最长同步的耗时，否则会误伤正在运行的任务。
    worker_job_lease_minutes: int = Field(default=90, ge=1, le=1440)

    # ---- 登录与权限（Sprint 1 缺失项的最小实现：单管理员密码 + 签名会话）----
    # 部署时必须通过环境变量覆盖，且不要使用默认值。
    admin_password: str = "changeme"
    # 会话令牌签名密钥：必须随机且**稳定**（重启后不变），否则已登录会话会失效。
    session_secret: str = "dev-insecure-session-secret"
    # 会话 Cookie 是否仅通过 HTTPS 传输（内网 HTTPS 部署时置 true）。
    cookie_secure: bool = False

    # ---- 界面调色盘 ----
    # 这些是设置页未保存运行时覆盖时的默认值；运行时覆盖保存在 system_settings。
    # 颜色通过设置接口限制为 6 位十六进制；前端会根据底色自动选择前景色。
    ui_primary_color: str = Field(default="#1A1C28", pattern=HEX_COLOR_PATTERN)
    ui_nav_color: str = Field(default="#181B27", pattern=HEX_COLOR_PATTERN)
    ui_accent_color: str = Field(default="#259D8D", pattern=HEX_COLOR_PATTERN)
    ui_background_color: str = Field(default="#F4F6F8", pattern=HEX_COLOR_PATTERN)
    ui_surface_color: str = Field(default="#FFFFFF", pattern=HEX_COLOR_PATTERN)

    # 登录失败限速：单客户端窗口内允许的失败次数 / 全局失败次数（防换 IP 绕过）。
    # 成功登录会清零该客户端计数；达到阈值后返回 429 并给出 Retry-After。
    login_max_failures: int = Field(default=5, ge=1, le=100)
    login_max_global_failures: int = Field(default=30, ge=1, le=1000)
    login_lockout_seconds: float = Field(default=300.0, ge=10.0)

    # ---- 企业微信登录（网页授权 + 扫码登录）----
    # 总开关。默认关闭：升级后「谁能登录」必须由管理员在设置页显式授权，
    # 不能因为发了新版本就把全公司一次性放进来。
    wecom_login_enabled: bool = False
    # 登录成功后的回调地址，必须与自建应用后台配置的
    # 「Web 网页授权回调域名 / OAuth 可信域名」完全一致（**含端口**）。
    # 留空时按请求 Host 自动推导（本机开发省事，生产建议显式配置）。
    wecom_login_redirect_uri: str = ""
    # 登录成功后的前端落地页。
    wecom_login_frontend_url: str = ""
    # 未授权人员的处理：show（默认，明确告知原因）/ redirect（退回登录页静默）。
    # 明确告知是默认值：成员点进来只看到「登录失败」会反复重试并来找管理员。
    wecom_login_denied_mode: Literal["show", "redirect"] = "show"

    # ---- 汇报周期规则（业务规则，必须可配置，禁止硬编码 ISO 周假设）----
    report_period_rule: Literal[REPORT_PERIOD_RULES] = "week"
    # 自定义周起始星期：0=周一 .. 6=周日，仅 report_period_rule=custom_week 生效。
    report_custom_week_start: int = 0

    # ---- 催交与员工健壮性（P2）----
    # 企微催交冷却：同一员工同一周期在冷却期内不重复发送（避免一天催五遍）
    remind_cooldown_hours: int = 24
    # 员工「疑似离职」判定阈值：同步时若某在职员工超过该天数未出现在企微返回中，
    # 标记为 MISSING（疑似离职）；空批次（接口抽风）绝不触发，避免误判全员离职。
    employee_missing_days: int = 180

    # ---- 卡点生命周期（候选 → 正式 → 休眠 → 归档）----
    # 静默多少周后降为休眠（灰显、仍可一键复活）。
    blocker_silence_dormant_weeks: int = Field(default=2, ge=1)
    # 静默多少周后归档（默认不在处理中心主列表出现，新证据自动复活）。
    blocker_silence_archive_weeks: int = Field(default=3, ge=1)
    # 升级为正式卡点所需的最少证据：不同周报周期数或不同员工数达到该值即 OPEN。
    blocker_upgrade_min_evidence: int = Field(default=2, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """测试用：丢弃缓存重新读取。"""
    get_settings.cache_clear()
    return get_settings()
