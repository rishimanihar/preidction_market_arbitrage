from datetime import UTC, datetime

import pytest

from pmarb.venues.polymarket_us.maintenance import in_maintenance, next_window


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (datetime(2026, 3, 5, 11, 0, tzinfo=UTC), True),  # EST, inclusive start
        (datetime(2026, 3, 5, 13, 0, tzinfo=UTC), False),  # EST, exclusive end
        (datetime(2026, 3, 12, 10, 0, tzinfo=UTC), True),  # EDT, inclusive start
        (datetime(2026, 3, 12, 12, 0, tzinfo=UTC), False),  # EDT, exclusive end
        (datetime(2026, 10, 29, 10, 30, tzinfo=UTC), True),  # Before fall-back
        (datetime(2026, 11, 5, 11, 30, tzinfo=UTC), True),  # After fall-back
    ],
)
def test_in_maintenance_dst_boundaries(instant, expected):
    assert in_maintenance(instant) is expected


@pytest.mark.parametrize(
    ("instant", "expected_start", "expected_end"),
    [
        (
            datetime(2026, 3, 5, 11, 30, tzinfo=UTC),
            datetime(2026, 3, 5, 11, tzinfo=UTC),
            datetime(2026, 3, 5, 13, tzinfo=UTC),
        ),
        (
            datetime(2026, 3, 6, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 12, 10, tzinfo=UTC),
            datetime(2026, 3, 12, 12, tzinfo=UTC),
        ),
        (
            datetime(2026, 10, 30, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 5, 11, tzinfo=UTC),
            datetime(2026, 11, 5, 13, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 5, 13, 0, tzinfo=UTC),
            datetime(2026, 11, 12, 11, tzinfo=UTC),
            datetime(2026, 11, 12, 13, tzinfo=UTC),
        ),
    ],
)
def test_next_window_is_current_or_next(instant, expected_start, expected_end):
    assert next_window(instant) == (expected_start, expected_end)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        in_maintenance(datetime(2026, 3, 5, 11))
