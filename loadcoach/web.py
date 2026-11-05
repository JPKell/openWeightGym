"""Shared layout for the standalone API and MCP diagnostic pages."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.responses import HTMLResponse


def diagnostic_page(template: Path) -> HTMLResponse:
    environment = Environment(
        loader=FileSystemLoader([template.parent, Path(__file__).with_name('pages')]),
        autoescape=select_autoescape(['html']),
    )
    return HTMLResponse(environment.get_template(template.name).render())
