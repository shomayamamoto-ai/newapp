"""Guards that matter when the accounts belong to somebody else.

Running an agency changes what a mistake costs. Posting one client's video on
another client's public account is not a bug report, it is a phone call, and
it cannot be undone by deleting the post.
"""

import pytest

from snsauto.models import Platform, Project, SocialAccount
from snsauto.pipeline import Pipeline
from snsauto.platforms.accounts import (
    AccountScopeError,
    AccountService,
    assert_account_scope,
)


@pytest.fixture
def clients(session):
    a = Project(name="クライアントA社", brand_profile={})
    b = Project(name="クライアントB社", brand_profile={})
    session.add_all([a, b])
    session.flush()

    def account(project, platform, name, external):
        row = SocialAccount(
            project_id=project.id if project else None, platform=platform,
            external_id=external, display_name=name, access_token="token",
            is_active=True,
        )
        session.add(row)
        session.flush()
        return row

    return {
        "a": a, "b": b,
        "a_yt": account(a, Platform.YOUTUBE, "A社チャンネル", "UC-A"),
        "b_yt": account(b, Platform.YOUTUBE, "B社の公式チャンネル", "UC-B"),
        "shared": account(None, Platform.X, "共通アカウント", "X-S"),
    }


class TestCrossClientPublishing:
    def test_another_client_s_account_is_refused(self, session, clients):
        with pytest.raises(AccountScopeError) as exc:
            Pipeline(session).publish_targets(
                clients["a"], account_ids=[clients["b_yt"].id]
            )
        # The message has to name the account, because the operator is looking
        # at a list of similar-looking channels.
        assert "B社の公式チャンネル" in str(exc.value)

    def test_it_refuses_rather_than_silently_dropping_the_target(self, session, clients):
        """Skipping would publish to fewer places than asked, without saying so."""
        with pytest.raises(AccountScopeError):
            Pipeline(session).publish_targets(
                clients["a"],
                account_ids=[clients["a_yt"].id, clients["b_yt"].id],
            )

    def test_the_client_s_own_account_still_works(self, session, clients):
        targets = Pipeline(session).publish_targets(
            clients["a"], account_ids=[clients["a_yt"].id]
        )
        assert targets == [(Platform.YOUTUBE, clients["a_yt"].id)]

    def test_an_explicitly_shared_account_is_allowed(self, session, clients):
        # project_id None means "shared on purpose", which is a decision
        # someone made, not an accident.
        targets = Pipeline(session).publish_targets(
            clients["a"], account_ids=[clients["shared"].id]
        )
        assert targets == [(Platform.X, clients["shared"].id)]

    def test_resolve_refuses_across_projects_too(self, session, clients):
        service = AccountService(session)
        with pytest.raises(AccountScopeError):
            service.resolve(
                Platform.YOUTUBE, clients["a"].id, account_id=clients["b_yt"].id
            )

    def test_naming_a_platform_never_reaches_another_client(self, session, clients):
        """Fan-out by platform must stay inside the project."""
        targets = Pipeline(session).publish_targets(
            clients["a"], platforms=[Platform.YOUTUBE]
        )
        assert clients["b_yt"].id not in [account_id for _, account_id in targets]

    def test_an_inactive_account_is_skipped_not_raised(self, session, clients):
        clients["a_yt"].is_active = False
        session.flush()
        assert Pipeline(session).publish_targets(
            clients["a"], account_ids=[clients["a_yt"].id]
        ) == []


class TestScopeRules:
    def test_a_shared_account_passes_for_any_project(self, clients):
        assert_account_scope(clients["shared"], clients["a"].id)
        assert_account_scope(clients["shared"], clients["b"].id)

    def test_no_project_context_falls_back_to_the_account_s_own_binding(self, clients):
        # A CLI one-off with no project named: the account's binding is the
        # only rule available, so it is not second-guessed.
        assert_account_scope(clients["b_yt"], None)

    def test_matching_project_passes(self, clients):
        assert_account_scope(clients["a_yt"], clients["a"].id)
