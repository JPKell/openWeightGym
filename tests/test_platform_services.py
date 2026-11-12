"""Initial service choices and live front policy across pages, fragments and APIs."""

import itertools
import json
import shutil
import sys
import time
import tomllib

import pytest
from starlette.testclient import TestClient

from loadcoach.platform_services import PlatformServices, SERVICE_SETTINGS
from weightroom.navigation import navigation
from weightroom.app import load_settings
from test_weightroom import desk, sign_in
from test_setup_recovery import ROOT, one_function, script_functions, shell
from test_audio_models_browser import studio
from test_console_browser import playwright


@pytest.mark.parametrize('images,speech,audio,downloads', list(itertools.product((False, True), repeat=4)))
def test_every_service_combination_keeps_navigation_consistent(images, speech, audio, downloads):
    services = PlatformServices(images, speech, audio, downloads)
    can = ['manage-models', 'manage-settings', 'front-voice']
    nav = navigation('/models', can, True, services)
    links = {url for group in nav['groups'] for _, url in group['links']}
    for url, enabled in {'/chat/images': images, '/models/images': images, '/settings/image': images,
                         '/chat/music': audio, '/chat/sounds': audio, '/chat/speech': speech,
                         '/voice': speech, '/voice/desktop': speech, '/system/voice': speech,
                         '/models/audio': speech or audio, '/system/audio': speech or audio,
                         '/hub': downloads, '/civitai': images and downloads}.items():
        assert (url in links) == enabled, url
    assert {'/chat', '/models', '/files', '/settings'} <= links
    account = navigation('/account', can, True, services)
    assert ('/account/audio' in dict(account['links']).values()) == (speech or audio)


def save_services(server, **choices):
    deadline = time.monotonic() + 5
    while 'name="value-allow_image_generation"' not in server.get('/settings').text:
        assert time.monotonic() < deadline, 'WeightRoom did not report its settings'
        time.sleep(.02)
    return server.post('/settings/weightroom', data={'value-' + name: str(value).lower() for name, value in choices.items()})


def test_general_switches_apply_immediately_to_links_fragments_and_api_requests(desk):
    server, wire, _ = desk
    sign_in(server, 'jordan')
    saved = save_services(server, **dict.fromkeys(SERVICE_SETTINGS, False))
    assert saved.status_code == 200 and 'Platform services' in saved.text
    for url in ('/chat/images', '/chat/music', '/chat/sounds', '/chat/speech', '/models/images',
                '/models/audio', '/hub', '/civitai', '/voice', '/system/voice', '/account/audio'):
        assert f'href="{url}"' not in saved.text, url
        assert server.get(url).status_code == 403, url
    models = server.get('/models?kind=all').text
    assert '?kind=image' not in models and '?kind=audio' not in models
    assert server.get('/models?kind=image&part=list').status_code == 403
    assert server.get('/benchmarks/node-measurements?kind=audio').status_code == 403
    api = TestClient(wire.front, base_url='http://front:8442')
    headers = {'authorization': 'Bearer ' + server.cookies['weightroom']}
    for path in ('/v1/images/generations', '/v1/audio/speech', '/v1/audio/transcriptions',
                 '/v1/audio/generations', '/turnstile/artifacts/downloads', '/soundroom/jobs'):
        assert api.post(path, json={}, headers=headers).status_code == 403, path
    assert api.get('/turnstile/artifacts/hub', headers=headers).status_code == 403
    # Re-enabling downloads keeps Hugging Face available and Civitai hidden while Images is off.
    restored = save_services(server, allow_model_downloads=True)
    assert restored.status_code == 200 and 'href="/hub"' in restored.text and 'href="/civitai"' not in restored.text
    hub = server.get('/hub')
    assert hub.status_code == 200 and 'href="/civitai"' not in hub.text
    assert api.post('/turnstile/artifacts/downloads', json={'repository': 'civitai/123/456'}, headers=headers).status_code == 403
    assert api.post('/turnstile/models/install', json={'name': 'disabled-image', 'machines': ['compute'],
        'settings': {'engine': 'sdcpp'}}, headers=headers).status_code == 403
    assert api.post('/loadcoach/settings/weightroom', json={'allow_speech': 1}, headers=headers).status_code == 400
    # Reset uses the initial file/default value immediately, rather than retaining the previous false.
    reset = server.post('/settings/weightroom', data={'reset': 'allow_image_generation'})
    assert reset.status_code == 200 and 'href="/civitai"' in reset.text


