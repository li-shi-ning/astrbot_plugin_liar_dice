from __future__ import annotations

import random
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from src.engine import GamePhase, LiarDiceError, LiarDiceGame  # noqa: E402


def make_game(dice_per_player: int = 2) -> LiarDiceGame:
    game = LiarDiceGame(
        group_id="g",
        owner_id="a",
        dice_per_player=dice_per_player,
        starting_chips=1000,
        max_players=4,
    )
    game.add_player("a", "A")
    game.add_player("b", "B")
    return game


def start_and_fix_dice(
    game: LiarDiceGame,
    dice_a: list[int],
    dice_b: list[int],
) -> None:
    game.start_game(random.Random(0))
    by_id = {player.user_id: player for player in game.players}
    by_id["a"].dice = list(dice_a)
    by_id["b"].dice = list(dice_b)


def other_player(game: LiarDiceGame, user_id: str):
    return next(player for player in game.players if player.user_id != user_id)


def test_start_rolls_ten_dice_by_default() -> None:
    game = LiarDiceGame("g", "a", max_players=4)
    game.add_player("a", "A")
    game.add_player("b", "B")

    game.start_game(random.Random(1))

    assert game.phase == GamePhase.PLAYING
    assert len(game.players[0].dice) == 10
    assert len(game.players[1].dice) == 10
    assert game.current_player() is not None
    assert all(1 <= die <= 6 for player in game.players for die in player.dice)


def test_bid_must_be_strictly_higher() -> None:
    game = make_game()
    game.start_game(random.Random(0))
    first = game.current_player()
    assert first is not None

    game.place_bid(first.user_id, 4, 2)
    second = other_player(game, first.user_id)
    assert second is not None

    try:
        game.place_bid(second.user_id, 4, 2)
    except LiarDiceError as exc:
        assert "新叫骰必须比上家大" in str(exc)
    else:  # pragma: no cover - guard against regression
        raise AssertionError("same bid should be rejected")

    try:
        game.place_bid(first.user_id, 5, 2)
    except LiarDiceError as exc:
        assert "不能连续叫骰" in str(exc)
    else:  # pragma: no cover - guard against regression
        raise AssertionError("the current bidder should not bid twice")

    game.place_bid(second.user_id, 5, 2)
    assert game.current_bid is not None
    assert game.current_bid.face == 5
    assert game.current_bid.count == 2


def test_open_then_reveal_bidder_wins() -> None:
    game = make_game()
    start_and_fix_dice(game, [4, 4], [2, 3])
    bidder = game.current_player()
    assert bidder is not None
    game.place_bid(bidder.user_id, 4, 2)
    opener = other_player(game, bidder.user_id)
    assert opener is not None

    game.open(opener.user_id, 100)
    result = game.reveal(opener.user_id)

    assert result.actual_count == 2
    assert result.bidder_won is True
    assert result.winner_id == bidder.user_id
    assert result.loser_id == opener.user_id
    assert {p.user_id: p.chips for p in game.players}[bidder.user_id] == 1100
    assert {p.user_id: p.chips for p in game.players}[opener.user_id] == 900


def test_open_then_reveal_opener_wins_and_raise_is_valid() -> None:
    game = make_game()
    start_and_fix_dice(game, [1, 2], [3, 3])
    bidder = game.current_player()
    assert bidder is not None
    game.place_bid(bidder.user_id, 6, 2)
    opener = other_player(game, bidder.user_id)
    assert opener is not None

    game.open(opener.user_id, 50)
    game.raise_stake(bidder.user_id, 70)
    result = game.reveal(bidder.user_id)

    assert result.wager == 120
    assert result.actual_count == 0
    assert result.bidder_won is False
    assert result.wager == 120
    chips = {p.user_id: p.chips for p in game.players}
    assert chips[opener.user_id] == 1120
    assert chips[bidder.user_id] == 880


def test_ones_are_not_wild() -> None:
    game = make_game()
    start_and_fix_dice(game, [1, 1], [1, 6])
    bidder = game.current_player()
    assert bidder is not None
    game.place_bid(bidder.user_id, 6, 3)
    opener = other_player(game, bidder.user_id)
    assert opener is not None

    game.open(opener.user_id, 10)
    result = game.reveal()

    assert result.actual_count == 1
    assert result.bidder_won is False


def test_first_bid_is_starter_only_and_later_bids_ignore_seating_order() -> None:
    game = LiarDiceGame("g", "a", dice_per_player=1, max_players=4)
    game.add_player("a", "A")
    game.add_player("b", "B")
    game.add_player("c", "C")
    game.start_game(random.Random(3))

    starter = game.current_player()
    assert starter is not None
    third = game.players[2]
    second = game.players[1]

    try:
        game.place_bid(third.user_id, 1, 1)
    except LiarDiceError as exc:
        assert "第一手只能由先手" in str(exc)
    else:  # pragma: no cover - guard against regression
        raise AssertionError("non-starter must not make the first bid")

    game.place_bid(starter.user_id, 1, 1)
    game.place_bid(third.user_id, 1, 2)
    game.place_bid(second.user_id, 2, 2)
    game.place_bid(starter.user_id, 3, 2)

    assert game.current_bid is not None
    assert game.current_bid.user_id == starter.user_id
    assert game.current_bid.count == 2
    assert game.current_bid.face == 3


def test_raise_stake_adds_to_existing_wager() -> None:
    game = make_game()
    game.start_game(random.Random(0))
    bidder = game.current_player()
    assert bidder is not None
    game.place_bid(bidder.user_id, 3, 1)
    opener = other_player(game, bidder.user_id)

    game.open(opener.user_id, 100)
    lines = game.raise_stake(bidder.user_id, 25)
    assert "加筹码 25" in lines[0]
    assert "提高到 125 筹码" in lines[0]
    assert game.wager == 125

    lines = game.raise_stake(bidder.user_id, 15)
    assert "加筹码 15" in lines[0]
    assert "提高到 140 筹码" in lines[0]
    assert game.wager == 140


def test_start_message_contains_only_random_starter() -> None:
    game = make_game()
    lines = game.start_game(random.Random(0))
    assert len(lines) == 1
    assert lines[0].startswith("随机先手：")
