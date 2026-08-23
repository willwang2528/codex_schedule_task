#!/usr/bin/env python3
"""Validate semantic notification cards and render safe Feishu Card 2.0 JSON."""

from __future__ import annotations

import copy
import html
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse


ALLOWED_THEMES = {
    "blue",
    "wathet",
    "turquoise",
    "green",
    "yellow",
    "orange",
    "red",
    "carmine",
    "violet",
    "purple",
    "indigo",
    "grey",
}
ALLOWED_TEMPLATES = {"research_item", "market_dashboard", "price_alert"}
PRESENTATION_RULES: Dict[str, Tuple[str, int, int]] = {
    "research_top5_cards": ("research_item", 5, 5),
    "market_dashboard_card": ("market_dashboard", 3, 3),
    "price_alert_cards": ("price_alert", 1, 5),
}
THEME_STYLE = {
    "blue": ("blue-50", "blue"),
    "wathet": ("wathet-50", "wathet"),
    "turquoise": ("turquoise-50", "turquoise"),
    "green": ("green-50", "green"),
    "yellow": ("yellow-50", "yellow"),
    "orange": ("orange-50", "orange"),
    "red": ("red-50", "red"),
    "carmine": ("carmine-50", "carmine"),
    "violet": ("violet-50", "violet"),
    "purple": ("purple-50", "purple"),
    "indigo": ("indigo-50", "indigo"),
    "grey": ("grey-50", "grey"),
}
TEMPLATE_ICONS = {
    "research_item": "myai_colorful",
    "market_dashboard": "chart_colorful",
    "price_alert": "notice_colorful",
}
PROFILE_INSTRUCTIONS = {
    "research_top5_cards": (
        "For SUCCESS_NOTIFY, notification.cards must contain exactly five "
        "research_item cards in the same rank order as the Top 5. Use one card per "
        "paper or verified research release: focus.value is #1 through #5; fields "
        "are labelled 日期, 状态, Agent Memory 相关性, and 入选理由. Each card "
        "has exactly five ordered sections: one visible 中文摘要 followed by four "
        "collapsed sections titled 现存问题, 已有方法的不足, "
        "当前方法为什么可行, and 未来展望. The Chinese summary explains "
        "the problem, mechanism, strongest evidence, and material boundary in 3-4 "
        "sentences. Keep every claim grounded in the primary source; clearly label "
        "editor inference in 未来展望 and state when evidence is insufficient. "
        "source is the verified primary URL; image_url is a verified HTTPS research "
        "or project image when available, otherwise an empty string."
    ),
    "market_dashboard_card": (
        "For a normal trading-session SUCCESS_NOTIFY, notification.cards must contain "
        "exactly three market_dashboard cards in this order: (1) tag 盘面总览 using "
        "theme red for a clear broad rise, green for a clear broad fall, blue for a "
        "mixed/flat market, or grey when evidence is insufficient; (2) tag 情绪与主线 "
        "using red for broad money-making, green for broad loss effect, orange for "
        "rotation/divergence, or grey when evidence is insufficient; and (3) tag "
        "异动与风险 with theme yellow. Card 1 visible fields must include 上证指数, 深证成指, "
        "创业板指, 科创50, 沪深成交额, 上涨 / 下跌 / 平盘, and 涨停 / 跌停 / 炸板. "
        "Each card has exactly the three ordered sections required for its tag: "
        "盘面结论 / 指数与阶段变化 / 数据口径与核验; 情绪与主线结论 / "
        "梯队、代表股与驱动 / 持续性与证据边界; or 风险与验证结论 / "
        "异动与风险证据 / 待验证清单与数据口径. The first section is visible and "
        "answers the investor's main question in 2-3 short Chinese sentences; the other "
        "two sections are collapsed details. Put the most decision-relevant metric in "
        "focus, retain one verified source URL per card, and use Chinese-market red-up "
        "green-down semantics. Never infer a strong market from one isolated metric or "
        "replace non-empty deterministic market evidence with a blanket unverifiable label."
    ),
    "price_alert_cards": (
        "For every successful daily run, notification.cards must contain one to five "
        "price_alert cards covering verified alerts, baselines, unchanged offers, or "
        "source-check status. The trustworthy actual price or the explicit monitoring "
        "status is the focus; fields distinguish SKU, channel, offer conditions, "
        "baseline and historical low; sections explain change and credibility; source "
        "is an exact checked offer or official storefront URL. Never merge materially "
        "different offer conditions into one card."
    ),
}

