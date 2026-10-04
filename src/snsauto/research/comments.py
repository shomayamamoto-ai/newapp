"""Mining what viewers actually wrote under a competitor's post.

The comment *count* was already collected and it says almost nothing: a post
can earn 400 comments because it was useful or because it was wrong. The text
says which, and more usefully it says what the audience still does not know -
which is the cheapest source of next-video ideas there is.

What is reachable, per platform:

* YouTube  - ``commentThreads`` with the same plain API key search uses. Any
             public video whose owner left comments on.
* X        - replies, via recent search. Same 7-day window as everything else
             on that tier.
* Instagram- own media only. A competitor's comments are not exposed.
* TikTok   - no public search, so no post to read comments from.

Intent classification is regex, not a model: five buckets, Japanese and
English. It is here to sort a few hundred lines into piles, not to be subtle,
and a wrong bucket costs nothing because the raw text is kept.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

from ..models import CompetitorPost, PostComment, ResearchRun
from sqlalchemy import select

from ..platforms import CommentRecord, PlatformError, get_adapter
from .text import tokenize

log = logging.getLogger(__name__)

# Order matters: a question that also complains is still a question, because a
# question is the one bucket that converts directly into a video.
INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("question", re.compile(
        r"[?？]|ですか|ますか|でしょうか|どう(すれば|やって|なる)|なぜ|どこで|いつ|"
        r"教えて|知りたい|どれくらい|何が|どっち",
        re.I)),
    ("request", re.compile(
        r"して(ほしい|欲しい)|お願いします|希望|リクエスト|続編|次は|"
        r"取り上げて|やってほしい|please (do|make|cover)|request",
        re.I)),
    ("complaint", re.compile(
        r"違う|間違|嘘|うまくいかない|できなかった|効果がない|残念|"
        r"わかりにくい|分かりにくい|微妙|wrong|doesn't work|misleading",
        re.I)),
    ("praise", re.compile(
        r"ありがとう|助かり|わかりやすい|分かりやすい|最高|참고|神|"
        r"勉強になり|よかった|thank|helpful|love this|great video",
        re.I)),
]

def classify_intent(text: str) -> str:
    for name, pattern in INTENT_PATTERNS:
        if pattern.search(text):
            return name
    return "other"


class CommentMiner:
    """Fetches comments for stored posts and turns them into themes."""

    def __init__(self, session, settings=None, llm=None):
        self.session = session
        self.settings = settings
        self.llm = llm

    def limit(self) -> int:
        return getattr(self.settings, "comment_fetch_limit", 50) if self.settings else 50

    def mine_post(self, post: CompetitorPost, limit: int | None = None) -> int:
        """Fetch and persist comments for one post. Returns how many are new."""
        limit = limit or self.limit()
        adapter = get_adapter(post.platform, settings=self.settings)
        try:
            records: list[CommentRecord] = adapter.fetch_comments(
                post.external_id, limit=limit
            )
        except PlatformError as exc:
            # Comments disabled, an unsupported platform, a post outside the
            # search window: all normal, none of them fatal to a research run.
            log.info("no comments for %s: %s", post.external_id, exc)
            return 0

        # Query rather than read ``post.comments_mined``: that collection is
        # cached on the loaded post and does not see rows added earlier in
        # this session, so a second mine would re-insert and hit the unique
        # constraint instead of reporting zero new comments.
        existing = set(self.session.scalars(
            select(PostComment.external_id).where(PostComment.post_id == post.id)
        ))
        added = 0
        for record in records:
            if record.external_id in existing:
                continue
            self.session.add(PostComment(
                post_id=post.id,
                external_id=record.external_id,
                text=record.text[:4000],
                author=record.author,
                likes=record.likes,
                reply_count=record.reply_count,
                published_at=record.published_at,
                intent=classify_intent(record.text),
            ))
            existing.add(record.external_id)
            added += 1
        self.session.flush()
        return added

    def mine_run(self, run: ResearchRun, top_n: int = 10) -> int:
        """Mine the top performers only - the tail is not worth the quota."""
        total = 0
        for post in sorted(run.posts, key=lambda p: p.rank)[:top_n]:
            try:
                total += self.mine_post(post)
            except Exception as exc:
                log.warning("comment mining failed for post %s: %s", post.id, exc)
        return total


def summarize_comments(comments: list[PostComment], top_n: int = 12) -> dict:
    """What the audience is asking for, ranked by how much agreement it has."""
    if not comments:
        return {"count": 0}

    intents = Counter(c.intent or "other" for c in comments)
    questions = [c for c in comments if c.intent == "question"]

    # Rank by likes, not recency: a question with 60 likes is 60 people with
    # the same gap, and that is what makes it worth a video.
    ranked = sorted(questions, key=lambda c: (c.likes, c.reply_count), reverse=True)

    return {
        "count": len(comments),
        "intent_mix": intents.most_common(),
        "question_share": round(len(questions) / len(comments), 3),
        "top_questions": [
            {"text": c.text[:280], "likes": c.likes, "replies": c.reply_count}
            for c in ranked[:top_n]
        ],
        "recurring_terms": _terms(questions or comments, 15),
        "unmet_needs": [
            {"text": c.text[:280], "likes": c.likes}
            for c in sorted(
                (c for c in comments if c.intent in ("request", "complaint")),
                key=lambda c: c.likes, reverse=True,
            )[:6]
        ],
    }


def _terms(comments: list[PostComment], n: int) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for comment in comments:
        counter.update(tokenize(comment.text))
    return counter.most_common(n)
