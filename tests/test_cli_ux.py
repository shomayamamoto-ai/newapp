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

    def test_every_option_help_is_japanese(self):
        """The command descriptions were translated; the option help was not.

        `--help` shows both on the same screen, so half a translation reads as
        a bug rather than as a language choice.
        """
        tree = ast.parse(CLI.read_text(encoding="utf-8"))
        english = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) not in ("typer.Option", "typer.Argument"):
                continue
            for keyword in node.keywords:
                if keyword.arg != "help" or not isinstance(keyword.value, ast.Constant):
                    continue
                text = keyword.value.value
                if (isinstance(text, str) and re.search(r"[A-Za-z]{3,}", text)
                        and not JAPANESE.search(text)):
                    english.append((node.lineno, text))
        assert english == [], f"English option help: {english}"

    def test_every_table_heading_is_japanese(self):
        """A Japanese table with English column headings is the same problem,
        and it is the part the operator reads most often."""
        tree = ast.parse(CLI.read_text(encoding="utf-8"))
        # Acronyms that are read as-is in Japanese.
        allowed = {"PDCA", "URL", "ID", "API", "HTML", "PDF", "CSV", "SNS", "AI"}
        english = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "table.add_column"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                label = node.args[0].value
                if (re.search(r"[A-Za-z]{3,}", label)
                        and not JAPANESE.search(label)
                        and label not in allowed):
                    english.append((node.lineno, label))
        assert english == [], f"English column headings: {english}"


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


class TestTheModuleFormIsTheWholeCli:
    """`python -m snsauto.cli` has to be the same tool as `snsauto`.

    It was not: a stray `if __name__ == "__main__": app()` sat two thirds of
    the way down the file, so the module form ran before the later command
    groups had been registered and silently offered a truncated CLI. It also
    called `app()` rather than `main()`, losing the error explanations.
    """

    def test_the_entry_guard_is_the_last_thing_in_the_file(self):
        body = CLI.read_text(encoding="utf-8")
        assert body.count('if __name__ == "__main__":') == 1
        assert body.rstrip().endswith('if __name__ == "__main__":\n    main()')

    def test_it_exposes_every_command_group(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "snsauto.cli", "--help"],
            capture_output=True, text=True,
            cwd=str(CLI.resolve().parents[3]),
        )
        assert result.returncode == 0
        # The groups defined after the old guard's position are the ones that
        # used to disappear.
        for name in ("footage", "ab", "user", "workspace", "setup", "db", "account"):
            assert name in result.stdout, name

    def test_the_module_form_explains_errors_rather_than_tracing_them(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "snsauto.cli", "setup", "review-pack", "x"],
            capture_output=True, text=True,
            cwd=str(CLI.resolve().parents[3]),
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0
        assert "Traceback" not in combined
