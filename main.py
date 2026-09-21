from __future__ import annotations

import asyncio
import importlib.util
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .src.engine import GamePhase, LiarDiceError, LiarDiceGame, RoundResult
    from .src.qqofficial import (
        ButtonSpec,
        extract_context,
        is_qqofficial_event,
        send_group_reply,
    )
except ImportError:  # pragma: no cover - direct local import fallback
    from src.engine import GamePhase, LiarDiceError, LiarDiceGame, RoundResult
    from src.qqofficial import (
        ButtonSpec,
        extract_context,
        is_qqofficial_event,
        send_group_reply,
    )


PLUGIN_NAME = "astrbot_plugin_liar_dice"
BEIJING_TZ = timezone(timedelta(hours=8))


@dataclass
class CommandOutcome:
    """Result of one game command."""

    text: str
    game: LiarDiceGame | None = None
    buttons: list[ButtonSpec] | None = None
    error: bool = False


@register(
    PLUGIN_NAME,
    "Codex",
    "群聊吹牛骰子：创建房间、掷骰叫数、开骰加筹码和结算。",
    "1.0.0",
)
class LiarDicePlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = dict(config) if config else {}
        self.dice_per_player = self._config_int(
            "dice_per_player", 10, minimum=1, maximum=20
        )
        self.starting_chips = self._config_int("starting_chips", 1000, minimum=1)
        self.max_players = self._config_int("max_players", 8, minimum=2, maximum=12)
        self.use_texas_holdem_chips = self._config_bool("use_texas_holdem_chips", False)
        self.texas_holdem_plugin_name = self._config_str(
            "texas_holdem_plugin_name", "astrbot_plugin_official_TexasHoldem"
        )
        self.texas_chip_initial = self._config_int("texas_chip_initial", 600, minimum=0)
        self.texas_store: Any | None = None
        self.games: dict[str, LiarDiceGame] = {}
        self.group_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        """Initialize the plugin and optional TexasHoldem chip adapter."""

        if self.use_texas_holdem_chips:
            try:
                self.texas_store = self._load_texas_store()
                await self.texas_store.init_db()
                logger.info(
                    "[LiarDice] TexasHoldem chip adapter enabled: %s",
                    self.texas_store.db_path,
                )
            except Exception as exc:  # noqa: BLE001 - fall back to local chips
                self.texas_store = None
                logger.warning("[LiarDice] TexasHoldem chip adapter disabled: %s", exc)
        logger.info("[LiarDice] initialized")

    async def terminate(self) -> None:
        """Drop all in-memory games on plugin unload."""

        self.games.clear()
        self.group_locks.clear()

    # ------------------------------------------------------------------
    # Command registration
    # ------------------------------------------------------------------
    @filter.command("吹牛菜单", alias={"吹牛帮助", "吹牛"})
    async def menu_command(self, event: AstrMessageEvent):
        """Open the liar's dice menu."""

        async for result in self._handle_command(event, "menu"):
            yield result
        event.stop_event()

    @filter.command("吹牛创建", alias={"吹牛开局", "吹牛开房"})
    async def create_command(self, event: AstrMessageEvent):
        """Create a waiting room in the current group."""

        async for result in self._handle_command(event, "create"):
            yield result
        event.stop_event()

    @filter.command("吹牛加入", alias={"吹牛报名"})
    async def join_command(self, event: AstrMessageEvent):
        """Join the waiting room."""

        async for result in self._handle_command(event, "join"):
            yield result
        event.stop_event()

    @filter.command("吹牛退出", alias={"吹牛离开"})
    async def leave_command(self, event: AstrMessageEvent):
        """Leave the waiting room."""

        async for result in self._handle_command(event, "leave"):
            yield result
        event.stop_event()

    @filter.command("吹牛开始", alias={"吹牛掷骰", "吹牛发牌"})
    async def start_command(self, event: AstrMessageEvent):
        """Roll dice and start the game."""

        async for result in self._handle_command(event, "start"):
            yield result
        event.stop_event()

    @filter.command("吹牛看", alias={"吹牛状态"})
    async def status_command(self, event: AstrMessageEvent):
        """Show public game status."""

        async for result in self._handle_command(event, "status"):
            yield result
        event.stop_event()

    @filter.command("吹牛看骰", alias={"我的骰子", "吹牛我的骰子"})
    async def dice_command(self, event: AstrMessageEvent):
        """Show the caller's per-user dice buttons."""

        async for result in self._handle_command(event, "dice"):
            yield result
        event.stop_event()

    @filter.command("叫", alias={"吹牛叫", "叫骰"})
    async def bid_command(self, event: AstrMessageEvent):
        """Place a bid."""

        async for result in self._handle_command(event, "bid"):
            yield result
        event.stop_event()

    @filter.command("开", alias={"吹牛开"})
    async def open_command(self, event: AstrMessageEvent):
        """Open the previous bid with a wager."""

        async for result in self._handle_command(event, "open"):
            yield result
        event.stop_event()

    @filter.command("加筹码", alias={"吹牛加筹码", "加注"})
    async def raise_stake_command(self, event: AstrMessageEvent):
        """Raise the stake while being opened."""

        async for result in self._handle_command(event, "raise"):
            yield result
        event.stop_event()

    @filter.command("揭晓", alias={"吹牛揭晓", "开牌"})
    async def reveal_command(self, event: AstrMessageEvent):
        """Reveal dice and settle the round."""

        async for result in self._handle_command(event, "reveal"):
            yield result
        event.stop_event()

    @filter.command("吹牛结束", alias={"吹牛取消"})
    async def end_command(self, event: AstrMessageEvent):
        """Cancel the current room as owner or admin."""

        async for result in self._handle_command(event, "end"):
            yield result
        event.stop_event()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def _handle_command(self, event: AstrMessageEvent, command: str):
        """Run one command under a per-group lock and send its outcome."""

        group_id, user_id, name = self._identity(event)
        if not group_id:
            yield event.plain_result("吹牛骰子只能在群聊中使用。")
            return
        lock = self.group_locks.setdefault(group_id, asyncio.Lock())
        async with lock:
            try:
                outcome = await self._execute_command(
                    event, group_id, user_id, name, command
                )
            except LiarDiceError as exc:
                outcome = CommandOutcome(text=str(exc), error=True)
            except Exception as exc:  # noqa: BLE001 - keep one group isolated
                logger.exception("[LiarDice] command %s failed: %s", command, exc)
                outcome = CommandOutcome(text=f"吹牛骰子处理失败：{exc}", error=True)

        if outcome.error:
            yield event.plain_result(outcome.text)
            return
        buttons = outcome.buttons
        if buttons is None and outcome.game is not None:
            buttons = self._game_buttons(outcome.game)
        if await self._try_send_qqofficial(event, outcome.text, buttons):
            return
        if outcome.text:
            yield event.plain_result(outcome.text)

    async def _execute_command(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        name: str,
        command: str,
    ) -> CommandOutcome:
        """Execute a parsed command and return an outcome."""

        if command == "menu":
            return self._menu_outcome()
        if command == "create":
            return self._create_game(group_id, user_id, name)
        if command == "join":
            return self._join_game(group_id, user_id, name)
        if command == "leave":
            return self._leave_game(group_id, user_id)
        if command == "start":
            return await self._start_game(event, group_id, user_id)
        if command == "status":
            return self._show_status(group_id)
        if command == "dice":
            return await self._show_dice(event, group_id, user_id)
        if command == "bid":
            return self._place_bid(group_id, user_id, self._message_text(event))
        if command == "open":
            return self._open_bid(group_id, user_id, self._message_text(event))
        if command == "raise":
            return self._raise_stake(
                self.games.get(group_id), user_id, self._message_text(event)
            )
        if command == "reveal":
            return await self._reveal(group_id, user_id)
        if command == "end":
            return self._end_game(event, group_id, user_id)
        raise LiarDiceError("未知指令。")

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------
    def _create_game(self, group_id: str, user_id: str, name: str) -> CommandOutcome:
        """Create a waiting room."""

        existing = self.games.get(group_id)
        if existing is not None and existing.phase != GamePhase.FINISHED:
            raise LiarDiceError("本群已经有一局吹牛正在进行。")
        game = LiarDiceGame(
            group_id=group_id,
            owner_id=user_id,
            dice_per_player=self.dice_per_player,
            starting_chips=self.starting_chips,
            max_players=self.max_players,
        )
        game.add_player(user_id, name)
        self.games[group_id] = game
        if self.use_texas_holdem_chips:
            chip_text = "官方德州每日筹码（开始游戏时读取）"
        else:
            chip_text = f"初始筹码 {self.starting_chips}"
        return CommandOutcome(
            text=(
                "吹牛骰子房间已创建。\n"
                f"每人 {self.dice_per_player} 颗骰子，{chip_text}，"
                f"最多 {self.max_players} 人。\n"
                "其他人发送“吹牛加入”，房主发送“吹牛开始”掷骰。"
            ),
            game=game,
        )

    def _join_game(self, group_id: str, user_id: str, name: str) -> CommandOutcome:
        """Join a waiting room."""

        game = self.games.get(group_id)
        if game is None or game.phase != GamePhase.WAITING:
            raise LiarDiceError("当前没有等待加入的吹牛房间。")
        game.add_player(user_id, name)
        return CommandOutcome(
            text=(
                f"{name} 已加入，当前 {len(game.players)}/{game.max_players} 人。"
                "等待房主发送“吹牛开始”。"
            ),
            game=game,
        )

    def _leave_game(self, group_id: str, user_id: str) -> CommandOutcome:
        """Leave a waiting room."""

        game = self.games.get(group_id)
        if game is None or game.phase != GamePhase.WAITING:
            raise LiarDiceError("当前没有等待加入的吹牛房间。")
        game.remove_player(user_id)
        return CommandOutcome(text="已退出等待房间。", game=game)

    async def _start_game(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> CommandOutcome:
        """Roll dice and start the game."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        if game.phase != GamePhase.WAITING:
            raise LiarDiceError("游戏已经开始。")
        if user_id != game.owner_id and not self._is_admin(event):
            raise LiarDiceError("只有房主或管理员可以开始游戏。")

        lines = game.start_game(random.Random())
        if self.texas_store is not None:
            try:
                await self._load_texas_stacks(group_id, game)
                lines.append("已读取官方德州每日筹码作为本局筹码。")
            except Exception as exc:  # noqa: BLE001 - keep the game playable
                logger.exception("[LiarDice] load TexasHoldem chips failed: %s", exc)
                lines.append(f"读取官方德州筹码失败，本局使用默认筹码：{exc}")
        lines.append(
            "掷骰完成。请点击下方只属于你的“看骰”按钮查看自己的骰子；"
            "按钮内容只会在你的输入框出现，请勿发送到群里。"
        )
        return CommandOutcome(text="\n".join(lines), game=game)

    def _show_status(self, group_id: str) -> CommandOutcome:
        """Show public game status."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        return CommandOutcome(text="\n".join(game.status_lines()), game=game)

    async def _show_dice(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> CommandOutcome:
        """Return a per-user dice button for the caller."""

        game = self.games.get(group_id)
        if game is None or game.phase not in {GamePhase.PLAYING, GamePhase.CHALLENGE}:
            raise LiarDiceError("当前没有正在进行的吹牛游戏。")
        player = game.get_player(user_id)
        if player is None or not player.dice:
            raise LiarDiceError("你不在本局游戏中。")

        return CommandOutcome(
            text="点击下方只属于你的骰子按钮查看；看完请勿发送到群里。",
            game=game,
            buttons=self._dice_buttons(game),
        )

    def _place_bid(self, group_id: str, user_id: str, text: str) -> CommandOutcome:
        """Parse and apply a bid command."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        face, count = self._parse_bid(text)
        lines = game.place_bid(user_id, face, count)
        return CommandOutcome(text="\n".join(lines), game=game)

    def _open_bid(self, group_id: str, user_id: str, text: str) -> CommandOutcome:
        """Parse and apply an open command."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        stake = self._parse_stake(text, default=1)
        lines = game.open(user_id, stake)
        return CommandOutcome(text="\n".join(lines), game=game)

    def _raise_stake(
        self,
        game: LiarDiceGame | None,
        user_id: str,
        text: str,
    ) -> CommandOutcome:
        """Parse and apply a stake raise."""

        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        stake = self._parse_stake(text)
        lines = game.raise_stake(user_id, stake)
        return CommandOutcome(text="\n".join(lines), game=game)

    async def _reveal(self, group_id: str, user_id: str) -> CommandOutcome:
        """Reveal dice, settle the pot, and remove the finished room."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        result = game.reveal(user_id)
        self.games.pop(group_id, None)
        lines = list(result.lines)
        if self.texas_store is not None:
            try:
                await self._settle_texas_chips(group_id, result)
                lines.append("已同步官方德州每日筹码。")
            except Exception as exc:  # noqa: BLE001 - settlement already happened
                logger.exception("[LiarDice] settle TexasHoldem chips failed: %s", exc)
                lines.append(f"官方德州筹码同步失败：{exc}")
        return CommandOutcome(text="\n".join(lines), game=None, buttons=[])

    def _end_game(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> CommandOutcome:
        """Cancel a room as owner or admin."""

        game = self.games.get(group_id)
        if game is None:
            raise LiarDiceError("当前没有吹牛房间。")
        if user_id != game.owner_id and not self._is_admin(event):
            raise LiarDiceError("只有房主或管理员可以结束房间。")
        self.games.pop(group_id, None)
        return CommandOutcome(text="吹牛房间已结束。", game=None, buttons=[])

    # ------------------------------------------------------------------
    # Buttons and rendering
    # ------------------------------------------------------------------
    def _menu_outcome(self) -> CommandOutcome:
        """Build the help menu."""

        text = (
            "吹牛骰子命令：\n"
            "吹牛创建 / 吹牛加入 / 吹牛开始 / 吹牛看\n"
            "叫 <点数> <个数>      例：叫 4 3 表示猜 3 个 4\n"
            "开 <筹码>             例：开 100，双方各下注 100\n"
            "加筹码 <筹码>         被开后可以加注，开骰方不能拒绝\n"
            "揭晓                  展示全场骰子并结算\n"
            "吹牛看骰 / 吹牛结束\n\n"
            f"规则：每人 {self.dice_per_player} 颗骰子，不采用 1 点万能；"
            "开骰时若场上点数的实际数量 >= 叫数，则被开的人赢，"
            "否则开的人赢。"
        )
        return CommandOutcome(text=text, buttons=self._menu_buttons())

    def _menu_buttons(self) -> list[ButtonSpec]:
        """Build menu buttons."""

        return [
            ButtonSpec("liar_menu_create", "创建房间", "吹牛创建"),
            ButtonSpec("liar_menu_join", "加入", "吹牛加入"),
            ButtonSpec("liar_menu_start", "开始", "吹牛开始"),
            ButtonSpec("liar_menu_status", "状态", "吹牛看"),
            ButtonSpec("liar_menu_dice", "看骰", "吹牛看骰"),
            ButtonSpec("liar_menu_end", "结束", "吹牛结束"),
        ]

    def _game_buttons(self, game: LiarDiceGame | None) -> list[ButtonSpec]:
        """Build context buttons for the current room state."""

        if game is None or game.phase == GamePhase.FINISHED:
            return []
        if game.phase == GamePhase.WAITING:
            return [
                ButtonSpec("liar_wait_join", "加入", "吹牛加入"),
                ButtonSpec(
                    "liar_wait_start", "开始", "吹牛开始", only_for=game.owner_id
                ),
                ButtonSpec("liar_wait_status", "状态", "吹牛看"),
                ButtonSpec("liar_wait_end", "结束", "吹牛结束", only_for=game.owner_id),
            ]
        if game.phase == GamePhase.PLAYING:
            actor = game.current_player()
            buttons = self._dice_buttons(game)
            if actor is not None:
                buttons.extend(
                    [
                        ButtonSpec(
                            "liar_act_bid",
                            "叫",
                            "叫 [点数] [个数]",
                            only_for=actor.user_id,
                        ),
                        ButtonSpec(
                            "liar_act_open",
                            "开",
                            "开 [筹码]",
                            only_for=actor.user_id,
                        ),
                    ]
                )
            buttons.extend(
                [
                    ButtonSpec("liar_act_status", "状态", "吹牛看"),
                    ButtonSpec(
                        "liar_act_end", "结束", "吹牛结束", only_for=game.owner_id
                    ),
                ]
            )
            return buttons
        bidder = game.current_player()
        buttons = self._dice_buttons(game)
        if bidder is not None:
            buttons.extend(
                [
                    ButtonSpec(
                        "liar_challenge_raise",
                        "加筹码",
                        "加筹码 [筹码]",
                        only_for=bidder.user_id,
                    ),
                    ButtonSpec(
                        "liar_challenge_reveal",
                        "揭晓",
                        "揭晓",
                        only_for=bidder.user_id,
                    ),
                ]
            )
        buttons.extend(
            [
                ButtonSpec("liar_challenge_status", "状态", "吹牛看"),
                ButtonSpec(
                    "liar_challenge_end", "结束", "吹牛结束", only_for=game.owner_id
                ),
            ]
        )
        return buttons

    def _dice_buttons(self, game: LiarDiceGame) -> list[ButtonSpec]:
        """Build QQ Official-only individual dice reveal buttons."""

        buttons: list[ButtonSpec] = []
        for index, player in enumerate(game.players, start=1):
            if not player.dice:
                continue
            buttons.append(
                ButtonSpec(
                    f"liar_dice_{index}",
                    f"{player.name} 看骰",
                    f"吹牛看骰 {' '.join(str(value) for value in player.dice)}",
                    only_for=player.user_id,
                )
            )
        return buttons

    # ------------------------------------------------------------------
    # Platform helpers
    # ------------------------------------------------------------------
    def _identity(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        """Extract group id, user id, and display name from an event."""

        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        name = str(event.get_sender_name() or "") or f"玩家_{user_id[-6:]}"
        return group_id, user_id, name

    async def _try_send_qqofficial(
        self, event: AstrMessageEvent, text: str, buttons: list[ButtonSpec] | None
    ) -> bool:
        """Send through the QQ Official API when available."""

        if not is_qqofficial_event(event):
            return False
        context = extract_context(event)
        if context is None:
            return False
        return await send_group_reply(event, context, text, buttons or [])

    def _message_text(self, event: AstrMessageEvent) -> str:
        """Return the plain text content of an event."""

        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            return str(getter() or "")
        return str(getattr(event, "message_str", "") or "")

    # ------------------------------------------------------------------
    # Optional TexasHoldem chip adapter
    # ------------------------------------------------------------------
    def _today(self) -> str:
        """Return the current Beijing date used by TexasHoldem daily chips."""

        return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    def _load_texas_store(self) -> Any:
        """Load the official TexasHoldem SQLite store from its plugin directory.

        Returns:
            An initialized ``TexasHoldemStore``-compatible object.

        Raises:
            FileNotFoundError: If the referenced plugin or storage module is missing.
        """

        plugin_dir = Path(__file__).resolve().parents[1] / self.texas_holdem_plugin_name
        storage_file = plugin_dir / "src" / "storage.py"
        if not storage_file.is_file():
            raise FileNotFoundError(f"未找到官方德州插件存储模块：{storage_file}")
        spec = importlib.util.spec_from_file_location(
            f"{PLUGIN_NAME}_texas_storage", storage_file
        )
        if spec is None or spec.loader is None:
            raise ImportError("无法加载官方德州插件存储模块。")  # pragma: no cover
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        store_class = getattr(module, "TexasHoldemStore", None)
        if store_class is None:
            raise ImportError("官方德州插件存储模块缺少 TexasHoldemStore。")
        data_dir = Path(StarTools.get_data_dir(self.texas_holdem_plugin_name))
        return store_class(data_dir / "texas_holdem.sqlite3")

    async def _load_texas_stacks(self, group_id: str, game: LiarDiceGame) -> None:
        """Replace local game chips with each player's TexasHoldem daily balance."""

        day = self._today()
        for player in game.players:
            balance = await self.texas_store.get_daily_chips(
                day, group_id, player.user_id
            )
            if balance is None:
                balance = self.texas_chip_initial
            player.chips = int(balance)

    async def _settle_texas_chips(self, group_id: str, result: RoundResult) -> None:
        """Apply the same daily/total chip deltas as the TexasHoldem plugin."""

        day = self._today()
        entries = [
            (result.winner_id, result.winner_name, result.wager),
            (result.loser_id, result.loser_name, -result.wager),
        ]
        for user_id, name, delta in entries:
            await self.texas_store.add_daily_chips(
                day,
                group_id,
                user_id,
                delta,
                name,
                initial=self.texas_chip_initial,
            )
            await self.texas_store.add_total_delta(group_id, user_id, delta, name)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------
    def _parse_bid(self, text: str) -> tuple[int, int]:
        """Parse a bid command into ``(face, count)``.

        Supported forms:
        - ``叫 4 3``: face 4, count 3 (the documented form).
        - ``叫 3个4``: count 3, face 4 (natural Chinese form).
        - ``叫 4 3个``: face 4, count 3.
        """

        compact = re.sub(r"\s+", " ", str(text or "").strip())
        prefix = r"(?:吹牛)?叫(?:骰)?"
        patterns: list[tuple[str, Any]] = [
            (
                rf"{prefix}\s*(\d+)\s*个\s*(\d+)",
                lambda match: (int(match.group(2)), int(match.group(1))),
            ),
            (
                rf"{prefix}\s*(\d+)\s*点\s*(\d+)",
                lambda match: (int(match.group(1)), int(match.group(2))),
            ),
            (
                rf"{prefix}\s*(\d+)\s*(\d+)\s*个",
                lambda match: (int(match.group(1)), int(match.group(2))),
            ),
            (
                rf"{prefix}\s*(\d+)\s+(\d+)",
                lambda match: (int(match.group(1)), int(match.group(2))),
            ),
        ]
        for pattern, converter in patterns:
            match = re.search(pattern, compact)
            if match:
                return converter(match)
        raise LiarDiceError("格式错误，请发送“叫 <点数> <个数>”，例如：叫 4 3。")

    def _parse_stake(self, text: str, default: int | None = None) -> int:
        """Parse a positive chip amount from a command text."""

        match = re.search(r"(?:加筹码|加注|开)\s*(\d+)", str(text or ""))
        if match:
            return int(match.group(1))
        numbers = re.findall(r"\d+", str(text or ""))
        if numbers:
            return int(numbers[-1])
        if default is not None:
            return default
        raise LiarDiceError("请带上筹码数量，例如：加筹码 100。")

    def _config_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """Read, convert, and clamp an integer config value."""

        raw = self.config.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _config_bool(self, key: str, default: bool) -> bool:
        """Read a boolean config value."""

        raw = self.config.get(key, default)
        if isinstance(raw, bool):
            return raw
        return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "是", "开"}

    def _config_str(self, key: str, default: str) -> str:
        """Read a string config value."""

        raw = self.config.get(key, default)
        return str(raw if raw is not None else default)
