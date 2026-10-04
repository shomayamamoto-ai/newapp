"""Comment mining: intent buckets, persistence, and what gets summarised."""

from datetime import datetime, timezone

import pytest

from snsauto.models import CompetitorPost, Platform, PostComment, ResearchRun
from snsauto.platforms import CommentRecord, CapabilityUnavailable
from snsauto.research.comments import (
    CommentMiner,
    classify_intent,
    summarize_comments,
)


@pytest.fixture
def post(session, project):
    run = ResearchRun(project_id=project.id, keyword="k", platform=Platform.YOUTUBE)
    session.add(run)
    session.flush()
    p = CompetitorPost(run_id=run.id, external_id="vid1",
                       platform=Platform.YOUTUBE, rank=1)
    session.add(p)
    session.flush()
    return p


class StubAdapter:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.calls = 0

    def fetch_comments(self, external_id, limit=50):
        self.calls += 1
        if self.error:
            raise self.error
        return self.records


def record(external_id, text, likes=0):
    return CommentRecord(external_id=external_id, text=text, author="viewer",
                         likes=likes, published_at=datetime.now(timezone.utc))


class TestIntent:
    @pytest.mark.parametrize("text,expected", [
        ("これってどうやるんですか？", "question"),
        ("何分くらいかかりますか", "question"),
        ("続編お願いします", "request"),
        ("次は筋トレを取り上げてほしい", "request"),
        ("説明が分かりにくいです", "complaint"),
        ("やってみたけどうまくいかない", "complaint"),
        ("ありがとうございます、助かりました", "praise"),
        ("今日から始めます", "other"),
    ])
    def test_buckets(self, text, expected):
        assert classify_intent(text) == expected

    def test_a_question_that_also_complains_stays_a_question(self):
        # A question is the one bucket that converts straight into a video.
        assert classify_intent("これ間違ってませんか？") == "question"


class TestMining:
    def test_comments_are_persisted_with_their_intent(self, session, post, monkeypatch):
        adapter = StubAdapter([record("c1", "どうやるんですか？"),
                               record("c2", "ありがとう")])
        monkeypatch.setattr("snsauto.research.comments.get_adapter",
                            lambda *a, **k: adapter)

        assert CommentMiner(session).mine_post(post) == 2
        intents = {c.external_id: c.intent for c in post.comments_mined}
        assert intents == {"c1": "question", "c2": "praise"}

    def test_re_mining_adds_nothing_new(self, session, post, monkeypatch):
        adapter = StubAdapter([record("c1", "質問ですか？")])
        monkeypatch.setattr("snsauto.research.comments.get_adapter",
                            lambda *a, **k: adapter)
        miner = CommentMiner(session)

        assert miner.mine_post(post) == 1
        assert miner.mine_post(post) == 0
        assert len(post.comments_mined) == 1

    def test_a_platform_without_comment_access_is_not_an_error(self, session, post, monkeypatch):
        # Instagram exposes a competitor's comments to nobody, and TikTok has
        # no post to read from. A research run must survive both.
        monkeypatch.setattr(
            "snsauto.research.comments.get_adapter",
            lambda *a, **k: StubAdapter(error=CapabilityUnavailable("no comments")),
        )
        assert CommentMiner(session).mine_post(post) == 0

    def test_very_long_comments_are_truncated_not_rejected(self, session, post, monkeypatch):
        monkeypatch.setattr(
            "snsauto.research.comments.get_adapter",
            lambda *a, **k: StubAdapter([record("c1", "あ" * 9000)]),
        )
        CommentMiner(session).mine_post(post)
        assert len(post.comments_mined[0].text) == 4000


class TestSummary:
    def _comments(self, rows):
        return [
            PostComment(post_id=1, external_id=f"c{i}", text=text,
                        likes=likes, intent=classify_intent(text))
            for i, (text, likes) in enumerate(rows)
        ]

    def test_questions_are_ranked_by_agreement_not_recency(self):
        # 60 likes on a question is 60 people with the same gap.
        comments = self._comments([
            ("最初の質問ですか？", 2), ("後の質問ですか？", 60),
        ])
        summary = summarize_comments(comments)
        assert summary["top_questions"][0]["likes"] == 60

    def test_requests_and_complaints_surface_as_unmet_needs(self):
        comments = self._comments([
            ("続編お願いします", 30), ("分かりにくい", 10), ("ありがとう", 99),
        ])
        needs = summarize_comments(comments)["unmet_needs"]
        assert [n["likes"] for n in needs] == [30, 10]

    def test_the_question_share_is_a_fraction_of_all_comments(self):
        comments = self._comments([
            ("なぜですか？", 0), ("どうやって？", 0), ("ありがとう", 0), ("最高", 0),
        ])
        assert summarize_comments(comments)["question_share"] == pytest.approx(0.5)

    def test_no_comments_is_reported_as_a_count_not_an_error(self):
        assert summarize_comments([]) == {"count": 0}

    def test_recurring_terms_skip_stop_words(self):
        comments = self._comments([
            ("プロテインはいつ飲むんですか？", 0),
            ("プロテインの量はどれくらいですか？", 0),
        ])
        terms = dict(summarize_comments(comments)["recurring_terms"])
        assert terms.get("プロテイン") == 2
        assert "です" not in terms


class TestJapaneseTokenising:
    """The old pattern matched a whole clause as one token, so nothing agreed."""

    def test_a_clause_is_split_into_content_words(self):
        from snsauto.research.text import tokenize

        assert tokenize("プロテインはいつ飲むんですか？") == ["プロテイン"]

    def test_the_same_term_in_two_phrasings_counts_as_two(self):
        comments = [
            PostComment(post_id=1, external_id="a", intent="question",
                        text="プロテインはいつ飲むんですか？"),
            PostComment(post_id=1, external_id="b", intent="question",
                        text="プロテインの量はどれくらいですか"),
        ]
        terms = dict(summarize_comments(comments)["recurring_terms"])
        assert terms["プロテイン"] == 2

    def test_hiragana_grammar_is_not_counted_as_a_term(self):
        from snsauto.research.text import tokenize

        tokens = tokenize("これはとてもよかったです")
        assert "これは" not in tokens and "です" not in tokens

    def test_a_short_comment_still_contributes_something(self):
        from snsauto.research.text import tokenize

        assert tokenize("糖質?") == ["糖質"]
