from datetime import datetime, timedelta, timezone

from snsauto.models import Platform
from snsauto.platforms import PostRecord
from snsauto.research.keyword import (
    BASIS_INTERACTIONS,
    BASIS_MIXED,
    BASIS_VIEWS,
    corpus_basis,
    engagement_basis,
    engagement_rate,
    score_posts,
    summarize_corpus,
    velocity,
)


def _post(**kw):
    base = dict(external_id="x", platform=Platform.YOUTUBE, views=1000, likes=100)
    return PostRecord(**{**base, **kw})


def test_engagement_rate_uses_all_interactions():
    p = _post(views=1000, likes=50, comments=30, shares=20)
    assert engagement_rate(p) == 0.1


def test_hidden_views_yield_interactions_and_say_so():
    """Some platforms hide view counts, so there is no rate to compute.

    The old code returned ``min(1.0, interactions / 1000)``, which clamped
    every post above 1000 interactions to exactly 1.0 - the entire top of an
    Instagram corpus tied, and ranking stopped working there. The count is
    returned instead, and the basis says it is a count.
    """
    p = _post(views=0, likes=200, comments=0, shares=0)
    assert engagement_rate(p) == 200.0
    assert engagement_basis(p) == BASIS_INTERACTIONS


def test_big_instagram_posts_no_longer_all_tie():
    small = _post(views=0, likes=1200, comments=0, shares=0)
    huge = _post(views=0, likes=40000, comments=0, shares=0)
    assert engagement_rate(huge) > engagement_rate(small)


def test_a_corpus_with_views_is_a_rate():
    posts = [_post(views=1000, likes=50), _post(views=2000, likes=90)]
    assert corpus_basis(posts) == BASIS_VIEWS


def test_a_corpus_mixing_bases_refuses_to_be_one_number():
    """A ratio averaged with a count is not a quantity."""
    posts = [_post(views=1000, likes=50), _post(views=0, likes=500)]
    assert corpus_basis(posts) == BASIS_MIXED


def test_engagement_rate_zero_when_no_signal():
    assert engagement_rate(_post(views=0, likes=0)) == 0.0


def test_velocity_uses_age():
    now = datetime(2026, 1, 11, tzinfo=timezone.utc)
    p = _post(views=1000, published_at=now - timedelta(days=10))
    assert velocity(p, now) == 100.0


def test_velocity_floors_age_for_fresh_posts():
    """A post minutes old must not produce an infinite velocity."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p = _post(views=1000, published_at=now)
    assert velocity(p, now) == 2000.0


def test_high_engagement_small_account_outranks_viral_outlier():
    """The whole point of the composite score: format quality over raw reach."""
    now = datetime(2026, 1, 11, tzinfo=timezone.utc)
    week_ago = now - timedelta(days=7)
    viral = _post(external_id="viral", views=5_000_000, likes=25_000, published_at=week_ago)
    tight = _post(external_id="tight", views=40_000, likes=4_400, comments=600,
                  shares=400, published_at=week_ago)
    ranked = score_posts([viral, tight], now)
    assert ranked[0][0].external_id == "tight"


def test_score_posts_sorted_descending():
    posts = [_post(external_id=str(i), views=i * 1000, likes=i * 10) for i in range(1, 6)]
    scores = [m["score"] for _, m in score_posts(posts)]
    assert scores == sorted(scores, reverse=True)


def test_score_posts_empty():
    assert score_posts([]) == []


def test_summarize_corpus_reports_duration_band():
    now = datetime(2026, 1, 11, tzinfo=timezone.utc)
    posts = [
        _post(external_id=str(i), views=10_000 + i, likes=1500,
              duration_sec=22.0, published_at=now - timedelta(days=2))
        for i in range(6)
    ]
    summary = summarize_corpus(posts)
    assert summary["count"] == 6
    assert summary["duration_sec"]["band_top"] == "15-30s"
    assert summary["engagement"]["median"] > 0


def test_summarize_corpus_empty():
    assert summarize_corpus([])["count"] == 0


def test_summarize_corpus_counts_hashtags():
    posts = [_post(external_id=str(i), caption="#副業 #節約 tips") for i in range(3)]
    tags = dict(summarize_corpus(posts)["top_hashtags"])
    assert tags["副業"] == 3 and tags["節約"] == 3
