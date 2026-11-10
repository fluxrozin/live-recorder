"""配信者の表示順。データベースなしで並びのルールだけを確認する。"""

import pytest

from live_recorder.db.models import Streamer
from live_recorder.db.repository import RepositoryError, assign_sort_order


def test_assign_sort_order_rewrites_positions():
    rows = [Streamer(id=1), Streamer(id=2), Streamer(id=3)]
    assign_sort_order(rows, [3, 1, 2])
    assert [row.sort_order for row in rows] == [1, 2, 0]


def test_assign_sort_order_rejects_a_different_set():
    rows = [Streamer(id=1), Streamer(id=2)]
    with pytest.raises(RepositoryError):
        assign_sort_order(rows, [1])
    with pytest.raises(RepositoryError):
        assign_sort_order(rows, [1, 1])
    with pytest.raises(RepositoryError):
        assign_sort_order(rows, [1, 3])
