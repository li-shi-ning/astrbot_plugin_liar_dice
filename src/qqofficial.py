from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

try:
    from botpy.types import inline as qinline
    from botpy.types.message import MarkdownPayload
except ImportError:  # pragma: no cover - only used at runtime
    qinline = None  # type: ignore[assignment]
    MarkdownPayload = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ButtonSpec:
    """One QQ Official inline keyboard button."""

    button_id: str
    label: str
    data: str
    only_for: str | None = None


@dataclass(frozen=True)
class QQOfficialContext:
    """Identity fields needed for a QQ Official group reply."""

    platform_id: str
    group_openid: str
    member_openid: str
    display_name: str
    message_id: str
    msg_seq: int | None = None


def is_qqofficial_event(event: Any) -> bool:
    """Return whether *event* looks like a QQ Official group message event."""

    platform_name = ""
    get_platform_name = getattr(event, "get_platform_name", None)
    if callable(get_platform_name):
        try:
            platform_name = str(get_platform_name() or "").lower()
        except Exception:  # noqa: BLE001 - platform compatibility is best-effort
            platform_name = ""
    if platform_name in {"qq_official", "qq_official_webhook"}:
        return True
    api = getattr(getattr(event, "bot", None), "api", None)
    return callable(getattr(api, "post_group_message", None))


def extract_context(event: Any) -> QQOfficialContext | None:
    """Read trusted group/member identity fields from a QQ Official event."""

    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    author = getattr(raw_message, "author", None)
    group_openid = _first_non_empty_str(
        getattr(raw_message, "group_openid", None),
        _safe_call(event, "get_group_id"),
    )
    member_openid = _first_non_empty_str(
        getattr(author, "member_openid", None),
        getattr(raw_message, "member_openid", None),
        getattr(raw_message, "group_member_openid", None),
        _safe_call(event, "get_sender_id"),
    )
    if not group_openid or not member_openid:
        return None
    message_id = _first_non_empty_str(
        getattr(raw_message, "id", None),
        getattr(getattr(event, "message_obj", None), "message_id", None),
    )
    display_name = _first_non_empty_str(
        _safe_call(event, "get_sender_name"),
        getattr(author, "username", None),
    )
    return QQOfficialContext(
        platform_id=str(_safe_call(event, "get_platform_id") or ""),
        group_openid=group_openid,
        member_openid=member_openid,
        display_name=display_name or f"玩家_{member_openid[-6:]}",
        message_id=message_id or "",
        msg_seq=_as_int(getattr(raw_message, "msg_seq", None)),
    )


def build_keyboard(buttons: list[ButtonSpec]) -> Any:
    """Build a QQ Official keyboard payload."""

    if not buttons or qinline is None:
        return None
    rows = [
        {"buttons": [_build_button(spec) for spec in buttons[index : index + 3]]}
        for index in range(0, min(len(buttons), 15), 3)
    ]
    return {"content": {"rows": rows}}


def build_payload(text: str, buttons: list[ButtonSpec] | None = None) -> dict[str, Any]:
    """Build a ``post_group_message`` payload."""

    markdown = MarkdownPayload(content=text or "吹牛骰子") if MarkdownPayload else text
    return {
        "msg_type": 2,
        "markdown": markdown,
        "keyboard": build_keyboard(buttons or []),
    }


async def send_group_reply(
    event: Any,
    context: QQOfficialContext,
    text: str,
    buttons: list[ButtonSpec] | None = None,
) -> bool:
    """Send a QQ Official group Markdown message with optional buttons.

    Returns:
        ``True`` when the platform API accepted the message, otherwise
        ``False`` so the caller can fall back to ``event.plain_result``.
    """

    api = getattr(getattr(event, "bot", None), "api", None)
    post_group_message = getattr(api, "post_group_message", None)
    if not callable(post_group_message):
        return False
    payload = build_payload(text, buttons)
    if context.message_id:
        payload["msg_id"] = context.message_id
        payload["msg_seq"] = (
            context.msg_seq if context.msg_seq is not None else random.randint(1, 10000)
        )
    try:
        await post_group_message(group_openid=context.group_openid, **payload)
        return True
    except Exception as exc:  # noqa: BLE001 - network failure must not crash a game
        logger.warning("[LiarDice] send group markdown failed: %s", exc)
        return False


def _build_button(spec: ButtonSpec) -> dict[str, Any]:
    """Build one button payload with per-user visibility support."""

    permission: dict[str, Any]
    if spec.only_for:
        permission = {"type": 0, "specify_user_ids": [spec.only_for]}
    else:
        permission = {"type": 2}
    return {
        "id": spec.button_id,
        "render_data": {
            "label": spec.label,
            "visited_label": spec.label,
            "style": 1,
        },
        "action": {
            "type": 2,
            "permission": permission,
            "data": spec.data,
            "reply": True,
            "enter": False,
            "unsupport_tips": "当前客户端不支持该按钮。",
        },
    }


def _first_non_empty_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return None


def _safe_call(target: Any, name: str) -> Any:
    method = getattr(target, name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:  # noqa: BLE001 - event compatibility is best-effort
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