RESEARCH_SECTION_TITLES = (
    "中文摘要",
    "现存问题",
    "已有方法的不足",
    "当前方法为什么可行",
    "未来展望",
)
RESEARCH_REQUIRED_FIELD_LABELS = {
    "日期",
    "状态",
    "Agent Memory 相关性",
    "入选理由",
}
MARKET_SECTION_TITLES_BY_TAG = {
    "盘面总览": (
        "盘面结论",
        "指数与阶段变化",
        "数据口径与核验",
    ),
    "情绪与主线": (
        "情绪与主线结论",
        "梯队、代表股与驱动",
        "持续性与证据边界",
    ),
    "异动与风险": (
        "风险与验证结论",
        "异动与风险证据",
        "待验证清单与数据口径",
    ),
}
MARKET_ALLOWED_THEMES_BY_TAG = {
    "盘面总览": {"red", "green", "blue", "grey"},
    "情绪与主线": {"red", "green", "orange", "grey"},
    "异动与风险": {"yellow"},
}

# Card 2.0 component fields used by this renderer. Keep this allowlist aligned
# with the per-component Feishu documentation. It intentionally excludes
# style-guide-only examples that are not accepted by the production API.
RENDERED_COMPONENT_FIELDS = {
    "img": {
        "tag",
        "img_key",
        "alt",
        "title",
        "scale_type",
        "size",
        "corner_radius",
        "transparent",
        "preview",
        "margin",
        "element_id",
    },
    "markdown": {
        "tag",
        "content",
        "text_size",
        "text_align",
        "icon",
        "margin",
        "element_id",
    },
    "div": {
        "tag",
        "text",
        "fields",
        "icon",
        "width",
        "margin",
        "element_id",
    },
    "column_set": {
        "tag",
        "columns",
        "flex_mode",
        "horizontal_spacing",
        "horizontal_align",
        "background_style",
        "action",
        "margin",
        "element_id",
    },
    "column": {
        "tag",
        "elements",
        "width",
        "weight",
        "vertical_align",
        "direction",
        "horizontal_spacing",
        "vertical_spacing",
        "padding",
        "margin",
        "background_style",
        "action",
    },
    "collapsible_panel": {
        "tag",
        "header",
        "elements",
        "expanded",
        "background_color",
        "border",
        "direction",
        "vertical_spacing",
        "horizontal_spacing",
        "padding",
        "margin",
    },
    "button": {
        "tag",
        "text",
        "type",
        "size",
        "width",
        "behaviors",
        "icon",
        "hover_tips",
        "disabled",
        "disabled_tips",
        "confirm",
        "margin",
        "element_id",
    },
}


class CardSpecError(ValueError):
    """Raised when Agent-proposed semantic card data is unsafe or inconsistent."""


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise CardSpecError(f"{path} must be a string")
    return value


def _valid_url(value: str, *, https_only: bool = False) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    allowed = {"https"} if https_only else {"http", "https"}
    return parsed.scheme in allowed and bool(parsed.netloc) and not parsed.username


