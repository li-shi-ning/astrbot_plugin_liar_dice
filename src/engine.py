from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

DEFAULT_DICE_PER_PLAYER = 10
DEFAULT_STARTING_CHIPS = 1000
DEFAULT_MAX_PLAYERS = 8


class LiarDiceError(ValueError):
    """Raised when a liar's dice action is not legal."""


class GamePhase(str, Enum):
    WAITING = "waiting"
    PLAYING = "playing"
    CHALLENGE = "challenge"
    FINISHED = "finished"


@dataclass(frozen=True)
class Bid:
    user_id: str
    user_name: str
    face: int
    count: int

    def label(self) -> str:
        """Return a human-readable bid label."""

        return f"{self.count} 个 {self.face}"


@dataclass
class PlayerState:
    user_id: str
    name: str
    dice: list[int] = field(default_factory=list)
    chips: int = DEFAULT_STARTING_CHIPS

    def dice_text(self) -> str:
        """Format this player's dice as a space-separated string."""

        return " ".join(str(value) for value in self.dice)


@dataclass
class RoundResult:
    winner_id: str
    winner_name: str
    loser_id: str
    loser_name: str
    bidder_id: str
    opener_id: str
    face: int
    bid_count: int
    actual_count: int
    wager: int
    bidder_won: bool
    dice_by_player: dict[str, list[int]]
    lines: list[str]


