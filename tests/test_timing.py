"""Posting-time analysis: the hours that worked, not the hours that were busy."""

from dataclasses import dataclass
from datetime import datetime, timezone


from snsauto.research.timing import (
    MIN_BUCKET,
    describe_best,
    posting_time_analysis,
    resolve_zone,
)


@dataclass
class Rec:
    published_at: datetime | None
    engagement: float


def at(hour, engagement, day=6):
    return Rec(datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc), engagement)


def analyse(records, **kwargs):
    return posting_time_analysis(records, lambda r: r.engagement, **kwargs)


class TestItMeasuresPerformanceNotPopularity:
    def test_the_busiest_hour_is_not_the_recommended_hour(self):
        """The defect this replaces.

        Counting publication hours returns the mode of when people post. In a
        crowded niche that is usually the worst slot, because everything else
        was posted into it too.
        """
        crowded_but_bad = [at(11, 0.02) for _ in range(14)]
        quiet_but_good = [at(22, 0.11) for _ in range(5)]
        result = analyse(crowded_but_bad + quiet_but_good)

        best_hours = {entry["bucket"] for entry in result["best"]}
        assert 7 in best_hours          # 22:00 UTC is 07:00 JST
        assert 20 not in best_hours     # 11:00 UTC is 20:00 JST - the crowd

    def test_a_lucky_single_post_cannot_win_an_hour(self):
        steady = [at(9, 0.05) for _ in range(10)]
        one_fluke = [at(3, 0.90)]
        result = analyse(steady + one_fluke)

        assert all(entry["posts"] >= MIN_BUCKET for entry in result["best"])
        assert 12 not in {e["bucket"] for e in result["best"]}   # 3 UTC = 12 JST

    def test_an_hour_below_the_corpus_median_is_never_recommended(self):
        result = analyse([at(9, 0.01) for _ in range(5)] + [at(15, 0.09) for _ in range(5)])
        assert all(entry["above_median"] for entry in result["best"])


class TestHonestRefusal:
    def test_too_few_posts_per_bucket_refuses_to_answer(self):
        result = analyse([at(3, 0.05), at(9, 0.06), at(15, 0.04)])
        assert not result["usable"]
        assert str(MIN_BUCKET) in result["reason"]
        assert "判定できません" in describe_best(result)

    def test_no_timestamps_is_reported_not_guessed(self):
        result = analyse([Rec(None, 0.05), Rec(None, 0.06)])
        assert not result["usable"]
        assert "投稿日時" in result["reason"]

    def test_flat_performance_says_there_is_no_pattern(self):
        result = analyse([at(9, 0.05) for _ in range(6)] + [at(15, 0.05) for _ in range(6)])
        assert describe_best(result).startswith("どの時間帯も")

    def test_it_falls_back_to_dayparts_when_hours_are_too_thin(self):
        # Six posts spread over three adjacent evening hours: no single hour
        # qualifies, but the evening as a whole does.
        evening = [at(9, 0.10), at(9, 0.11), at(10, 0.10), at(10, 0.12),
                   at(11, 0.11), at(11, 0.10)]
        morning = [at(21, 0.02) for _ in range(6)]
        result = analyse(evening + morning)
        assert result["grain"] == "daypart"
        assert result["usable"]


class TestTimezone:
    def test_it_reports_in_local_time_by_default(self):
        # An operator schedules against their own clock, never against UTC.
        result = analyse([at(22, 0.09) for _ in range(4)])
        assert result["timezone"] == "Asia/Tokyo"
        assert result["by_hour"]["all"][0]["bucket"] == 7

    def test_an_explicit_zone_is_honoured(self):
        result = analyse([at(22, 0.09) for _ in range(4)], timezone_name="UTC")
        assert result["by_hour"]["all"][0]["bucket"] == 22

    def test_an_unknown_zone_falls_back_to_jst_not_utc(self):
        assert resolve_zone("Mars/Olympus").utcoffset(None).total_seconds() == 9 * 3600

    def test_naive_timestamps_are_treated_as_utc(self):
        naive = Rec(datetime(2026, 9, 6, 22, 0), 0.09)
        result = analyse([naive] * 4)
        assert result["by_hour"]["all"][0]["bucket"] == 7


class TestWeekday:
    def test_weekdays_are_labelled_in_japanese(self):
        result = analyse([at(9, 0.09, day=7) for _ in range(4)])
        labels = {e.get("label") for e in result["by_weekday"]["all"]}
        assert labels <= set("月火水木金土日")
        assert labels