def test_file_defaults_and_saved_service_overrides_survive_restart(tmp_path):
    (tmp_path / 'front.key').write_text('fixture-service-key')
    (tmp_path / 'front.toml').write_text('compute = "http://compute:8442"\nkey_file = "front.key"\ndata = "data"\n'
                                       'allow_image_generation = false\nallow_speech = false\n')
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'settings.json').write_text(json.dumps({'weightroom': {'allow_speech': True, 'allow_audio_generation': False}}))
    initial = load_settings(tmp_path / 'front.toml')
    assert PlatformServices.from_settings(initial) == PlatformServices(images=False, speech=True, audio=False, downloads=True)
    assert initial.file['allow_speech'] is False
    (data / 'settings.json').write_text('{}')
    reset = load_settings(tmp_path / 'front.toml')
    assert PlatformServices.from_settings(reset) == PlatformServices(images=False, speech=False, audio=True, downloads=True)


def test_platform_services_remain_editable_with_compute_offline(desk):
    server, wire, _ = desk
    wire.off = True
    sign_in(server, 'jordan')
    saved = save_services(server, allow_image_generation=False)
    assert saved.status_code == 200 and 'href="/chat/images"' not in saved.text
    restored = save_services(server, allow_image_generation=True)
    assert restored.status_code == 200 and 'href="/chat/images"' in restored.text


@pytest.mark.parametrize('speech,audio', [(False, True), (True, False)])
def test_audio_task_filters_follow_independent_speech_and_generation_choices(desk, speech, audio):
    server, _, _ = desk
    sign_in(server, 'jordan')
    assert save_services(server, allow_speech=speech, allow_audio_generation=audio).status_code == 200
    studio = server.get('/models/audio')
    assert studio.status_code == 200
    for task, enabled in [('music', audio), ('sound', audio), ('voice', speech), ('understand', speech)]:
        assert (f'data-audio-task="{task}"' in studio.text) == enabled
    preferences = server.get('/account/audio')
    assert preferences.status_code == 200
    assert ('id="ap-speech"' in preferences.text) == speech


def test_initial_front_install_asks_four_questions_and_records_choices(tmp_path):
    answer = shell(tmp_path, script_functions('install.sh') + '''
parse_arguments --dir "$INSTALL" --topology front
ui_section() { :; }
ui_confirm() {
  printf '%s\\n' "$1" >> "$QUESTIONS"
  [[ $1 == 'Allow image generation?' || $1 == 'Allow model downloads?' ]]
}
choose_platform_services
printf '%s\\n' "$PLATFORM_SERVICES"
''', env={'INSTALL': str(tmp_path / 'install'), 'QUESTIONS': str(tmp_path / 'questions')})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip() == 'images,downloads'
    questions = (tmp_path / 'questions').read_text().splitlines()
    assert len(questions) == 4 and any('TTS, STT' in question for question in questions)


@pytest.mark.parametrize('selected', ['none,images', 'images,,speech', 'images speech', 'unknown'])
def test_invalid_service_options_are_rejected_before_setup(tmp_path, selected):
    answer = shell(tmp_path, script_functions('install.sh') + '''
parse_arguments --platform-services "$SELECTED"
printf '%s\\n' "$ARGUMENT_ERROR"
''', env={'SELECTED': selected})
    assert answer.returncode == 0 and 'Platform services must be' in answer.stdout


