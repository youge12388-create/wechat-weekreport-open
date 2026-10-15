"""前端关键产品约束的轻量回归检查。

项目暂未配置前端单测框架；这些检查只守住容易被旧分支合回的页面结构，
具体交互仍由 TypeScript 检查和浏览器验收覆盖。
"""

import re
from pathlib import Path


BLOCKERS_PAGE = (
    Path(__file__).resolve().parents[1] / "frontend" / "app" / "blockers" / "page.tsx"
)
BLOCKERS_LAYOUT = BLOCKERS_PAGE.with_name("layout.tsx")
ROOT_LAYOUT = BLOCKERS_PAGE.parents[1] / "layout.tsx"
SHELL = BLOCKERS_PAGE.parents[1].parents[0] / "components" / "shell.tsx"
LOGIN_PAGE = BLOCKERS_PAGE.parents[1] / "login" / "page.tsx"
CHUNK_RECOVERY = BLOCKERS_PAGE.parents[1].parents[0] / "components" / "chunk-recovery.tsx"
OPERATING_DASHBOARD = BLOCKERS_PAGE.parents[1] / "operating-dashboard" / "page.tsx"
OPERATING_OVERVIEW = OPERATING_DASHBOARD.with_name("overview.tsx")
AI_PAGE = BLOCKERS_PAGE.parents[1] / "ai" / "page.tsx"
DEPARTMENT_DETAIL = BLOCKERS_PAGE.parents[1] / "departments" / "[id]" / "page.tsx"


def test_blockers_page_keeps_only_two_period_cards() -> None:
    source = BLOCKERS_PAGE.read_text(encoding="utf-8")

    assert 'grid grid-cols-2 gap-2' in source
    assert '[["last_week", "上周卡点"], ["this_week", "本周卡点"]]' in source
    assert "跨期待处理" not in source


def test_removed_range_links_cannot_become_custom_periods() -> None:
    source = BLOCKERS_PAGE.read_text(encoding="utf-8")

    assert 'const hasRemovedRange = searchParams.has("range")' in source
    assert "!hasRemovedRange && isIsoDate(urlStart)" in source


def test_blockers_route_is_not_long_term_static_html() -> None:
    source = BLOCKERS_LAYOUT.read_text(encoding="utf-8")

    assert 'dynamic = "force-dynamic"' in source
    assert "revalidate = 0" in source


def test_root_pages_are_not_cached_across_releases() -> None:
    source = ROOT_LAYOUT.read_text(encoding="utf-8")

    assert 'dynamic = "force-dynamic"' in source
    assert "revalidate = 0" in source


def test_member_cannot_see_management_shell_before_redirect() -> None:
    source = SHELL.read_text(encoding="utf-8")

    assert "正在确认登录状态" in source
    assert "正在打开发给你的事项" in source
    assert 'window.location.replace(MEMBER_NAV_ITEM.href)' in source


def test_login_and_chunk_recovery_escape_stale_release_html() -> None:
    login_source = LOGIN_PAGE.read_text(encoding="utf-8")
    recovery_source = CHUNK_RECOVERY.read_text(encoding="utf-8")
    root_source = ROOT_LAYOUT.read_text(encoding="utf-8")

    assert 'window.location.href = "/dashboard"' in login_source
    assert 'params.get("next") || "/dashboard"' in login_source
    assert "ChunkLoadError" in recovery_source
    assert 'url.searchParams.set("_reload"' in recovery_source
    assert 'id="zhoubao-chunk-recovery-bootstrap"' in root_source
    assert "dangerouslySetInnerHTML" in root_source
    assert 'url.searchParams.set("_reload"' in root_source


def test_department_analysis_button_scans_inside_department_scope() -> None:
    """部门页就地补分析（2026-09-28）：不再跳转 /ai，扫描必须带部门范围。

    旧设计是跳 ``/ai`` 让用户手动触发（入口埋在折叠卡里，闭环断了）；
    现在覆盖率条与卡点卡各有一个 ``AnalysisScanButton``，直接以当前部门
    与周期入队。``/ai`` 页仍支持 ``department_id`` 的对话范围（历史与
    记忆按部门过滤），这部分契约不变。
    """
    department_page = DEPARTMENT_DETAIL.read_text(encoding="utf-8")
    ai_page = AI_PAGE.read_text(encoding="utf-8")

    assert department_page.count("<AnalysisScanButton") == 2
    assert "departmentId={id}" in department_page
    assert "onChanged={reloadAnalysis}" in department_page
    assert "去分析本期" not in department_page
    assert 'params.get("department_id")' in ai_page
    # surface/dashboard_view 由后端按 department_id 推导；前端重复传会出现
    # 「谁才是真源」的两个答案，这里守护前端不再传这两个参数。
    assert "surface: departmentId" not in ai_page
    assert "dashboard_view: departmentId" not in ai_page
    assert "historyMatchesDepartment" in ai_page
    assert "const requestedDepartmentId = resolution?.department_id ?? departmentId;" in ai_page
    assert "department_id: requestedDepartmentId ?? undefined" in ai_page
    assert "(payload.department_id ?? null) !== requestedDepartmentId" in ai_page


