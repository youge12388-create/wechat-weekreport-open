"""验收项 8：两条正文通道都能正确解析。"""

from __future__ import annotations

from app.processors.journal_parser import (
    detect_report_type,
    parse_journal_info,
    render_apply_data,
    strip_html,
)
from app.processors.text_cleaner import clean_text


def _info(**kwargs) -> dict:
    return {
        "journal_uuid": "j1",
        "template_name": "本周工作汇报",
        "template_id": "tpl-1",
        "report_time": 1786000000,
        "submitter": {"userid": "u1"},
        **kwargs,
    }


def test_channel_apply_data() -> None:
    info = _info(
        apply_data={
            "contents": [
                {
                    "control": "Textarea",
                    "title": [{"text": "本周工作"}],
                    "value": {"text": "优化筛查网站英文版本"},
                }
            ]
        }
    )
    parsed = parse_journal_info(info, "j1")
    assert "优化筛查网站英文版本" in parsed.content
    assert parsed.report_type == "weekly"
    assert parsed.submitter_userid == "u1"
    assert not parsed.warnings


def test_channel_sys_journal_data() -> None:
    """原生汇报模板：正文在 sys_journal_data，apply_data 不返回。"""
    info = _info(
        sys_journal_data="<p>本周优化了筛查网站</p><p>下周继续测试</p>"
    )
    parsed = parse_journal_info(info, "j1")
    assert "本周优化了筛查网站" in parsed.content
    assert "下周继续测试" in parsed.content
    assert "<p>" not in parsed.content
    assert not parsed.is_empty


def test_both_channels_are_concatenated() -> None:
    info = _info(
        sys_journal_data="<p>富文本正文</p>",
        apply_data={
            "contents": [
                {
                    "control": "Text",
                    "title": [{"text": "补充说明"}],
                    "value": {"text": "表单正文"},
                }
            ]
        },
    )
    parsed = parse_journal_info(info, "j1")
    assert "富文本正文" in parsed.content
    assert "表单正文" in parsed.content


def test_empty_content_raises_warning_not_silence() -> None:
    parsed = parse_journal_info(_info(), "j1")
    assert parsed.is_empty
    assert any("empty_content" in w for w in parsed.warnings)


def test_missing_submitter_warning() -> None:
    info = _info(sys_journal_data="<p>x</p>")
    info.pop("submitter")
    parsed = parse_journal_info(info, "j1")
    assert any("missing_submitter" in w for w in parsed.warnings)


def test_file_control_records_attachment_without_downloading() -> None:
    attachments: list[dict] = []
    text = render_apply_data(
        {
            "contents": [
                {
                    "control": "File",
                    "title": [{"text": "附件"}],
                    "value": {"files": [{"file_id": "media-1"}]},
                }
            ]
        },
        attachments,
    )
    assert attachments == [{"type": "file", "file_id": "media-1"}]
    assert "[附件 1 个]" in text


def test_table_control_renders_rows() -> None:
    attachments: list[dict] = []
    text = render_apply_data(
        {
            "contents": [
                {
                    "control": "Table",
                    "title": [{"text": "项目明细"}],
                    "value": {
                        "children": [
                            {
                                "list": [
                                    [
                                        {
                                            "control": "Text",
                                            "title": [{"text": "项目"}],
                                            "value": {"text": "筛查系统"},
                                        },
                                        {
                                            "control": "Text",
                                            "title": [{"text": "进度"}],
                                            "value": {"text": "80%"},
                                        },
                                    ]
                                ]
                            }
                        ]
                    },
                }
            ]
        },
        attachments,
    )
    assert "筛查系统" in text and "80%" in text


def test_unknown_control_does_not_crash() -> None:
    text = render_apply_data(
        {"contents": [{"control": "FutureControl", "title": "x", "value": {}}]}, []
    )
    assert text == ""


def test_malformed_control_is_isolated() -> None:
    """单个控件结构异常不应导致整份周报解析失败。"""
    text = render_apply_data(
        {
            "contents": [
                {"control": "Text", "title": [{"text": "正常"}], "value": {"text": "内容"}},
                {"control": "Text", "value": "not-a-dict"},  # 故意畸形
            ]
        },
        [],
    )
    assert "内容" in text


def test_report_type_detection() -> None:
    assert detect_report_type("本周工作汇报") == "weekly"
    assert detect_report_type("工作日报") == "daily"
    assert detect_report_type("月度总结") == "monthly"
    assert detect_report_type("") == "other"
    assert detect_report_type(None) == "other"


def test_strip_html_and_clean_text() -> None:
    assert strip_html("<p>a<br/>b</p>") == "a\nb"
    assert clean_text("a\u200b\r\n\r\n\r\nb   ") == "a\n\nb"
    assert clean_text(None) == ""
