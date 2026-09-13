"""The path from a fresh install to a connection, and the reviews after it.

These gates are the reason an operator loses weeks: each one lets the API call
succeed and quietly changes the result, so none of them can be discovered by
trying. The tests here are mostly about keeping the written record honest -
that what we tell a reviewer matches what the code calls, and that nothing is
reported as satisfied unless it was actually checked.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from snsauto.config import Settings
from snsauto.models import Base, Platform, SocialAccount
from snsauto.onboarding import (
    ACCOUNT_PREREQUISITES, APP_SETUP, DONE, LAUNCH_GATES, SCOPE_USES, TODO,
    UNKNOWN, connect_plan, review_pack,
)


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/o.db")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, class_=Session)() as s:
        yield s


def plan(settings, session=None, platform=Platform.INSTAGRAM):
    steps, gates = connect_plan(platform, settings, session)
    return {s.key: s for s in steps}, gates


class TestNothingIsAssumed:
    def test_an_unconfigured_install_reports_no_step_as_done(self):
        steps, _ = plan(Settings(_env_file=None))
        assert not [s for s in steps.values() if s.state == DONE]

    def test_conditions_on_the_account_are_unknown_not_failed(self):
        """We cannot see whether an account is a Business account until it is
        connected, and reporting that as "未" would be a guess."""
        steps, _ = plan(Settings(_env_file=None))
        for requirement in ACCOUNT_PREREQUISITES[Platform.INSTAGRAM]:
            assert steps[requirement.key].state == UNKNOWN

    def test_app_credentials_are_checked_not_guessed(self):
        steps, _ = plan(Settings(_env_file=None))
        assert steps["app"].state == TODO
        assert "FACEBOOK_APP_ID" in steps["app"].detail

        steps, _ = plan(Settings(
            _env_file=None, FACEBOOK_APP_ID="a", FACEBOOK_APP_SECRET="b"
        ))
        assert steps["app"].state == DONE

    def test_the_redirect_url_is_shown_verbatim_once_it_can_be_built(self):
        """A mistyped redirect URL fails at the authorisation screen with a
        message that names neither the expected nor the received value."""
        steps, _ = plan(Settings(_env_file=None, SNSAUTO_PUBLIC_BASE_URL="https://x.jp/"))
        assert "https://x.jp/connect/instagram/callback" in steps["redirect"].detail


class TestConnectionState:
    def _connect(self, session, scopes):
        session.add(SocialAccount(
            platform=Platform.INSTAGRAM, external_id="1", access_token="t",
            display_name="showstagram", scopes=scopes, is_active=True,
        ))
        session.flush()

    def test_a_connected_account_is_named(self, session):
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        self._connect(session, list(INSTAGRAM_SCOPES))
        steps, _ = plan(Settings(_env_file=None), session)
        assert steps["connect"].state == DONE
        assert "showstagram" in steps["connect"].detail
        assert steps["scopes"].state == DONE

    def test_a_missing_permission_is_reported_with_what_it_costs(self, session):
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        self._connect(session, [s for s in INSTAGRAM_SCOPES
                                if s != "instagram_manage_insights"])
        steps, _ = plan(Settings(_env_file=None), session)
        assert steps["scopes"].state == TODO
        assert "instagram_manage_insights" in steps["scopes"].detail
        assert "保存" in steps["scopes"].action

    def test_an_unrecorded_permission_set_is_unknown(self, session):
        self._connect(session, [])
        steps, _ = plan(Settings(_env_file=None), session)
        assert steps["scopes"].state == UNKNOWN


class TestGatesAreStated:
    def test_every_platform_declares_what_its_review_blocks(self):
        for platform, gates in LAUNCH_GATES.items():
            assert gates, platform
            for gate in gates:
                # "Nothing" is never the honest answer to what a gate blocks.
                assert gate.blocks.strip()
                assert gate.source.startswith("https://"), gate.name

    def test_the_silent_ones_are_the_ones_worth_naming(self):
        """YouTube and TikTok both succeed and hide the post instead of
        refusing. That is the property that makes them worth writing down."""
        youtube = " ".join(g.blocks for g in LAUNCH_GATES[Platform.YOUTUBE])
        tiktok = " ".join(g.blocks for g in LAUNCH_GATES[Platform.TIKTOK])
        assert "非公開" in youtube
        assert "自分のみ" in tiktok

    def test_every_platform_has_somewhere_to_create_the_app(self):
        for platform in Platform:
            assert APP_SETUP[platform].console_url.startswith("https://")
            assert APP_SETUP[platform].env_vars


class TestReviewPack:
    def test_it_covers_exactly_the_scopes_we_request(self):
        """A submission narrower than the request is rejected; a submission
        wider than the request invites questions we cannot answer."""
        from snsauto.platforms.oauth import INSTAGRAM_SCOPES

        described = [use.scope for use in SCOPE_USES[Platform.INSTAGRAM]]
        assert sorted(described) == sorted(INSTAGRAM_SCOPES)

    def test_every_scope_carries_both_languages(self):
        for use in SCOPE_USES[Platform.INSTAGRAM]:
            assert use.purpose_en and use.purpose_ja
            # Meta reviews in English, so that field cannot be a translation
            # placeholder.
            assert use.purpose_en.isascii(), use.scope
            assert use.endpoints

    def test_every_field_we_claim_appears_in_the_code(self):
        """The commonest reason a submission is rejected is that it describes
        something the app does not do.

        So each endpoint in the pack is broken into its edges, fields and
        metric names, and every one of them has to appear in the adapter or
        the OAuth provider. Writing a metric into the submission that nothing
        requests is then a test failure rather than a reviewer's question.
        """
        import re
        from pathlib import Path

        source = "\n".join(
            Path(f"src/snsauto/platforms/{name}.py").read_text(encoding="utf-8")
            for name in ("instagram", "oauth")
        )
        # Query-string keys, not things the API names.
        generic = {"fields", "metric", "user_id"}
        checked = 0
        for use in SCOPE_USES[Platform.INSTAGRAM]:
            for endpoint in use.endpoints:
                body = re.sub(r"\{[^}]*\}", "", endpoint)   # our own placeholders
                for token in sorted(
                    t for t in re.findall(r"[a-z][a-z_]{4,}", body) if t not in generic
                ):
                    assert token in source, f"{use.scope}: {token} を呼んでいない"
                    checked += 1
        # Guards the regex itself: a pattern that matches nothing would make
        # every assertion above vacuous.
        assert checked >= 20

    def test_the_document_is_written_for_pasting_into_the_form(self):
        body = review_pack(Platform.INSTAGRAM)
        assert "instagram_manage_insights" in body
        assert "ログアウト状態から始める" in body     # the commonest rejection
        assert "ビジネス認証" in body

    def test_a_platform_without_a_pack_says_so(self):
        with pytest.raises(ValueError) as caught:
            review_pack(Platform.X)
        assert "Instagram" in str(caught.value)
