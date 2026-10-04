"""Shaping the research population: search options and exclusion rules."""

from datetime import datetime, timedelta, timezone


from snsauto.models import Platform
from snsauto.platforms import PostRecord, SearchOptions
from snsauto.research.keyword import ExclusionRules, ResearchService, build_options


def post(external_id="1", author="rival", views=1000, caption="本文", title=None):
    return PostRecord(
        external_id=external_id, platform=Platform.YOUTUBE, author=author,
        views=views, caption=caption, title=title, likes=100,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


class TestSearchOptions:
    def test_a_window_becomes_an_absolute_cutoff(self):
        after = SearchOptions(published_within_days=30).published_after()
        assert after is not None
        assert 29 < (datetime.now(timezone.utc) - after).days + 1 < 32

    def test_no_window_means_no_cutoff(self):
        assert SearchOptions().published_after() is None

    def test_options_a_platform_cannot_express_are_reported_as_ignored(self):
        # Instagram's hashtag endpoints take no filters at all. Silently
        # dropping them would let a report imply a date range that never
        # reached the API.
        options = SearchOptions(published_within_days=7, video_duration="short")
        assert options.applied(set()) == {
            "applied": {},
            "ignored": {"published_within_days": 7, "video_duration": "short"},
        }

    def test_the_default_order_is_not_reported_as_a_filter(self):
        assert SearchOptions().applied({"order"})["applied"] == {}

    def test_settings_with_no_overrides_change_nothing(self):
        class Bare:
            research_published_within_days = None
            research_video_duration = None
            research_order = "relevance"

        assert build_options(Bare()) == SearchOptions()


class TestExclusionRules:
    def test_your_own_account_is_dropped_from_the_benchmark(self):
        # Leaving it in means the corpus you compare yourself against contains
        # you, and moves every time you post.
        rules = ExclusionRules(authors="@mybrand, other")
        assert rules.reason(post(author="mybrand")) == "excluded_author"
        assert rules.reason(post(author="MyBrand")) == "excluded_author"
        assert rules.reason(post(author="rival")) is None

    def test_a_view_floor_removes_posts_with_no_signal(self):
        rules = ExclusionRules(min_views=500)
        assert rules.reason(post(views=100)) == "below_min_views"
        assert rules.reason(post(views=900)) is None

    def test_a_pattern_matches_title_and_caption_together(self):
        rules = ExclusionRules(pattern="(PR|広告)")
        assert rules.reason(post(caption="これは広告です")) == "excluded_pattern"
        assert rules.reason(post(title="PR: 新商品", caption="")) == "excluded_pattern"
        assert rules.reason(post(caption="普通の投稿")) is None

    def test_no_rules_configured_means_nothing_is_active(self):
        rules = ExclusionRules()
        assert not rules.active
        kept, dropped = rules.apply([post(), post("2")])
        assert len(kept) == 2 and dropped == {}

    def test_apply_counts_every_reason_separately(self):
        rules = ExclusionRules(authors="mine", min_views=500)
        kept, dropped = rules.apply([
            post("1", author="mine"), post("2", views=10), post("3"),
        ])
        assert [r.external_id for r in kept] == ["3"]
        assert dropped == {"excluded_author": 1, "below_min_views": 1}


class StubAdapter:
    def __init__(self, records, supported):
        self.records = records
        self.supported = supported
        self.seen = None

    def search(self, keyword, limit=50, options=None):
        self.seen = options
        return self.records

    def supported_options(self):
        return self.supported


class TestRunRecordsHowItWasShaped:
    def test_a_run_records_what_was_collected_kept_and_dropped(self, session, project, monkeypatch):
        records = [post("1", author="mine"), post("2"), post("3")]
        adapter = StubAdapter(records, {"published_within_days"})
        monkeypatch.setattr(
            "snsauto.research.keyword.get_adapter", lambda *a, **k: adapter
        )
        service = ResearchService(session)
        run = service.run(
            project, "keyword", Platform.YOUTUBE,
            options=SearchOptions(published_within_days=7, video_duration="short"),
            exclusions=ExclusionRules(authors="mine"),
        )

        assert run.filters["collected"] == 3
        assert run.filters["kept"] == 2
        assert run.filters["dropped"] == {"excluded_author": 1}
        # The duration filter YouTube would honour but this stub does not.
        assert run.filters["ignored"] == {"video_duration": "short"}
        assert run.filters["applied"] == {"published_within_days": 7}
        assert len(run.posts) == 2

    def test_pre_collected_records_skip_the_adapter_entirely(self, session, project):
        run = ResearchService(session).run(
            project, "imported", Platform.TIKTOK, records=[post("1"), post("2")],
            source="manual",
        )
        assert run.source == "manual"
        assert len(run.posts) == 2

    def test_options_reach_the_adapter(self, session, project, monkeypatch):
        adapter = StubAdapter([post()], {"order"})
        monkeypatch.setattr(
            "snsauto.research.keyword.get_adapter", lambda *a, **k: adapter
        )
        ResearchService(session).run(
            project, "k", Platform.YOUTUBE, options=SearchOptions(order="date")
        )
        assert adapter.seen.order == "date"


class TestScoringIsUnchangedByFiltering:
    def test_ranks_are_assigned_after_exclusions_not_before(self, session, project, monkeypatch):
        # A rank of 1 must mean "best of what we kept", never "best of what we
        # fetched, minus some holes".
        records = [
            post("keep-low", views=100), post("drop", author="mine", views=999999),
            post("keep-high", views=5000),
        ]
        monkeypatch.setattr(
            "snsauto.research.keyword.get_adapter",
            lambda *a, **k: StubAdapter(records, set()),
        )
        run = ResearchService(session).run(
            project, "k", Platform.YOUTUBE, exclusions=ExclusionRules(authors="mine")
        )
        ranks = sorted(p.rank for p in run.posts)
        assert ranks == [1, 2]
