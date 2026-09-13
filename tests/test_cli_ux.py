"""What the tool shows the person running it.

These are the failures a new install hits first, and the ones most likely to
make somebody conclude the tool is broken rather than unconfigured.
"""

import ast
import re
from pathlib import Path

import pytest

from snsauto.errors import PLATFORM_SETUP, explain

CLI = Path(__file__).resolve().parents[1] / "src" / "snsauto" / "cli.py"
JAPANESE = re.compile(r"[぀-ヿ一-鿿]")


class TestHelpIsInOneLanguage:
    """The UI, the reports and the errors are Japanese; the help was not."""

    def test_every_command_description_is_japanese(self):
        tree = ast.parse(CLI.read_text(encoding="utf-8"))
        english = [
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and not node.name.startswith("_")
            # `main` is the entry point wrapper, never shown as help text.
            and node.name != "main"
            and ast.get_docstring(node)
            and not JAPANESE.search(ast.get_docstring(node))
        ]
        assert english == [], f"English help still shown for: {english}"

    def test_the_top_level_help_is_japanese(self):
        from snsauto.cli import app

        assert JAPANESE.search(app.info.help or "")


class TestErrorsExplainThemselves:
    def test_a_missing_api_key_names_the_key_and_where_to_look(self):
        """The first command a new install runs used to print a traceback."""
        from snsauto.platforms.base import CredentialsMissing

        result = explain(CredentialsMissing(
            "youtube: 'search' unavailable - missing credentials or unsupported."
        ))
        assert "YOUTUBE" in result.title
        assert "YOUTUBE_API_KEY" in result.fix
        assert "doctor" in result.fix

    @pytest.mark.parametrize("platform", sorted(PLATFORM_SETUP))
    def test_every_platform_has_setup_guidance(self, platform):
        from snsauto.platforms.base import CredentialsMissing

        result = explain(CredentialsMissing(f"{platform}: 'search' unavailable"))
        assert result.fix.strip()
        assert JAPANESE.search(result.detail + result.fix)

    def test_an_unsupported_capability_is_not_reported_as_a_bug(self):
        from snsauto.platforms.base import CapabilityUnavailable

        result = explain(CapabilityUnavailable("tiktok search not supported"))
        assert "実装の不足ではなく" in result.fix

    def test_a_cross_project_account_explains_that_nothing_was_sent(self):
        from snsauto.platforms.accounts import AccountScopeError

        result = explain(AccountScopeError("アカウント「B社」は別のプロジェクト"))
        assert "実行されていません" in result.fix

    def test_an_expired_token_says_to_reconnect(self):
        from snsauto.platforms.base import PlatformError

        assert "再連携" in explain(PlatformError("X API 401: Unauthorized")).fix

    def test_a_rate_limit_says_to_wait_rather_than_to_fix_config(self):
        from snsauto.platforms.base import PlatformError

        assert "時間をおいて" in explain(PlatformError("HTTP 429 too many requests")).fix

    def test_an_unknown_error_still_explains_how_to_see_more(self):
        result = explain(ValueError("something odd"))
        assert "--debug" in result.fix
        assert "ValueError" in result.detail

    def test_every_explanation_is_in_japanese(self):
        from snsauto.platforms.base import CredentialsMissing, PlatformError

        for exc in (CredentialsMissing("youtube: 'search' unavailable"),
                    PlatformError("403 forbidden"),
                    ValueError("x"), FileNotFoundError("y")):
            result = explain(exc)
            assert JAPANESE.search(result.title)


class TestSetupChecklist:
    def test_a_fresh_install_points_at_the_first_missing_step(self, session):
        from snsauto.config import Settings
        from snsauto.setup_state import checklist, progress

        state = progress(checklist(session, Settings(_env_file=None)))
        assert not state["complete"]
        assert state["next"].key == "project"      # nothing exists yet
        assert state["done"] == 0

    def test_finishing_a_step_advances_the_pointer(self, session, project):
        from snsauto.config import Settings
        from snsauto.setup_state import checklist, progress

        state = progress(checklist(session, Settings(_env_file=None)))
        assert state["done"] == 1
        # With a project but no searchable platform, connecting one is next.
        assert state["next"].key == "search"

    def test_the_optional_step_does_not_block_completion(self, session):
        from snsauto.config import Settings
        from snsauto.setup_state import checklist, progress

        steps = checklist(session, Settings(_env_file=None))
        optional = [s for s in steps if s.optional]
        assert optional
        state = progress(steps)
        assert state["total"] == len(steps) - len(optional)

    def test_every_pending_step_says_what_to_do(self, session):
        from snsauto.config import Settings
        from snsauto.setup_state import checklist

        for step in checklist(session, Settings(_env_file=None)):
            assert step.label and JAPANESE.search(step.label)
            if not step.done:
                assert step.detail, f"{step.key} has no guidance"
