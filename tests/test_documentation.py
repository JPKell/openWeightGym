from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = (ROOT / "docs/README.md", ROOT / "docs/agents.md")


def local_link_targets(path):
    text = path.read_text()
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http:", "https:", "#")):
            continue
        resolved = (path.parent / target.split("#", 1)[0]).resolve()
        # Qualification receipts are private runtime artifacts, absent from a fresh clone.
        if resolved.is_relative_to(ROOT / "data"):
            continue
        yield resolved


def test_documentation_index_covers_maintained_readmes_and_status_files():
    indexed = {target for path in ENTRY_POINTS for target in local_link_targets(path)}
    excluded_parts = {
        ".git",
        ".claude",  # local agent worktrees are separate checkouts, not maintained copies
        ".pytest_cache",
        ".venv",
        "build",
        "dist",
        "voice-test",
        "data",
        "__pycache__",
    }
    expected = {"docs/README.md", "docs/agents.md", "docs/agent-status.md", "app-walkthrough.md"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or excluded_parts.intersection(path.relative_to(ROOT).parts):
            continue
        if path.suffix == ".log":  # Diagnostic captures are not maintained documentation.
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("benchmarks/suites/ifeval/vendor/"):
            continue
        lowered = path.name.lower()
        indexed_kind = lowered.startswith("readme") or any(
            word in lowered for word in ("overview", "progression", "install")
        )
        if indexed_kind:
            expected.add(relative)
    for relative in expected:
        assert (ROOT / relative).resolve() in indexed, relative


def test_agent_and_project_entry_points_link_to_documentation_contract():
    assert (ROOT / "docs/agents.md").resolve() in set(local_link_targets(ENTRY_POINTS[0]))
    assert (ROOT / "docs/agent-status.md").resolve() in set(local_link_targets(ENTRY_POINTS[1]))
    assert "## Milestones" not in ENTRY_POINTS[0].read_text()
    assert "## Milestones" in (ROOT / "docs/agent-status.md").read_text()
    assert {path.name for path in ROOT.glob("*.md")} == {"AGENTS.md", "CLAUDE.md", "README.md", "app-walkthrough.md", "makeBetter.md"}


def test_consolidated_document_links_and_package_readme_exist():
    paths = (*ENTRY_POINTS, ROOT / "docs/agent-status.md", ROOT / "app-walkthrough.md", ROOT / "docs/agent-walkthrough.md")
    for path in paths:
        for target in local_link_targets(path):
            assert target.exists(), f"{path.relative_to(ROOT)}: {target}"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert (ROOT / project["readme"]).resolve() == ENTRY_POINTS[0].resolve()
