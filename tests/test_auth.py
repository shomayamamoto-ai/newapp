"""Web authentication: hashing, signed sessions, CSRF, roles."""

import time

import pytest

from snsauto.config import Settings
from snsauto.models import User
from snsauto.web import auth


class TestPasswords:
    def test_roundtrip(self):
        encoded = auth.hash_password("correct horse battery")
        assert auth.verify_password("correct horse battery", encoded)
        assert not auth.verify_password("wrong", encoded)

    def test_salted_so_equal_passwords_differ(self):
        assert auth.hash_password("samepassword") != auth.hash_password("samepassword")

    def test_short_passwords_are_refused(self):
        with pytest.raises(auth.AuthError):
            auth.hash_password("short")

    @pytest.mark.parametrize("bad", ["", "not-a-hash", "scrypt$x$y", "md5$1$1$1$aa$bb"])
    def test_malformed_hashes_never_verify(self, bad):
        assert auth.verify_password("anything", bad) is False


class TestSessions:
    def test_roundtrip(self):
        assert auth.read_session(auth.issue_session(7, "k" * 32), "k" * 32) == 7

    def test_a_different_key_is_rejected(self):
        token = auth.issue_session(7, "k" * 32)
        assert auth.read_session(token, "other" * 8) is None

    def test_tampering_is_rejected(self):
        token = auth.issue_session(7, "k" * 32)
        assert auth.read_session(token[:-4] + "AAAA", "k" * 32) is None

    def test_a_forged_payload_is_rejected(self):
        """Swapping the user id must invalidate the signature."""
        import base64
        import json

        token = auth.issue_session(7, "k" * 32)
        _, signature = token.rsplit(".", 1)
        forged = base64.urlsafe_b64encode(
            json.dumps({"uid": 1, "exp": int(time.time()) + 999}).encode()
        ).decode().rstrip("=")
        assert auth.read_session(f"{forged}.{signature}", "k" * 32) is None

    def test_expiry_is_enforced(self):
        assert auth.read_session(auth.issue_session(7, "k" * 32, hours=-1), "k" * 32) is None

    @pytest.mark.parametrize("bad", [None, "", "no-dot", "a.b.c"])
    def test_malformed_tokens_return_none(self, bad):
        assert auth.read_session(bad, "k" * 32) is None


class TestCsrf:
    def test_matching_tokens_pass(self):
        token = auth.issue_csrf()
        assert auth.check_csrf(token, token)

    def test_mismatch_fails(self):
        assert not auth.check_csrf(auth.issue_csrf(), auth.issue_csrf())

    @pytest.mark.parametrize("cookie,form", [(None, "a"), ("a", None), (None, None), ("", "")])
    def test_missing_halves_fail(self, cookie, form):
        assert not auth.check_csrf(cookie, form)


class TestUsers:
    def test_create_and_authenticate(self, session):
        auth.create_user(session, "A@Example.com ", "supersecret1", "A", "admin")
        user = auth.authenticate(session, "a@example.com", "supersecret1")
        assert user and user.role == "admin" and user.last_login_at is not None

    def test_email_is_normalised(self, session):
        auth.create_user(session, "  Mixed@Case.COM", "supersecret1")
        assert session.query(User).one().email == "mixed@case.com"

    def test_duplicates_are_refused(self, session):
        auth.create_user(session, "a@b.com", "supersecret1")
        with pytest.raises(auth.AuthError):
            auth.create_user(session, "a@b.com", "supersecret1")

    @pytest.mark.parametrize("email", ["", "no-at-sign", "   "])
    def test_invalid_emails_are_refused(self, session, email):
        with pytest.raises(auth.AuthError):
            auth.create_user(session, email, "supersecret1")

    def test_unknown_role_is_refused(self, session):
        with pytest.raises(auth.AuthError):
            auth.create_user(session, "a@b.com", "supersecret1", role="superuser")

    def test_wrong_password_and_unknown_user_both_return_none(self, session):
        auth.create_user(session, "a@b.com", "supersecret1")
        assert auth.authenticate(session, "a@b.com", "nope") is None
        assert auth.authenticate(session, "ghost@b.com", "nope") is None

    def test_a_deactivated_account_cannot_sign_in(self, session):
        user = auth.create_user(session, "a@b.com", "supersecret1")
        user.is_active = False
        session.flush()
        assert auth.authenticate(session, "a@b.com", "supersecret1") is None

    def test_role_capabilities(self, session):
        admin = auth.create_user(session, "admin@b.com", "supersecret1", role="admin")
        editor = auth.create_user(session, "ed@b.com", "supersecret1", role="editor")
        viewer = auth.create_user(session, "v@b.com", "supersecret1", role="viewer")
        assert (admin.can_write, admin.can_publish) == (True, True)
        assert (editor.can_write, editor.can_publish) == (True, False)
        assert (viewer.can_write, viewer.can_publish) == (False, False)


class TestSecret:
    def test_auth_on_without_a_key_is_refused(self):
        """Signing sessions with a random per-process key silently logs
        everyone out on restart, so this must fail loudly instead."""
        with pytest.raises(auth.AuthError):
            auth.resolve_secret(Settings(_env_file=None, SNSAUTO_AUTH_ENABLED=True))

    def test_auth_off_generates_one(self):
        assert auth.resolve_secret(Settings(_env_file=None))

    def test_configured_key_is_used(self):
        settings = Settings(_env_file=None, SNSAUTO_SECRET_KEY="x" * 40)
        assert auth.resolve_secret(settings) == "x" * 40