def test_monthly_conclusions_require_current_analysis_and_summary() -> None:
    page = OPERATING_DASHBOARD.read_text(encoding="utf-8")
    assert 'summary.analysis_ready !== true' in page
    assert '本期周报尚未完成 AI 分析' in page
    assert '<MonthlyWaitingState' in page
    assert 'payload?.summary.analysis_ready === true' in page
    assert 'payload.summary.status === "success"' in page
    assert '!payload.summary.stale' in page
    assert '本期公司结论需要更新' in page
    assert '已就绪部门可在下方查看' in page
    assert '<MonthlyDepartments payload={payload}' in page
    assert '逐篇 AI 分析状态待核实' in page
    assert 'payload.coverage.analyzed_reports' not in page
    assert 'totalOverride={total}' in page

    scan_button = (OPERATING_DASHBOARD.parents[2] / "components" / "analysis-scan-button.tsx").read_text(encoding="utf-8")
    assert 'const displayAnalyzed = Math.max(0, displayTotal - displayRemaining)' in scan_button
    assert '本周期已完成 ${displayAnalyzed}/${displayTotal} 篇' in scan_button


def test_monthly_card_layout_is_condensed() -> None:
    page = OPERATING_DASHBOARD.read_text(encoding="utf-8")
    sections = (OPERATING_DASHBOARD.parent / "monthly-sections.tsx").read_text(encoding="utf-8")
    assert '<MonthlyMainlines' in page
    assert '<MonthlyChanges' in page
    assert '<MonthlyDepartments' in page
    assert '<MonthlyInterventions' in page
    assert '<MonthlySharedIssues' in page
    assert '<MonthlyStuck' in page
    assert '<MonthlyAnomalies' in page
    assert 'slice(0, 6)' in sections
    assert '公司现在主要推进什么' in sections
    assert 'mainline_summary' in sections
    assert 'SharedIssuesBlock' not in page

def test_overview_metrics_card_has_no_delta_or_basis_lines() -> None:
    """概览卡的四格只留「标签 + 数字 + 单位」（项目负责人 2026-09-24 要求）。

    删掉的是每格下面两行小字：环比（`delta`）与口径说明（`sub`）。
    它们仍在 payload 里（后端 `_overview_metrics` 照常返回，其它消费方可能用），
    这里只守「页面不再渲染」，不要顺手把后端字段删了。
    """
    overview = OPERATING_OVERVIEW.read_text(encoding="utf-8")

    assert "MetricDelta" not in overview
    assert "metric.sub" not in overview
    # 明细入口与四格本身保留：删的只是小字，不是整个指标区
    assert "查看明细" in overview
    assert "metric.value" in overview


def test_focus_card_has_no_basis_footnote_and_single_number_column() -> None:
    """精力投向卡：口径说明与重复的人数不占版面（项目负责人 2026-09-24 要求）。

    两件事各自对应一个真实冗余：
    1. 卡片底部那行「按『员工 × 工作类别』的唯一参与人次计算…」——口径要留着
       （说不清就会把份额读成工时占比），但不该占一屏里的固定一行，改为
       挂在「人员参与度」标签的悬停提示上，文字仍取后端的 `basis`。
    2. 图例每行同时给占比与绝对人次：环心已经写明总人次，八行再排八个绝对数
       等于十六个数字挤在一列里。绝对人次移到悬停提示。
    """
    overview = OPERATING_OVERVIEW.read_text(encoding="utf-8")

    # 口径仍被渲染，只是换成 title 属性；不能整个删掉
    assert "title={basis}" in overview
    assert "payload.focus" in overview
    # 图例里只剩一个数字（占比）：原先那列灰色的绝对人次已移出页面。
    # 绝对人次仍用于扇区取值与悬停提示，所以断言的是「渲染节点没了」而不是「字段没用」。
    assert "num shrink-0 text-muted-foreground\">" not in overview
    assert "category.share" in overview
    assert "category.people_entries" in overview