def validate_card_specs(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise CardSpecError("notification.cards must be an array")
    if len(value) > 5:
        raise CardSpecError("notification.cards cannot contain more than five cards")
    validated: List[Dict[str, Any]] = []
    required = {
        "template",
        "title",
        "subtitle",
        "theme",
        "tag",
        "focus",
        "fields",
        "sections",
        "source",
        "image_url",
    }
    for index, item in enumerate(value):
        path = f"notification.cards[{index}]"
        if not isinstance(item, dict):
            raise CardSpecError(f"{path} must be an object")
        missing = sorted(required.difference(item))
        if missing:
            raise CardSpecError(f"{path} missing fields: {', '.join(missing)}")
        unknown = sorted(set(item).difference(required))
        if unknown:
            raise CardSpecError(f"{path} has unknown fields: {', '.join(unknown)}")
        template = _require_string(item["template"], f"{path}.template")
        if template not in ALLOWED_TEMPLATES:
            raise CardSpecError(f"{path}.template is invalid")
        theme = _require_string(item["theme"], f"{path}.theme")
        if theme not in ALLOWED_THEMES:
            raise CardSpecError(f"{path}.theme is invalid")
        for key in ("title", "subtitle", "tag", "image_url"):
            _require_string(item[key], f"{path}.{key}")
        if not item["title"].strip():
            raise CardSpecError(f"{path}.title must not be empty")
        if not _valid_url(item["image_url"], https_only=True):
            raise CardSpecError(f"{path}.image_url must be empty or an HTTPS URL")

        focus = item["focus"]
        if not isinstance(focus, dict) or set(focus) != {"value", "label"}:
            raise CardSpecError(f"{path}.focus must contain value and label")
        for key in ("value", "label"):
            _require_string(focus[key], f"{path}.focus.{key}")
        if not focus["value"].strip() or not focus["label"].strip():
            raise CardSpecError(f"{path}.focus fields must not be empty")

        fields = item["fields"]
        if not isinstance(fields, list) or len(fields) > 8:
            raise CardSpecError(f"{path}.fields must be an array with at most 8 items")
        for field_index, field in enumerate(fields):
            field_path = f"{path}.fields[{field_index}]"
            if not isinstance(field, dict) or set(field) != {"label", "value", "short"}:
                raise CardSpecError(
                    f"{field_path} must contain label, value, and short"
                )
            _require_string(field["label"], f"{field_path}.label")
            _require_string(field["value"], f"{field_path}.value")
            if not isinstance(field["short"], bool):
                raise CardSpecError(f"{field_path}.short must be boolean")

        sections = item["sections"]
        if not isinstance(sections, list) or not 1 <= len(sections) <= 5:
            raise CardSpecError(f"{path}.sections must contain 1 to 5 items")
        for section_index, section in enumerate(sections):
            section_path = f"{path}.sections[{section_index}]"
            if not isinstance(section, dict) or set(section) != {
                "title",
                "content",
                "collapsed",
            }:
                raise CardSpecError(
                    f"{section_path} must contain title, content, and collapsed"
                )
            _require_string(section["title"], f"{section_path}.title")
            _require_string(section["content"], f"{section_path}.content")
            if not isinstance(section["collapsed"], bool):
                raise CardSpecError(f"{section_path}.collapsed must be boolean")

        source = item["source"]
        if not isinstance(source, dict) or set(source) != {"label", "url"}:
            raise CardSpecError(f"{path}.source must contain label and url")
        _require_string(source["label"], f"{path}.source.label")
        source_url = _require_string(source["url"], f"{path}.source.url")
        if not _valid_url(source_url):
            raise CardSpecError(f"{path}.source.url must be empty or HTTP(S)")
        validated.append(copy.deepcopy(item))
    return validated


def validate_presentation(
    presentation: str, cards: List[Dict[str, Any]], *, should_notify: bool
) -> None:
    if not should_notify:
        if cards:
            raise CardSpecError("non-notifying results cannot contain cards")
        return
    if presentation in {"", "post"}:
        if cards:
            raise CardSpecError("post presentation cannot contain cards")
        return
    rule = PRESENTATION_RULES.get(presentation)
    if rule is None:
        raise CardSpecError("delivery.presentation is invalid")
    template, minimum, maximum = rule
    if not minimum <= len(cards) <= maximum:
        expected = str(minimum) if minimum == maximum else f"{minimum}-{maximum}"
        raise CardSpecError(
            f"{presentation} requires {expected} cards, received {len(cards)}"
        )
    if any(card["template"] != template for card in cards):
        raise CardSpecError(f"{presentation} requires template {template}")
    for index, card in enumerate(cards, start=1):
        if not card["tag"].strip():
            raise CardSpecError(f"{presentation} card {index} requires a tag")
        if not card["fields"]:
            raise CardSpecError(f"{presentation} card {index} requires fields")
        if not card["source"]["url"]:
            raise CardSpecError(
                f"{presentation} card {index} requires a primary source URL"
            )
        if any(
            not section["title"].strip() or not section["content"].strip()
            for section in card["sections"]
        ):
            raise CardSpecError(
                f"{presentation} card {index} sections cannot be empty"
            )
    if presentation == "research_top5_cards":
        for rank, card in enumerate(cards, start=1):
            if card["focus"]["value"].strip() != f"#{rank}":
                raise CardSpecError(
                    "research_top5_cards focus values must be #1 through #5 in order"
                )
            sections = card["sections"]
            titles = tuple(section["title"].strip() for section in sections)
            if titles != RESEARCH_SECTION_TITLES:
                raise CardSpecError(
                    "research_top5_cards sections must be ordered as: "
                    + ", ".join(RESEARCH_SECTION_TITLES)
                )
            if sections[0]["collapsed"] or any(
                not section["collapsed"] for section in sections[1:]
            ):
                raise CardSpecError(
                    "research_top5_cards requires one visible Chinese summary and four collapsed detail sections"
                )
            chinese_characters = re.findall(
                r"[\u3400-\u4dbf\u4e00-\u9fff]", sections[0]["content"]
            )
            if len(chinese_characters) < 20:
                raise CardSpecError(
                    f"research_top5_cards card {rank} requires a substantive Chinese summary"
                )
            field_labels = {field["label"].strip() for field in card["fields"]}
            missing_labels = sorted(
                RESEARCH_REQUIRED_FIELD_LABELS.difference(field_labels)
            )
            if missing_labels:
                raise CardSpecError(
                    f"research_top5_cards card {rank} missing fields: "
                    + ", ".join(missing_labels)
                )
    if presentation == "market_dashboard_card":
        expected_tags = ("盘面总览", "情绪与主线", "异动与风险")
        for index, (card, expected_tag) in enumerate(
            zip(cards, expected_tags), start=1
        ):
            if card["tag"] != expected_tag:
                raise CardSpecError(
                    f"market_dashboard_card card {index} tag must be {expected_tag}"
                )
            sections = card["sections"]
            if len(sections) != 3 or sections[0]["collapsed"] or any(
                not section["collapsed"] for section in sections[1:]
            ):
                raise CardSpecError(
                    f"market_dashboard_card card {index} requires one visible and two collapsed sections"
                )
            titles = tuple(section["title"].strip() for section in sections)
            expected_titles = MARKET_SECTION_TITLES_BY_TAG[expected_tag]
            if titles != expected_titles:
                raise CardSpecError(
                    f"market_dashboard_card card {index} sections must be ordered as: "
                    + ", ".join(expected_titles)
                )
            allowed_themes = MARKET_ALLOWED_THEMES_BY_TAG[expected_tag]
            if card["theme"] not in allowed_themes:
                allowed = ", ".join(sorted(allowed_themes))
                raise CardSpecError(
                    f"market_dashboard_card {expected_tag} theme must be one of: {allowed}"
                )
        overview_labels = {field["label"] for field in cards[0]["fields"]}
        required_overview_labels = {
            "上证指数",
            "深证成指",
            "创业板指",
            "科创50",
            "沪深成交额",
            "上涨 / 下跌 / 平盘",
            "涨停 / 跌停 / 炸板",
        }
        missing_labels = sorted(required_overview_labels.difference(overview_labels))
        if missing_labels:
            raise CardSpecError(
                "market_dashboard_card overview missing visible fields: "
                + ", ".join(missing_labels)
            )
    if presentation == "price_alert_cards":
        for index, card in enumerate(cards, start=1):
            if len(card["sections"]) > 3:
                raise CardSpecError(
                    f"price_alert_cards card {index} supports at most three sections"
                )


def presentation_instruction(presentation: str) -> str:
    if presentation in {"", "post"}:
        return "notification.cards must be an empty array; delivery uses a rich-text post."
    instruction = PROFILE_INSTRUCTIONS.get(presentation)
    if instruction is None:
        raise CardSpecError("delivery.presentation is invalid")
    return instruction


def _clip(value: str, limit: int, *, preserve_newlines: bool = False) -> str:
    if preserve_newlines:
        text = "\n".join(
            " ".join(line.split()) for line in value.strip().splitlines()
        ).strip()
    else:
        text = " ".join(value.strip().split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _md(value: str, limit: int) -> str:
    escaped = html.escape(
        _clip(value, limit, preserve_newlines=True), quote=False
    )
    for character in ("\\", "`", "*", "~", "[", "]", "(", ")", "#", ":"):
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def _validate_rendered_element(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        raise CardSpecError(f"{path} must be an object")
    tag = value.get("tag")
    if tag not in RENDERED_COMPONENT_FIELDS:
        raise CardSpecError(f"{path}.tag is not supported by the card renderer")
    unknown = sorted(set(value).difference(RENDERED_COMPONENT_FIELDS[tag]))
    if unknown:
        raise CardSpecError(
            f"{path} has unsupported {tag} fields: {', '.join(unknown)}"
        )
    if tag == "column_set":
        columns = value.get("columns")
        if not isinstance(columns, list) or not columns:
            raise CardSpecError(f"{path}.columns must be a non-empty array")
        for index, column in enumerate(columns):
            _validate_rendered_element(column, f"{path}.columns[{index}]")
            if column.get("tag") != "column":
                raise CardSpecError(f"{path}.columns[{index}] must be a column")
    if tag in {"column", "collapsible_panel"}:
        elements = value.get("elements")
        if not isinstance(elements, list):
            raise CardSpecError(f"{path}.elements must be an array")
        for index, element in enumerate(elements):
            _validate_rendered_element(element, f"{path}.elements[{index}]")


def validate_rendered_card(value: Any) -> Dict[str, Any]:
    """Reject unsupported component fields before a card reaches Feishu."""

    if not isinstance(value, dict):
        raise CardSpecError("rendered card must be an object")
    if set(value).difference({"schema", "config", "header", "body", "card_link"}):
        raise CardSpecError("rendered card has unsupported root fields")
    if value.get("schema") != "2.0":
        raise CardSpecError("rendered card must use schema 2.0")
    body = value.get("body")
    if not isinstance(body, dict):
        raise CardSpecError("rendered card body must be an object")
    allowed_body_fields = {
        "direction",
        "padding",
        "horizontal_spacing",
        "vertical_spacing",
        "horizontal_align",
        "vertical_align",
        "elements",
    }
    unknown_body = sorted(set(body).difference(allowed_body_fields))
    if unknown_body:
        raise CardSpecError(
            "rendered card body has unsupported fields: "
            + ", ".join(unknown_body)
        )
    elements = body.get("elements")
    if not isinstance(elements, list):
        raise CardSpecError("rendered card body.elements must be an array")
    for index, element in enumerate(elements):
        _validate_rendered_element(element, f"rendered card body.elements[{index}]")
    return value


def render_card(spec: Dict[str, Any], *, image_key: str = "") -> Dict[str, Any]:
    """Render one previously validated semantic spec into Card 2.0 JSON."""

    card = validate_card_specs([spec])[0]
    theme = card["theme"]
    background, foreground = THEME_STYLE[theme]
    elements: List[Dict[str, Any]] = []
    if image_key:
        elements.append(
            {
                "tag": "img",
                "img_key": image_key,
                "alt": {
                    "tag": "plain_text",
                    "content": _clip(card["title"], 100),
                },
                "scale_type": "fit_horizontal",
                "corner_radius": "8px",
                "preview": True,
                "margin": "0px 0px 12px 0px",
            }
        )

    focus = card["focus"]
    focus_elements: List[Dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"## <font color='{foreground}'>{_md(focus['value'], 60)}</font>",
            "text_align": "center",
        },
        {
            "tag": "markdown",
            "content": f"<font color='grey'>{_md(focus['label'], 80)}</font>",
            "text_align": "center",
            "text_size": "notation",
        },
    ]
    if card["fields"]:
        focus_elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": bool(field["short"]),
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**{_md(field['label'], 40)}**\n"
                                f"{_md(field['value'], 180)}"
                            ),
                        },
                    }
                    for field in card["fields"][:8]
                ],
            }
        )
    focus_block = {
        "tag": "column_set",
        "flex_mode": "none",
        "margin": "0px 0px 12px 0px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": background,
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": focus_elements,
            }
        ],
    }

    sections = card["sections"]
    primary = next((item for item in sections if not item["collapsed"]), sections[0])
    primary_block = {
        "tag": "markdown",
        "content": (
            f"**{_md(primary['title'], 60)}**\n"
            f"{_md(primary['content'], 900)}"
        ),
        "text_size": "normal",
        "margin": "0px 0px 12px 0px",
    }
    if card["template"] in {"research_item", "market_dashboard"}:
        elements.extend((primary_block, focus_block))
    else:
        elements.extend((focus_block, primary_block))
    secondary = [item for item in sections if item is not primary]
    if secondary:
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "background_color": "grey-50",
                "border": {"color": "grey-200", "corner_radius": "8px"},
                "padding": "8px 12px 8px 12px",
                "margin": "0px 0px 12px 0px",
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": (
                            "论文详解（点击展开/收起）"
                            if card["template"] == "research_item"
                            else (
                                "证据详情（点击展开/收起）"
                                if card["template"] == "market_dashboard"
                                else "详细数据（点击展开/收起）"
                            )
                        ),
                    },
                    "width": "fill",
                    "icon": {
                        "tag": "standard_icon",
                        "token": "down_outlined",
                    },
                    "icon_position": "right",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**{_md(item['title'], 60)}**\n"
                            f"{_md(item['content'], 900)}"
                        ),
                    }
                    for item in secondary
                ],
            }
        )

    source = card["source"]
    if source["url"]:
        elements.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": _clip(source["label"] or "查看主要来源", 100),
                },
                "type": "primary_filled",
                "width": "fill",
                "behaviors": [
                    {"type": "open_url", "default_url": source["url"]}
                ],
            }
        )

    rendered = {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "enable_forward": True,
            "summary": {"content": _clip(card["title"], 100)},
            "style": {
                "text_size": {
                    "caption": {
                        "default": "notation",
                        "pc": "notation",
                        "mobile": "notation",
                    }
                }
            },
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": _clip(card["title"], 100),
            },
            "subtitle": {
                "tag": "plain_text",
                "content": _clip(card["subtitle"], 120),
            },
            "template": theme,
            "icon": {
                "tag": "standard_icon",
                "token": TEMPLATE_ICONS[card["template"]],
            },
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {
                        "tag": "plain_text",
                        "content": _clip(card["tag"], 30),
                    },
                    "color": "neutral" if theme == "grey" else theme,
                }
            ]
            if card["tag"]
            else [],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "0px",
            "elements": elements,
        },
    }
    return validate_rendered_card(rendered)
