"""Report generation: data -> HTML -> PDF, recorded in the database."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sqlalchemy import select

from ..analytics.collect import platform_breakdown, summarize_publication
from ..config import get_settings
from ..models import PdcaCycle, Project, Publication, PublicationStatus, Report, ResearchRun
from ..platforms import PostRecord
from ..research.keyword import W_ENGAGEMENT, W_REACH, W_VELOCITY, summarize_corpus
from .pdf import PdfError, html_to_pdf
from .templates import TemplateRegistry


class ReportService:
    def __init__(self, session, settings=None):
        self.session = session
        self.settings = settings or get_settings()
        self.registry = TemplateRegistry(settings=self.settings)

    def _out_dir(self) -> Path:
        path = self.settings.workspace / "reports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _emit(
        self, project: Project, kind: str, template: str, slug: str, context: dict, pdf: bool
    ) -> Report:
        html = self.registry.render(template, project=project, **context)
        out_dir = self._out_dir()
        html_path = out_dir / f"{slug}.html"
        html_path.write_text(html, encoding="utf-8")

        pdf_path = None
        if pdf:
            try:
                pdf_path = html_to_pdf(html, out_dir / f"{slug}.pdf")
            except PdfError as exc:
                # The HTML is the deliverable; PDF is a convenience. Record the
                # reason instead of losing the whole report.
                context = {**context, "pdf_error": str(exc)}

        report = Report(
            project_id=project.id,
            kind=kind,
            template=template,
            html_path=str(html_path),
            pdf_path=str(pdf_path) if pdf_path else None,
            context={"slug": slug, "pdf_error": context.get("pdf_error")},
        )
        self.session.add(report)
        self.session.flush()
        return report

    def research_report(self, run: ResearchRun, pdf: bool = True) -> Report:
        records = [
            PostRecord(
                external_id=p.external_id, platform=p.platform, url=p.url,
                title=p.title, caption=p.caption, author=p.author,
                published_at=p.published_at, duration_sec=p.duration_sec,
                views=p.views, likes=p.likes, comments=p.comments, shares=p.shares,
            )
            for p in run.posts
        ]
        summary = summarize_corpus(records)
        hooks = Counter(
            p.structure.hook_type for p in run.posts if p.structure and p.structure.hook_type
        )
        return self._emit(
            run.project, "research", "research.html.j2",
            f"research-{run.id}-{run.platform.value}",
            {
                "run": run,
                "summary": summary,
                "hook_distribution": hooks.most_common(),
                # Sourced from the scorer so the caption cannot drift from the
                # weights actually used to rank.
                "weights": {
                    "engagement": W_ENGAGEMENT,
                    "velocity": W_VELOCITY,
                    "reach": W_REACH,
                },
            },
            pdf,
        )

    def performance_report(self, project: Project, pdf: bool = True) -> Report:
        publications = list(
            self.session.scalars(
                select(Publication).where(
                    Publication.project_id == project.id,
                    Publication.status == PublicationStatus.PUBLISHED,
                )
            )
        )
        rows = [r for r in map(summarize_publication, publications) if r.get("has_data")]
        totals = {
            "views": sum(r["views"] for r in rows),
            "engagement_rate": (
                sum(r["engagement_rate"] for r in rows) / len(rows) if rows else 0.0
            ),
        }
        return self._emit(
            project, "performance", "performance.html.j2",
            f"performance-{project.id}",
            {
                "title": f"{project.name} performance",
                "publications": publications,
                "rows": sorted(rows, key=lambda r: r["views"], reverse=True),
                "totals": totals,
                "breakdown": platform_breakdown(publications),
            },
            pdf,
        )

    def pdca_report(self, cycle: PdcaCycle, pdf: bool = True) -> Report:
        return self._emit(
            cycle.project, "pdca", "pdca.html.j2",
            f"pdca-{cycle.id}", {"cycle": cycle}, pdf,
        )