@pytest.mark.parametrize('reason', ['compute', 'unattended', 'explicit', 'existing'])
def test_service_prompt_preserves_existing_unattended_and_explicit_choices(tmp_path, reason):
    install = tmp_path / 'install'
    if reason == 'existing':
        (install / 'settings').mkdir(parents=True)
        (install / 'settings/weightroom.toml').write_text('allow_image_generation = false\n')
    arguments = '--topology compute' if reason == 'compute' else '--topology front'
    arguments += ' --yes' if reason == 'unattended' else ' --platform-services none' if reason == 'explicit' else ''
    answer = shell(tmp_path, script_functions('install.sh') + '''
parse_arguments --dir "$INSTALL" ''' + arguments + '''
ui_confirm() { printf 'UNEXPECTED PROMPT'; exit 99; }
choose_platform_services
printf '%s\\n' "$PLATFORM_SERVICES"
''', env={'INSTALL': str(install)})
    assert answer.returncode == 0, answer.stderr
    assert answer.stdout.strip() == ('none' if reason == 'explicit' else 'images,speech,audio,downloads')


@pytest.mark.parametrize('selected', ['none', 'images,downloads', 'speech', 'images,speech,audio,downloads'])
def test_new_config_gets_choices_and_repair_keeps_existing_file(tmp_path, selected):
    root = tmp_path / 'installed front'
    (root / 'settings/examples').mkdir(parents=True)
    shutil.copyfile(ROOT / 'settings/examples/weightroom.toml', root / 'settings/examples/weightroom.toml')
    (root / 'scripts').mkdir()
    (root / 'scripts/install-suite-support.py').symlink_to(ROOT / 'scripts/install-suite-support.py')
    source = '''set -euo pipefail
source "$TEST_REPO/scripts/lib/ui.sh"
ui_init
ROOT=$INSTALL PY=$TEST_PYTHON TOPOLOGY=front DRY_RUN=0
FRONT_URL='' COMPUTE_URL='' LLAMA_SERVER='' MACHINE_NAME=''
declare -A WAS_CONFIGURED=()
has_service() { return 1; }
die() { ui_die "$@"; }
''' + one_function('scripts/install-suite.sh', 'install_example_config') + '''
install_example_config weightroom
PLATFORM_SERVICES=images,speech,audio,downloads
install_example_config weightroom
'''
    answer = shell(tmp_path, source, env={'INSTALL': str(root), 'TEST_PYTHON': sys.executable, 'PLATFORM_SERVICES': selected})
    assert answer.returncode == 0, answer.stdout + answer.stderr
    settings = tomllib.loads((root / 'settings/weightroom.toml').read_text())
    assert {key: settings[key] for key in SERVICE_SETTINGS} == {key: value in selected.split(',') for key, value in SERVICE_SETTINGS.items()}


@pytest.mark.parametrize('width', [1440, 390])
def test_browser_save_refreshes_navigation_and_dynamic_audio_options(studio, width):
    page, _ = studio
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto('http://console.test/settings')
    section = page.locator('[data-card-key="settings:Platform services"]')
    section.locator('[name="value-allow_image_generation"][type="checkbox"]').uncheck()
    section.locator('[name="value-allow_speech"][type="checkbox"]').uncheck()
    section.get_by_role('button', name='Save platform services', exact=True).click()
    playwright.expect(page).to_have_url('http://console.test/settings?said=Saved.')
    playwright.expect(page.locator('a[href="/chat/images"]')).to_have_count(0)
    playwright.expect(page.locator('a[href="/chat/speech"]')).to_have_count(0)
    playwright.expect(page.locator('a[href="/civitai"]')).to_have_count(0)
    page.goto('http://console.test/models/audio?model=song')
    playwright.expect(page.locator('#am-total-models')).to_have_text('1')
    playwright.expect(page.locator('[data-audio-task="voice"]')).to_have_count(0)
    page.get_by_role('button', name='Add audio model', exact=True).click()
    playwright.expect(page.locator('#am-kind option[value="speech"]')).to_have_count(0)
    playwright.expect(page.locator('#am-kind option[value="music"]')).to_have_count(1)
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