@dataclass
class LiarDiceGame:
    group_id: str
    owner_id: str
    dice_per_player: int = DEFAULT_DICE_PER_PLAYER
    starting_chips: int = DEFAULT_STARTING_CHIPS
    max_players: int = DEFAULT_MAX_PLAYERS
    players: list[PlayerState] = field(default_factory=list)
    phase: GamePhase = GamePhase.WAITING
    current_index: int = 0
    current_bid: Bid | None = None
    bidder_index: int | None = None
    opener_index: int | None = None
    wager: int = 0
    raise_count: int = 0
    hand_no: int = 0

    @property
    def total_dice(self) -> int:
        """Return the number of dice currently on the table."""

        return len(self.players) * self.dice_per_player

    def get_player(self, user_id: str) -> PlayerState | None:
        """Return a player by user id, or ``None`` when the player is absent."""

        return next(
            (player for player in self.players if player.user_id == user_id), None
        )

    def current_player(self) -> PlayerState | None:
        """Return the player whose turn it is, if any."""

        if self.phase not in {GamePhase.PLAYING, GamePhase.CHALLENGE}:
            return None
        if not self.players:
            return None
        return self.players[self.current_index % len(self.players)]

    def add_player(self, user_id: str, name: str) -> None:
        """Add a player to a waiting room.

        Args:
            user_id: Stable platform user id.
            name: Display name shown in game messages.

        Raises:
            LiarDiceError: If the room is not waiting, is full, or the player
                has already joined.
        """

        if self.phase != GamePhase.WAITING:
            raise LiarDiceError("游戏已经开始，不能加入。")
        if self.get_player(user_id) is not None:
            raise LiarDiceError("你已经加入本局。")
        if len(self.players) >= self.max_players:
            raise LiarDiceError("房间人数已满。")
        self.players.append(
            PlayerState(
                user_id=user_id,
                name=name,
                chips=self.starting_chips,
            )
        )

    def remove_player(self, user_id: str) -> None:
        """Remove a player from a waiting room."""

        if self.phase != GamePhase.WAITING:
            raise LiarDiceError("游戏已经开始，不能退出。")
        player = self.get_player(user_id)
        if player is None:
            raise LiarDiceError("你还没有加入本局。")
        if player.user_id == self.owner_id:
            raise LiarDiceError("房主不能退出，请直接结束房间。")
        self.players.remove(player)

    def start_game(self, rng: random.Random | None = None) -> list[str]:
        """Roll dice, shuffle the table, and choose a random first bidder.

        Args:
            rng: Optional random source for deterministic tests.

        Returns:
            Public message lines describing the start of the game.

        Raises:
            LiarDiceError: If fewer than two players have joined.
        """

        if self.phase != GamePhase.WAITING:
            raise LiarDiceError("游戏已经开始。")
        if len(self.players) < 2:
            raise LiarDiceError("至少需要两名玩家才能开始游戏。")
        rng = rng or random.Random()
        rng.shuffle(self.players)
        for player in self.players:
            player.dice = [rng.randint(1, 6) for _ in range(self.dice_per_player)]
            player.chips = self.starting_chips
        self.phase = GamePhase.PLAYING
        self.current_index = 0
        self.current_bid = None
        self.bidder_index = None
        self.opener_index = None
        self.wager = 0
        self.raise_count = 0
        self.hand_no += 1
        first = self.players[0]
        order = "、".join(player.name for player in self.players)
        return [
            f"每人 {self.dice_per_player} 颗骰子已经掷好，本局不采用 1 点万能。",
            f"行动顺序：{order}",
            f"当前先手：{first.name}。",
            f"请 {first.name} 发送“叫 <点数> <个数>”开始叫骰。",
        ]

    def place_bid(self, user_id: str, face: int, count: int) -> list[str]:
        """Place a bid on the current turn.

        The bid means "there are at least ``count`` dice showing ``face`` on
        the whole table".  Later bids must be strictly greater in
        ``(count, face)`` order.

        Args:
            user_id: The acting player.
            face: Number from 1 to 6.
            count: Positive number of dice, at most ``total_dice``.

        Returns:
            Message lines to send to the group.

        Raises:
            LiarDiceError: If the action is not legal.
        """

        if self.phase != GamePhase.PLAYING:
            raise LiarDiceError("当前不能叫骰。")
        player = self.current_player()
        if player is None or player.user_id != user_id:
            raise LiarDiceError("还没有轮到你叫骰。")
        if not 1 <= int(face) <= 6:
            raise LiarDiceError("点数必须是 1-6。")
        if not 1 <= int(count) <= self.total_dice:
            raise LiarDiceError(f"个数必须在 1-{self.total_dice} 之间。")
        if self.current_bid is not None:
            old = (self.current_bid.count, self.current_bid.face)
            new = (int(count), int(face))
            if new <= old:
                raise LiarDiceError(
                    "新叫骰必须比上家大：个数更多，或个数相同但点数更大。"
                    f" 当前是 {self.current_bid.label()}。"
                )

        self.current_bid = Bid(
            user_id=player.user_id,
            user_name=player.name,
            face=int(face),
            count=int(count),
        )
        self.bidder_index = self.current_index
        self.current_index = self._next_index(self.current_index)
        next_player = self.current_player()
        lines = [f"{player.name} 叫 {self.current_bid.label()}。"]
        if next_player is not None:
            lines.append(
                f"轮到 {next_player.name} 行动：可以继续叫，或发送“开 <筹码>”。"
            )
        return lines

    def open(self, user_id: str, stake: int) -> list[str]:
        """Open the previous player's bid and start the stake-reaction phase.

        Args:
            user_id: The acting player, which must be the next player.
            stake: Chips each side will put into the pot.

        Returns:
            Message lines describing the challenge.

        Raises:
            LiarDiceError: If the action is not legal.
        """

        if self.phase != GamePhase.PLAYING:
            raise LiarDiceError("当前不能开骰。")
        if self.current_bid is None or self.bidder_index is None:
            raise LiarDiceError("还没有人叫骰，不能开。")
        player = self.current_player()
        if player is None or player.user_id != user_id:
            raise LiarDiceError("还没有轮到你开骰。")
        if int(stake) < 1:
            raise LiarDiceError("筹码必须大于 0。")

        bidder = self.players[self.bidder_index]
        self.opener_index = self.current_index
        self.wager = int(stake)
        self.raise_count = 0
        self.phase = GamePhase.CHALLENGE
        self.current_index = self.bidder_index
        return [
            f"{player.name} 开 {bidder.name} 的 {self.current_bid.label()}，"
            f"双方各下注 {self.wager} 筹码。",
            f"{bidder.name} 可以发送“加筹码 <筹码>”提高双方下注，"
            "或发送“揭晓”直接结算；加筹码后开骰方不能拒绝。",
        ]

    def challenge(self, user_id: str, stake: int) -> list[str]:
        """Alias for :meth:`open`."""

        return self.open(user_id, stake)

    def open_challenge(self, user_id: str, stake: int) -> list[str]:
        """Alias for :meth:`open`."""

        return self.open(user_id, stake)

    def raise_stake(self, user_id: str, stake: int) -> list[str]:
        """Increase the stake while the challenged bidder is reacting.

        Args:
            user_id: The challenged bidder.
            stake: New total stake per side.  Must be greater than the current
                wager.

        Returns:
            Message lines for the raise.

        Raises:
            LiarDiceError: If the action is not legal.
        """

        if self.phase != GamePhase.CHALLENGE:
            raise LiarDiceError("当前没有待加筹码的开骰。")
        player = self.current_player()
        if player is None or player.user_id != user_id:
            raise LiarDiceError("只有被开的人可以加筹码。")
        if int(stake) <= self.wager:
            raise LiarDiceError(f"加筹码必须大于当前下注 {self.wager}。")
        self.wager = int(stake)
        self.raise_count += 1
        return [
            f"{player.name} 加筹码到 {self.wager}，双方各下注 {self.wager} 筹码，"
            "开骰方不能拒绝。",
            "请发送“揭晓”结算，或继续加筹码。",
        ]

    def reveal(self, user_id: str | None = None) -> RoundResult:
        """Reveal all dice, determine the winner, and settle chips.

        Args:
            user_id: Optional acting player.  When supplied, it must be the
                challenged bidder or the opener for API compatibility.

        Returns:
            The complete settlement result.

        Raises:
            LiarDiceError: If the game is not in the challenge phase.
        """

        if self.phase != GamePhase.CHALLENGE:
            raise LiarDiceError("当前没有可以揭晓的开骰。")
        if (
            self.current_bid is None
            or self.bidder_index is None
            or self.opener_index is None
        ):
            raise LiarDiceError("开骰状态不完整。")
        bidder = self.players[self.bidder_index]
        opener = self.players[self.opener_index]
        if user_id is not None and user_id not in {bidder.user_id, opener.user_id}:
            raise LiarDiceError("只有开骰双方可以揭晓。")

        actual_count = sum(
            1
            for player in self.players
            for die in player.dice
            if die == self.current_bid.face
        )
        bidder_won = actual_count >= self.current_bid.count
        winner = bidder if bidder_won else opener
        loser = opener if bidder_won else bidder
        winner.chips += self.wager
        loser.chips -= self.wager
        self.phase = GamePhase.FINISHED

        dice_by_player = {player.user_id: list(player.dice) for player in self.players}
        lines = [
            f"揭晓：{bidder.name} 叫的是 {self.current_bid.label()}，"
            f"全场实际有 {actual_count} 个 {self.current_bid.face}。",
            "全场骰子：",
        ]
        for player in self.players:
            lines.append(f"- {player.name}：{player.dice_text()}")
        if bidder_won:
            lines.append(
                f"实际数量 {actual_count} >= 叫数 {self.current_bid.count}，"
                f"被开的 {bidder.name} 赢。"
            )
        else:
            lines.append(
                f"实际数量 {actual_count} < 叫数 {self.current_bid.count}，"
                f"开骰的 {opener.name} 赢。"
            )
        lines.append(
            f"结算：{winner.name} +{self.wager} 筹码，{loser.name} -{self.wager} 筹码。"
        )
        return RoundResult(
            winner_id=winner.user_id,
            winner_name=winner.name,
            loser_id=loser.user_id,
            loser_name=loser.name,
            bidder_id=bidder.user_id,
            opener_id=opener.user_id,
            face=self.current_bid.face,
            bid_count=self.current_bid.count,
            actual_count=actual_count,
            wager=self.wager,
            bidder_won=bidder_won,
            dice_by_player=dice_by_player,
            lines=lines,
        )

    def settle(self, user_id: str | None = None) -> RoundResult:
        """Alias for :meth:`reveal`."""

        return self.reveal(user_id)

    def bid(self, user_id: str, face: int, count: int) -> list[str]:
        """Alias for :meth:`place_bid`."""

        return self.place_bid(user_id, face, count)

    def raise_bet(self, user_id: str, stake: int) -> list[str]:
        """Alias for :meth:`raise_stake`."""

        return self.raise_stake(user_id, stake)

    def status_lines(self, reveal_dice: bool = False) -> list[str]:
        """Build public status text.

        Args:
            reveal_dice: When true, include every player's dice.  This is only
                intended for finished games or tests.

        Returns:
            Message lines describing players, chips, turn, and bid.
        """

        lines = [f"阶段：{self._phase_label()}"]
        if self.current_bid is not None:
            lines.append(
                f"当前叫骰：{self.current_bid.user_name} {self.current_bid.label()}。"
            )
        if self.phase == GamePhase.CHALLENGE:
            bidder = (
                self.players[self.bidder_index]
                if self.bidder_index is not None
                else None
            )
            opener = (
                self.players[self.opener_index]
                if self.opener_index is not None
                else None
            )
            if bidder and opener:
                lines.append(
                    f"开骰：{opener.name} 开 {bidder.name}，"
                    f"双方下注 {self.wager} 筹码。"
                )
        lines.append("玩家：")
        for player in self.players:
            dice = f"，骰子 {player.dice_text()}" if reveal_dice and player.dice else ""
            lines.append(f"- {player.name}：{player.chips} 筹码{dice}")
        actor = self.current_player()
        if actor is not None:
            lines.append(f"当前行动：{actor.name}")
        return lines

    def _next_index(self, index: int) -> int:
        """Return the next player index around the table."""

        return (index + 1) % len(self.players)

    def _phase_label(self) -> str:
        """Return a Chinese label for the current phase."""

        labels = {
            GamePhase.WAITING: "等待加入",
            GamePhase.PLAYING: "叫骰中",
            GamePhase.CHALLENGE: "开骰加筹码",
            GamePhase.FINISHED: "已结束",
        }
        return labels[self.phase]

    def to_summary(self) -> dict[str, Any]:
        """Serialize the game state for diagnostics or tests."""

        return {
            "group_id": self.group_id,
            "owner_id": self.owner_id,
            "phase": self.phase.value,
            "current_bid": None
            if self.current_bid is None
            else {
                "user_id": self.current_bid.user_id,
                "face": self.current_bid.face,
                "count": self.current_bid.count,
            },
            "wager": self.wager,
            "players": [
                {
                    "user_id": player.user_id,
                    "name": player.name,
                    "chips": player.chips,
                    "dice": list(player.dice),
                }
                for player in self.players
            ],
        }
