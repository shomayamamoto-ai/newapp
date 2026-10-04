import pytest

from snsauto.models import Platform
from snsauto.platforms import PostRecord
from snsauto.reporting.templates import TemplateRegistry
from snsauto.research.keyword import summarize_corpus


@pytest.fixture
def registry(tmp_path):
    return TemplateRegistry(user_dir=tmp_path / "templates")


def _summary(n=6):
    records = [
        PostRecord(
            external_id=f"p{i}", platform=Platform.TIKTOK, title=f"タイトル{i}",
            caption="#副業 保存してね", duration_sec=22.0,
            views=10_000 + i, likes=1200, comments=90, shares=40,
        )
        for i in range(n)
    ]
    return summarize_corpus(records)


class TestTemplateRegistry:
    def test_builtins_discovered(self, registry):
        assert {"research.html.j2", "performance.html.j2", "pdca.html.j2"} <= set(registry.names())

    def test_eject_copies_to_user_dir(self, registry):
        dest = registry.eject("research.html.j2")
        assert dest.exists()
        assert registry.is_overridden("research.html.j2")

    def test_eject_unknown_template_raises(self, registry):
        with pytest.raises(FileNotFoundError):
            registry.eject("nope.html.j2")

    def test_user_template_shadows_builtin(self, registry, tmp_path):
        (tmp_path / "templates").mkdir(parents=True, exist_ok=True)
        (tmp_path / "templates" / "research.html.j2").write_text(
            "OVERRIDDEN {{ run.keyword }}", encoding="utf-8"
        )
        html = registry.render(
            "research.html.j2",
            run=type("R", (), {"keyword": "副業", "platform": Platform.TIKTOK,
                               "source": "manual", "created_at": None})(),
            summary=_summary(), project=None, hook_distribution=[],
            weights={"engagement": 0.55, "velocity": 0.27, "reach": 0.18},
        )
        assert html.startswith("OVERRIDDEN 副業")


class TestFilters:
    def test_number_and_percent_formatting(self, registry):
        env = registry.env
        assert env.filters["int_"](1234567) == "1,234,567"
        assert env.filters["pct"](0.0953) == "9.53%"
        assert env.filters["dur"](95) == "1:35"

    def test_filters_tolerate_none(self, registry):
        env = registry.env
        assert env.filters["int_"](None) == "-"
        assert env.filters["pct"](None) == "-"
        assert env.filters["dur"](None) == "-"


class TestResearchTemplate:
    def _render(self, registry, summary, hooks=()):
        run = type("R", (), {"keyword": "副業", "platform": Platform.TIKTOK,
                             "source": "manual", "created_at": None})()
        project = type("P", (), {"name": "demo"})()
        return registry.render(
            "research.html.j2", run=run, summary=summary, project=project,
            hook_distribution=list(hooks),
            weights={"engagement": 0.55, "velocity": 0.27, "reach": 0.18},
        )

    def test_renders_stats_and_rows(self, registry):
        html = self._render(registry, _summary())
        assert "Top performers" in html
        assert "タイトル0" in html
        assert "55%" in html  # weights come from the scorer, not hardcoded

    def test_empty_corpus_renders_without_crashing(self, registry):
        html = self._render(registry, summarize_corpus([]))
        assert "No posts were collected" in html

    def test_hook_distribution_section_appears_when_present(self, registry):
        html = self._render(registry, _summary(), hooks=[("question", 4), ("listicle", 2)])
        assert "Hook archetypes" in html and "question" in html

    def test_autoescape_prevents_html_injection(self, registry):
        summary = _summary()
        summary["top_posts"][0]["title"] = "<script>alert(1)</script>"
        html = self._render(registry, summary)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


@pytest.mark.slow
def test_pdf_export_produces_a_pdf(tmp_path):
    from snsauto.reporting.pdf import PdfError, html_to_pdf

    out = tmp_path / "r.pdf"
    try:
        html_to_pdf("<h1>レポート</h1><p>本文</p>", out)
    except PdfError:
        pytest.skip("no PDF renderer available in this environment")
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF")