def test_focus_card_keeps_distinct_colors_and_legend_linkage() -> None:
    """精力投向卡的两条视觉约束（项目负责人 2026-09-24）。

    ① **八个类别必须各有其色**。中间试过一版同色深浅色阶（`chartSequential`），
       被否掉了：并列的几摊工作要能一眼对上号，深浅序列会把「哪块是招生」
       变成必须先读图例。`chartCategorical` 保留，且**不引用 `warning`/`destructive`**
       —— 那两个色在本站是「需关注 / 风险」，放进精力分布是语义误导。
    ② 图例与扇区**双向联动**。颜色再多也只是「读图例找颜色」的一步，联动把它消掉；
       环心文字走 DOM（canvas 缩放会发虚），所以不能再回到 ECharts 的 `label`。
    """
    theme = (BLOCKERS_PAGE.parents[1].parents[0] / "lib" / "chart-theme.ts").read_text(
        encoding="utf-8"
    )
    overview = OPERATING_OVERVIEW.read_text(encoding="utf-8")

    assert "export function chartCategorical" in theme
    # 分类色板不得借用风险语义色
    categorical = theme.split("export function chartCategorical")[1].split("\n}")[0]
    assert "warning" not in categorical
    assert "destructive" not in categorical

    assert "chartCategorical" in overview
    assert "chartSequential" not in overview
    # 双向联动：图例行 → 扇区高亮，扇区 hover → 图例行高亮
    assert "dispatchAction" in overview
    assert "focusSlice" in overview
    assert 'chart.on("mouseover"' in overview
    # 环心文字在 DOM 层
    assert "label: { show: false }" in overview
    assert "参与人次</span>" in overview


FONT_LAYOUT = BLOCKERS_PAGE.parents[1] / "layout.tsx"


def test_cjk_webfont_never_preloads_its_hundred_slices() -> None:
    """中文字体必须是「按需下载」，不能被 preload 全量预拉。

    Noto Sans SC 按 unicode-range 切成 101 个分片（构建产物实测合计约 4.3MB）。
    `preload` 默认是 true，一旦被删掉或改成 true，这些分片会全变成
    `<link rel=preload>` —— 每次进页面都先拉全量，首屏直接被拖垮。
    实测首屏只下载用到的 14 个分片（约 800KB），这个开关是那 800KB 成立的前提。
    """
    layout = FONT_LAYOUT.read_text(encoding="utf-8")

    assert "Noto_Sans_SC" in layout
    assert "preload: false" in layout
    # 中文分片没有可用的 Google subset，声明 latin 只为关闭「缺 subsets」的构建报错
    assert 'subsets: ["latin"]' in layout



def test_monthly_has_quick_month_presets_and_defaults_to_a_preset() -> None:
    """每月视图必须给固定的快捷月份档，且默认落其中一档。

    项目负责人反馈「怎么没看到变化」的直接原因是周期对不上：页面默认开的是自造的
    「近三个月」区间，而简报按具体周期生成 —— 他打开的那一档从来没算过，页面就是
    代码算的计数明细，看着像改动没生效。日期区间对「看简报」这个动作是多余的自由度，
    固定档位才能保证点同一个按钮永远是同一份简报。
    """
    page = OPERATING_DASHBOARD.read_text(encoding="utf-8")

    for label in ('"本月"', '"上月"', '"近三月"', '"近半年"'):
        assert label in page, f"缺少快捷月份档：{label}"
    assert "function monthPresets(" in page
    assert "function activeMonthPreset(" in page
    # 默认必须落在一档上（否则一进页面就是那份没算过的区间）
    assert "monthPresets()[0].range" in page
    # 自造的「近三个月」区间已删除，不留两条口径
    assert "recentMonthsRange" not in page


def test_monthly_auto_queue_requires_completed_analysis() -> None:
    page = OPERATING_DASHBOARD.read_text(encoding="utf-8")
    assert 'state.analysis_ready === false' in page
    assert 'state.status !== "not_generated"' in page
    assert 'autoQueuedRef.current.has(key)' in page


def test_monthly_issue_sections_explain_empty_results_and_keep_analysis_progress() -> None:
    page = OPERATING_DASHBOARD.read_text(encoding="utf-8")
    sections = (OPERATING_DASHBOARD.parent / "monthly-sections.tsx").read_text(encoding="utf-8")
    assert '<AnalysisScanButton' in page
    assert 'analysis_completed' in page
    assert '本期未识别到有来源支持的' in sections
    assert '暂不能判断这一类问题' in sections
    assert '相关问题已列入上方分类' in sections
