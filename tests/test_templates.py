"""Every shipped template must parse.

An unclosed {% if %} only surfaces when someone opens that page, which in
practice means a user finds it. Parsing all of them here turns that into a
test failure instead.
"""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from snsauto.reporting.templates import _fmt_dt, _fmt_dur, _fmt_int, _fmt_pct

TEMPLATE_DIRS = [
    Path("src/snsauto/web/templates"),
    Path("src/snsauto/reporting/templates"),
]


def _env(directory: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(directory)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(int_=_fmt_int, pct=_fmt_pct, dur=_fmt_dur, dt=_fmt_dt)
    return env


def _all_templates():
    for directory in TEMPLATE_DIRS:
        for path in sorted(directory.glob("*.j2")):
            yield directory, path.name


@pytest.mark.parametrize("directory,name", list(_all_templates()),
                         ids=lambda v: v if isinstance(v, str) else v.name)
def test_template_parses(directory, name):
    _env(directory).get_template(name)


def test_the_expected_templates_are_present():
    web = {p.name for p in TEMPLATE_DIRS[0].glob("*.j2")}
    assert {"layout.html.j2", "login.html.j2", "dashboard.html.j2", "project.html.j2",
            "run.html.j2", "script.html.j2", "cycle.html.j2", "jobs.html.j2",
            "experiment.html.j2", "capabilities.html.j2"} <= web
