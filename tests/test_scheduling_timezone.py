"""Wall-clock times from the browser, read in the operator's timezone."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


from snsauto.config import Settings
from snsauto.scheduling.worker import local_zone, parse_local_datetime, to_local

JST = ZoneInfo("Asia/Tokyo")


def settings(tz=None):
    return Settings(_env_file=None, **({"SNSAUTO_TIMEZONE": tz} if tz else {}))


class TestParsing:
    def test_a_form_time_is_the_operator_s_wall_clock(self):
        """The bug this replaces.

        `datetime-local` has no zone in it. Reading "20:00" as UTC put every
        Japanese schedule out at 05:00 the next morning - a nine-hour slip that
        looks like nothing until a client asks why the post went out at dawn.
        """
        result = parse_local_datetime("2026-09-20T20:00", settings())
        assert result.astimezone(JST).hour == 20
        assert result == datetime(2026, 9, 20, 11, 0, tzinfo=timezone.utc)

    def test_it_is_stored_in_utc(self):
        result = parse_local_datetime("2026-09-20T20:00", settings())
        assert result.tzinfo == timezone.utc

    def test_an_explicit_offset_in_the_string_is_respected(self):
        # Some clients do send one; it must not be overwritten.
        result = parse_local_datetime("2026-09-20T20:00+00:00", settings())
        assert result.astimezone(JST).hour == 5

    def test_another_timezone_is_honoured(self):
        result = parse_local_datetime("2026-09-20T20:00", settings("America/New_York"))
        assert result.astimezone(ZoneInfo("America/New_York")).hour == 20

    def test_an_empty_value_is_not_a_schedule(self):
        assert parse_local_datetime("", settings()) is None
        assert parse_local_datetime(None, settings()) is None


class TestZoneResolution:
    def test_the_default_is_jst_not_utc(self):
        assert local_zone(settings()).utcoffset(datetime(2026, 1, 1)).total_seconds() == 9 * 3600

    def test_an_unknown_zone_falls_back_to_jst(self):
        assert local_zone(settings("Mars/Olympus")).utcoffset(
            datetime(2026, 1, 1)
        ).total_seconds() == 9 * 3600


class TestDisplay:
    def test_a_stored_time_reads_back_as_local(self):
        """A schedule shown in UTC cannot be checked by a human."""
        stored = datetime(2026, 9, 20, 11, 0)      # naive UTC, as the DB holds it
        assert to_local(stored, settings()).hour == 20

    def test_a_round_trip_preserves_the_wall_clock(self):
        for wall in ("2026-01-05T07:30", "2026-07-20T23:45", "2026-12-31T00:15"):
            stored = parse_local_datetime(wall, settings())
            shown = to_local(stored.replace(tzinfo=None), settings())
            assert shown.strftime("%Y-%m-%dT%H:%M") == wall

    def test_nothing_in_gives_nothing_out(self):
        assert to_local(None, settings()) is None
