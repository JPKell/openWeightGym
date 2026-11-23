"""A plain run leaves out the Chromium tests and keeps the plain tests beside them (tests/conftest.py)."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collected(*arguments):
    printed = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-n', '0', '-p', 'no:cacheprovider', '--co', *arguments],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return {line.split('::', 1)[1] for line in printed.splitlines() if '::' in line}


def test_browser_tests_are_marked_by_what_they_reach():
    browser = collected('-m', 'browser', 'tests/test_batches_browser.py', 'tests/test_ui_consistency.py')
    # In its body, through a fixture and through a helper of the same module.
    assert 'test_ledger_fits_themes_dialogs_and_retains_filters_during_refresh' in browser
    assert 'test_models_list_leads_and_add_explains_disabled_state' in browser
    assert 'test_video_surfaces_share_console_controls_and_fit[light-1440]' in browser
    # Plain tests in the same files are not.
    assert 'test_filters_export_pagination_and_permissions_use_real_records' not in browser
    assert 'test_templates_use_shared_components' not in browser


def test_naming_a_file_runs_its_browser_tests():
    assert 'test_ledger_fits_themes_dialogs_and_retains_filters_during_refresh' in collected('tests/test_batches_browser.py')
