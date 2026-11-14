"""SoundRoom editing and recovery workflows in Chromium, with isolated API responses."""
import copy
import json
from urllib.parse import parse_qs

import pytest

from test_console_browser import browser_context, playwright
from test_soundroom import wav_bytes
from test_weightroom import desk as audio_desk
from test_weightroom import sign_in

desk = audio_desk


def audio_model(name, kind="music", **profile):
    return {"id": name, "capabilities": {"audio": kind}, "audio_profile": {
        "family": "ace_step", "kind": kind, "modes": ["song", "cover", "repaint"], "lyrics": True,
        "limits": {"max_seconds": 600, "max_takes": 8}, "options": [{"name": "bpm", "type": "int", "default": 120}], **profile}}


def asset(index=1, **metadata):
    return {"id": f"audio_{index:032x}", "filename": f"take-{index}.wav", "kind": "take", "seconds": 30, "rate": 48000,
            "channels": 2, "created": 300-index, "mine": True, "temporary": False, "tags": [], "favourite": False,
            "metadata": {"model": "ace", "seed": 17, "settings": {"prompt": f"take {index}"}, **metadata}}


def state(**values):
    return {"models": [audio_model("ace")], "assets": [], "next": None, "voices": [], "jobs": [], "groups": [],
            "preferences": {}, "can_unlogged": False, **values}


def capture_audio(payload, submitted, older=None, wav_seconds=.1):
    def capture(route, path):
        if path.startswith("/soundroom-api/state"):
            parameters = parse_qs(path.partition("?")[2])
            result = older if older is not None and "before" in parameters else payload
            route.fulfill(content_type="application/json", body=json.dumps(result))
            return True
        if path == "/soundroom-api/jobs":
            submitted.append(json.loads(route.request.post_data))
            route.fulfill(status=201, content_type="application/json", body='{"id":"new-job"}')
            return True
        if path.startswith("/soundroom-api/assets/"):
            content = wav_bytes(wav_seconds)
            headers = {'accept-ranges': 'bytes'}
            status = 200
            if requested := route.request.headers.get('range'):
                start, end = requested.removeprefix('bytes=').split('-', 1)
                start, end = int(start), int(end) if end else len(content) - 1
                headers['content-range'] = f'bytes {start}-{end}/{len(content)}'
                content = content[start:end + 1]
                status = 206
            route.fulfill(status=status, content_type="audio/wav", headers=headers, body=content)
            return True
    return capture


def test_session_studio_preserves_each_music_models_unsaved_draft(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    models = [audio_model('ace'), audio_model('lyrics', family='heartmula', modes=['song'], lyrics_required=True)]
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(models=models), []))
        page.goto('http://console.test/chat/music')
        page.locator('#sr-prompt').fill('Keep my synth draft')
        page.locator('#sr-lyrics').fill('[chorus]\nKeep these lyrics')
        page.locator('#sr-option-bpm').fill('92')
        page.locator('#sr-duration').fill('45')
        page.locator('#sr-model').select_option('lyrics')
        page.locator('#sr-prompt').fill('A second model draft')
        page.locator('#sr-lyrics').fill('[verse]\nA different lyric')
        page.locator('#sr-model').select_option('ace')
        playwright.expect(page.locator('#sr-prompt')).to_have_value('Keep my synth draft')
        playwright.expect(page.locator('#sr-lyrics')).to_have_value('[chorus]\nKeep these lyrics')
        playwright.expect(page.locator('#sr-option-bpm')).to_have_value('92')
        playwright.expect(page.locator('#sr-duration')).to_have_value('45')
        page.evaluate('window.soundroomRefresh()')
        playwright.expect(page.locator('#sr-prompt')).to_have_value('Keep my synth draft')
        page.locator('#sr-model').select_option('lyrics')
        playwright.expect(page.locator('#sr-prompt')).to_have_value('A second model draft')
        playwright.expect(page.locator('#sr-lyrics')).to_have_value('[verse]\nA different lyric')
        browser.close()


def test_sounds_uses_model_options_loop_and_source_editing_without_music_fields(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    sound = audio_model('foley', 'sound', family='stable_audio', lyrics=False,
        limits={'max_seconds': 47, 'max_takes': 6}, options=[{'name':'guidance_scale', 'type':'float', 'default':4.5, 'min':0, 'max':10}])
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(models=[sound], assets=[asset(model='foley')]), sent))
        page.goto('http://console.test/chat/sounds')
        assert page.get_by_role('heading', level=1).all_text_contents() == ['Sounds']
        assert not page.locator('#sr-lyrics, #sr-instrumental, #sr-helper, #sr-musical-wrap').count()
        playwright.expect(page.locator('#sr-duration')).to_have_attribute('max', '47')
        playwright.expect(page.locator('#sr-n')).to_have_attribute('max', '6')
        page.get_by_text('Advanced settings', exact=True).click()
        page.locator('#sr-option-guidance_scale').fill('5.5')
        page.locator('#sr-prompt').fill('Rain striking a metal roof, close perspective')
        page.locator('#sr-loop').check()
        page.locator('#sr-generate').click()
        page.wait_for_function('document.querySelector("#sr-generate").disabled === false')
        assert sent[0]['request']['loop'] is True
        assert sent[0]['request']['n'] == 4 and sent[0]['request']['duration'] == 8
        assert sent[0]['request']['options'] == {'guidance_scale': 5.5}
        assert not {'lyrics', 'instrumental', 'mode'} & sent[0]['request'].keys()
        page.locator('#sr-list button').click()
        page.locator('#sr-mode').select_option('repaint')
        playwright.expect(page.locator('#sr-loop')).to_be_disabled()
        page.locator('#sr-use-selected').click()
        page.locator('#sr-start').fill('2')
        page.locator('#sr-end').fill('4')
        page.locator('#sr-generate').click()
        page.wait_for_function('document.querySelector("#sr-generate").disabled === false')
        assert sent[1]['source'] == asset()['id']
        assert sent[1]['request']['mode'] == 'repaint'
        assert sent[1]['request']['repaint_start'] == 2 and sent[1]['request']['repaint_end'] == 4
        assert 'loop' not in sent[1]['request']
        browser.close()


def test_session_studio_sorts_loaded_takes_and_retains_selection(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    takes = [asset(1, settings={'prompt':'Zebra'}), asset(2, settings={'prompt':'Apple'})]
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(assets=takes), []))
        page.goto('http://console.test/chat/music')
        page.locator('#sr-list button').first.click()
        page.locator('#take-sort').select_option('name')
        assert page.locator('[data-take-title]').all_text_contents() == ['Apple', 'Zebra']
        playwright.expect(page.locator('#sr-title')).to_have_text('Zebra')
        playwright.expect(page.locator('#sr-list button[aria-pressed=true]')).to_contain_text('Zebra')
        page.evaluate('window.soundroomRefresh()')
        playwright.expect(page.locator('#sr-title')).to_have_text('Zebra')
        assert page.locator('[data-take-title]').all_text_contents() == ['Apple', 'Zebra']
        browser.close()


@pytest.mark.parametrize('path,kind', [('/chat/music','music'), ('/chat/sounds','sound')])
@pytest.mark.parametrize('width', [1600, 1024, 390])
def test_session_studio_layout_model_states_and_headers(desk, tmp_path, path, kind, width):
    from test_ui_consistency import MEASURE
    from test_themes import change_theme
    server, _, _ = desk
    sign_in(server, 'jordan')
    payload = state(models=[audio_model('ace', kind, lyrics=kind == 'music')], assets=[asset()])
    gaps = '''() => [...document.querySelectorAll('.music-settings-column, .music-direction-column, .music-playback-column')].flatMap(column => {
        const cards = [...column.children].filter(card => !card.closest('[hidden]') && card.getBoundingClientRect().height > 0);
        const expected = parseFloat(getComputedStyle(column).rowGap);
        return cards.slice(1).map((card, index) => ({
            column: column.className,
            expected,
            actual: card.getBoundingClientRect().top - cards[index].getBoundingClientRect().bottom
        }));
    })'''

    def check_gaps(page):
        if width > 760:
            for gap in page.evaluate(gaps):
                assert abs(gap['actual'] - gap['expected']) < 1, (path, width, gap)

    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(payload, []))
        page.set_viewport_size({'width':width, 'height':1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test' + path)
        playwright.expect(page.locator('#sr-model option')).to_have_count(1)
        page.locator('#sr-list button').click()
        playwright.expect(page.locator('#sr-selected')).to_be_visible()
        check_gaps(page)
        if kind == 'music':
            settings_top = page.locator('[data-music-section="settings"]').bounding_box()['y']
            queue_top = page.locator('.music-queue').bounding_box()['y']
            page.locator('#sr-helper').evaluate('details => details.open = true')
            check_gaps(page)
            if width > 1250:
                assert page.locator('[data-music-section="settings"]').bounding_box()['y'] == settings_top
                assert page.locator('.music-queue').bounding_box()['y'] == queue_top
            page.locator('#sr-helper').evaluate('details => details.open = false')
        page.screenshot(path=str(tmp_path / f'{kind}-{width}-selected.png'), full_page=True)
        assert page.get_by_role('heading', level=1).all_text_contents() == ['Music' if kind == 'music' else 'Sounds']
        assert not page.locator('.page-header .eyebrow').count()
        for mode, palette in [('light','sage'), ('dark','slate')]:
            change_theme(page, palette, mode)
            for scenario in ('normal', 'no-lyrics', 'empty'):
                payload['assets'] = [] if scenario == 'empty' else [asset()]
                payload['models'] = [] if scenario == 'empty' else [audio_model('prompt-only' if scenario == 'no-lyrics' else 'ace', kind, lyrics=scenario != 'no-lyrics', options=[] if scenario == 'no-lyrics' else [{'name':'bpm','type':'int','default':120}])]
                page.evaluate('window.soundroomRefresh()')
                if scenario == 'empty':
                    playwright.expect(page.locator('#sr-generate')).to_be_disabled()
                    playwright.expect(page.locator('#sr-library-empty')).to_be_visible()
                else:
                    playwright.expect(page.locator('#sr-generate')).to_be_enabled()
                measurements = page.evaluate(MEASURE)
                assert not measurements['overflow'], (path, width, scenario)
                assert not measurements['namelessFields'], measurements['namelessFields']
                assert not measurements['invalidControls'], measurements['invalidControls']
                check_gaps(page)
            page.screenshot(path=str(tmp_path / f'{kind}-{width}-{mode}.png'), full_page=True)
        assert not errors, errors
        browser.close()


def test_speech_loops_whole_recordings_and_regions_that_reach_the_end(desk):
    """A real media element keeps playing across multiple whole-file and selected-range wraps."""
    server, _, _ = desk
    sign_in(server, 'jordan')
    take = {**asset(), 'seconds': .6}
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(
            state(models=[audio_model('clone', 'clone', options=[])], assets=[take]), [], wav_seconds=.6))
        page.goto('http://console.test/chat/speech')
        page.locator('#sr-list button').click()
        page.wait_for_function('Number.isFinite(document.querySelector("#sr-audio").duration)')
        page.get_by_label('Loop region', exact=True).check()
        assert page.locator('#sr-audio').evaluate('audio => audio.loop')
        page.locator('#sr-audio').evaluate('''async audio => {
            window.wraps = 0; let previous = 0;
            window.mediaTrace = [];
            for (const type of ['play', 'pause', 'ended', 'seeking', 'seeked', 'timeupdate']) audio.addEventListener(type, () => {
                mediaTrace.push({type, at: audio.currentTime, paused: audio.paused, ended: audio.ended});
                mediaTrace = mediaTrace.slice(-30);
            });
            audio.addEventListener('timeupdate', () => { if (audio.currentTime < previous) wraps++; previous = audio.currentTime; });
            await audio.play();
        }''')
        page.wait_for_function('wraps >= 2', timeout=5000)
        page.get_by_label('Region start (s)', exact=True).fill('.2')
        page.get_by_label('Region start (s)', exact=True).press('Tab')
        assert not page.locator('#sr-audio').evaluate('audio => audio.loop')
        page.evaluate('wraps = 0')
        try:
            page.wait_for_function('wraps >= 3', timeout=5000)
        except Exception:
            raise AssertionError(page.evaluate('''() => ({trace: mediaTrace, wraps,
                region: document.querySelector('#sr-region').textContent,
                message: document.querySelector('#sr-message').textContent})''')) from None
        assert page.locator('#sr-audio').evaluate('audio => !audio.paused')
        page.get_by_role('button', name='Clear region', exact=True).click()
        assert page.locator('#sr-audio').evaluate('audio => audio.loop')
        page.get_by_label('Loop region', exact=True).uncheck()
        page.wait_for_function('document.querySelector("#sr-audio").ended', timeout=3000)
        browser.close()


def test_paused_audio_waveform_repaints_when_the_palette_changes(desk):
    from test_themes import change_theme

    server, _, _ = desk
    sign_in(server, 'jordan')
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(assets=[asset()]), []))
        page.goto('http://console.test/chat/music')
        page.locator('#sr-list button').click()
        page.wait_for_function('''() => {
            const wave = document.querySelector('#sr-wave');
            return wave.getContext('2d').getImageData(5, wave.height / 2, 1, 1).data[3] > 0;
        }''')
        wave = page.locator('#sr-wave')
        change_theme(page, 'slate', 'dark')
        previous = wave.evaluate('canvas => canvas.toDataURL()')
        for palette, mode in [('forest', 'dark'), ('clay', 'light')]:
            change_theme(page, palette, mode)
            current = wave.evaluate('canvas => canvas.toDataURL()')
            assert current != previous, (palette, mode)
            previous = current
        browser.close()


def test_music_reuse_restores_edit_range_source_and_musical_direction(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    take = asset()
    take["reuse"] = {"source": take["id"], "request": {"model": "ace", "prompt": "brighter chorus", "mode": "repaint",
        "duration": 30, "repaint_start": 3, "repaint_end": 6, "seed": 17, "n": 1, "lyrics": "[chorus]\nHello", "options": {"bpm": 95}}}
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(assets=[take]), sent))
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/chat/music")
        playwright.expect(page.locator("#sr-empty-title")).to_have_text("Select audio below")
        playwright.expect(page.locator("#sr-empty")).to_be_visible()
        page.locator("#sr-instrumental").check()
        page.locator("#sr-list button").click()
        page.get_by_role("button", name="Reuse settings", exact=True).click()
        playwright.expect(page.locator("#sr-lyrics")).to_be_enabled()
        playwright.expect(page.locator("#sr-start")).to_have_value("3")
        playwright.expect(page.locator("#sr-end")).to_have_value("6")
        playwright.expect(page.get_by_label("Tempo (BPM)", exact=True)).to_have_value("95")
        page.get_by_role("button", name="Generate", exact=True).click()
        page.wait_for_function("document.querySelector('#sr-generate').disabled === false")
        assert sent[0]["source"] == take["id"] and sent[0]["request"]["repaint_start"] == 3
        assert sent[0]["request"]["options"] == {"bpm": 95}
        page.get_by_role("button", name="New variation", exact=True).click()
        playwright.expect(page.locator("#sr-seed")).to_have_value("-1")
        assert not errors
        browser.close()


def test_speech_uses_default_voice_and_reuses_every_speakers_voice(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    voices = [{"id": "voice_one", "name": "One", "mine": True}, {"id": "voice_two", "name": "Two", "mine": True}]
    take = asset(model="clone", settings={"lines": [{"text": "Hello"}, {"text": "Goodbye"}]})
    script = [{"text": "Hello", "voice_id": "voice_one"}, {"text": "Goodbye", "voice_id": "voice_two"}]
    take["reuse"] = {"request": {"model": "clone", "script": script, "seed": 17, "gap": .4}}
    payload = state(models=[audio_model("clone", "clone", options=[])], assets=[take], voices=voices, preferences={"voice": "voice_two"})
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(payload, sent))
        page.goto("http://console.test/chat/speech")
        playwright.expect(page.locator("#sr-voice")).to_have_value("voice_two")
        page.locator("#sr-list button").click()
        page.get_by_role("button", name="Reuse settings", exact=True).click()
        playwright.expect(page.locator('[data-speaker="Speaker 1"]')).to_have_value("voice_one")
        playwright.expect(page.locator('[data-speaker="Speaker 2"]')).to_have_value("voice_two")
        page.get_by_role("button", name="Generate speech", exact=False).click()
        page.wait_for_function("document.querySelector('#sr-generate').disabled === false")
        assert sent[0]["request"]["script"] == script and sent[0]["request"]["gap"] == .4
        browser.close()


def test_sound_reuse_restores_loop_and_older_audio_remains_selected(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    take = asset()
    take["reuse"] = {"request": {"model": "sfx", "prompt": "rain", "loop": True, "duration": 8, "seed": 17, "n": 1}}
    first = state(models=[audio_model("sfx", "sound", family="stable_audio", lyrics=False)], assets=[take], next={"before": 10, "before_id": take["id"]})
    older = copy.deepcopy(first)
    older["assets"] = [asset(101, model="sfx")]; older["next"] = None
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(first, sent, older))
        page.goto("http://console.test/chat/sounds")
        playwright.expect(page.locator("#sr-mode-help")).to_contain_text("Seamless loop")
        page.locator("#sr-list button").click()
        page.get_by_role("button", name="Reuse settings", exact=True).click()
        playwright.expect(page.locator("#sr-loop")).to_be_checked()
        page.get_by_role("button", name="Load older audio", exact=True).click()
        playwright.expect(page.locator("#sr-list button")).to_have_count(2)
        page.locator("#sr-list button").last.click()
        page.locator("#sr-form").evaluate("form => form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}))")
        page.wait_for_function("document.querySelector('#sr-generate').disabled === false")
        playwright.expect(page.locator("#sr-title")).to_have_text("take 101")
        assert sent[0]["request"]["loop"] is True
        playwright.expect(page.locator("#sr-older")).to_be_hidden()
        browser.close()


def test_failed_job_has_one_status_and_retry_preserves_the_request(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    saved = {"request": {"model": "ace", "prompt": "acoustic guitar", "duration": 10, "n": 1, "seed": 7}}
    failed = {"id": "failed-job", "model": "ace", "status": "completed", "progress": {"stage": "failed"},
              "request_counts": {"total": 1, "completed": 0, "failed": 1}, "error": "Not enough memory", "settings": saved}
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(jobs=[failed]), sent))
        page.goto("http://console.test/chat/music")
        playwright.expect(page.locator("#sr-jobs")).to_contain_text("Failed")
        playwright.expect(page.locator("#sr-jobs")).to_contain_text("Not enough memory")
        assert "completed · failed" not in page.locator("#sr-jobs").inner_text()
        page.get_by_role("button", name="Retry", exact=True).click()
        page.wait_for_function("document.querySelector('#sr-jobs button:last-child').disabled === false")
        assert sent == [saved]
        browser.close()


def test_music_model_constraints_prevent_ineffective_or_invalid_requests(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    turbo = audio_model("ace", turbo=True, options=[{"name": "num_inference_steps", "type": "int", "default": 8},
                                                     {"name": "guidance_scale", "type": "float", "default": 1}])
    heartmula = audio_model("heartmula", family="heartmula", modes=["song"], lyrics_required=True,
                           help="Lyrics-driven songs; choose ACE-Step for an instrumental.")
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(state(models=[turbo, heartmula]), sent))
        page.goto("http://console.test/chat/music")
        playwright.expect(page.locator("#sr-option-num_inference_steps")).to_have_attribute("max", "8")
        playwright.expect(page.locator("#sr-option-guidance_scale")).to_be_disabled()
        page.locator("#sr-model").select_option("heartmula")
        playwright.expect(page.locator("#sr-instrumental")).to_be_disabled()
        playwright.expect(page.locator("#sr-lyrics")).to_have_attribute("required", "")
        playwright.expect(page.locator("#sr-model-help")).to_contain_text("Lyrics-driven")
        page.locator("#sr-prompt").fill("pop, piano")
        page.get_by_role("button", name="Generate", exact=True).click()
        assert not sent
        browser.close()


def test_job_polling_recovers_from_a_temporary_network_failure(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    active = state(jobs=[{"id": "working", "model": "ace", "status": "in_progress", "request_counts": {}, "settings": {}}])
    calls = []

    def capture(route, path):
        if path.startswith("/soundroom-api/state"):
            calls.append(path)
            if len(calls) == 2:
                route.fulfill(status=503, content_type="application/json", body='{"error":{"message":"temporarily unavailable"}}')
            else:
                payload = copy.deepcopy(active)
                if len(calls) > 2:
                    payload["jobs"][0]["status"] = "completed"
                route.fulfill(content_type="application/json", body=json.dumps(payload))
            return True
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture)
        errors = []; page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://console.test/chat/music")
        playwright.expect(page.locator("#sr-message")).to_contain_text("retrying automatically", timeout=5000)
        playwright.expect(page.locator("#sr-jobs")).to_contain_text("completed", timeout=7000)
        playwright.expect(page.locator("#sr-message")).to_be_hidden()
        assert len(calls) >= 3 and not errors
        browser.close()


def test_transcription_library_can_open_older_recordings(desk):
    server, _, _ = desk
    sign_in(server, "jordan")
    recent, earlier = asset(), asset(101, transcripts=[{"text": "An older recording", "segments": []}])
    recent["kind"] = earlier["kind"] = "recording"
    first = state(models=[audio_model("asr", "transcription")], assets=[recent], next={"before": 10, "before_id": recent["id"]})
    older = state(models=first["models"], assets=[earlier])
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(first, [], older))
        page.goto("http://console.test/chat/transcribe")
        page.get_by_role("button", name="Load older recordings", exact=True).click()
        playwright.expect(page.locator("#tr-list button")).to_have_count(2)
        page.locator("#tr-list button").last.click()
        playwright.expect(page.locator("#tr-result")).to_contain_text("An older recording")
        playwright.expect(page.locator("#tr-older")).to_be_hidden()
        browser.close()


def speech_state(**values):
    return state(models=[audio_model('clone', 'clone', options=[]),
                         audio_model('designed', 'design', options=[{'name': 'temperature', 'type': 'float', 'default': .8}]),
                         audio_model('builtin', 'speech', options=[])],
                 voices=[{'id': 'one', 'name': 'Juniper', 'mine': True},
                         {'id': 'two', 'name': 'Ellis', 'mine': False}], **values)


def test_script_room_cast_model_drafts_and_real_generation_requests(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(speech_state(can_unlogged=True), sent))
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://console.test/chat/speech')
        page.locator('#script-example').select_option('dialogue')
        page.locator('[data-speaker="Maya"]').select_option('two')
        page.locator('[data-speaker="Alex"]').select_option('one')
        script = page.locator('#sr-prompt').input_value()
        page.locator('#sr-model').select_option('designed')
        page.locator('[data-speaker="Maya"]').fill('Warm and measured')
        page.locator('[data-speaker="Alex"]').fill('Bright and lively')
        page.get_by_text('Model settings', exact=True).click()
        page.locator('#sr-option-temperature').fill('0.6')
        page.locator('#sr-model').select_option('clone')
        playwright.expect(page.locator('[data-speaker="Maya"]')).to_have_value('two')
        for mode in ('voices', 'studio'):
            page.locator(f'[data-tab="{mode}"]').click()
            playwright.expect(page.locator(f'[data-tab="{mode}"]')).to_have_attribute('aria-pressed', 'true')
        playwright.expect(page.locator('#sr-prompt')).to_have_value(script)
        page.locator('#sr-generate').click()
        playwright.expect(page.locator('#sr-generate')).to_be_enabled()
        assert [line['voice_id'] for line in sent[0]['request']['script']] == ['two', 'one', 'two', 'one']
        page.locator('#sr-model').select_option('designed')
        playwright.expect(page.locator('[data-speaker="Maya"]')).to_have_value('Warm and measured')
        playwright.expect(page.locator('#sr-option-temperature')).to_have_value('0.6')
        page.locator('#sr-prompt').press('Control+Enter')
        playwright.expect(page.locator('#sr-generate')).to_be_enabled()
        assert sent[1]['request']['options'] == {'temperature': .6}
        assert [line['instructions'] for line in sent[1]['request']['script']] == ['Warm and measured', 'Bright and lively'] * 2
        assert not errors
        browser.close()


def test_script_room_import_narrator_and_built_in_voice(desk):
    server, _, _ = desk
    sign_in(server, 'jordan')
    sent = []
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(speech_state(), sent))
        page.goto('http://console.test/chat/speech')
        page.locator('#script-import').set_input_files({'name': 'script.txt', 'mimeType': 'text/plain', 'buffer': b'A script from a local file.'})
        playwright.expect(page.locator('#sr-prompt')).to_have_value('A script from a local file.')
        playwright.expect(page.locator('#script-count')).to_contain_text('6 words')
        page.locator('[data-narrator]').select_option('two')
        playwright.expect(page.locator('[data-voice="two"]')).to_have_attribute('aria-pressed', 'true')
        page.locator('#sr-generate').click()
        playwright.expect(page.locator('#sr-generate')).to_be_enabled()
        assert sent[0]['request']['script'] == [{'text': 'A script from a local file.', 'voice_id': 'two'}]
        page.locator('[data-voice="one"]').click()
        playwright.expect(page.locator('[data-narrator]')).to_have_value('one')
        page.locator('#sr-model').select_option('builtin')
        page.locator('[data-narrator]').fill('custom-preset')
        page.evaluate('window.soundroomRefresh()')
        playwright.expect(page.locator('[data-narrator]')).to_be_focused()
        playwright.expect(page.locator('[data-narrator]')).to_have_value('custom-preset')
        page.locator('#sr-generate').click()
        playwright.expect(page.locator('#sr-generate')).to_be_enabled()
        assert sent[1]['request']['script'][0]['voice'] == 'custom-preset'
        page.locator('#script-import').set_input_files({'name': 'long.txt', 'mimeType': 'text/plain', 'buffer': b'x' * 20001})
        playwright.expect(page.locator('#sr-message')).to_contain_text('exceeds 20,000')
        playwright.expect(page.locator('#sr-prompt')).to_have_value('A script from a local file.')
        browser.close()


@pytest.mark.parametrize('width', [390, 1050, 1500])
@pytest.mark.parametrize('appearance', ['light', 'dark'])
def test_script_room_heading_layout_and_empty_states(desk, tmp_path, width, appearance):
    from test_ui_consistency import MEASURE

    server, _, _ = desk
    sign_in(server, 'jordan')
    payload = speech_state()
    with playwright.sync_playwright() as engine:
        browser, page = browser_context(engine, server, capture=capture_audio(payload, []))
        page.set_viewport_size({'width': width, 'height': 1000})
        page.emulate_media(color_scheme=appearance)
        page.goto('http://console.test/chat/speech')
        playwright.expect(page.locator('#sr-voice')).to_have_value('one')
        playwright.expect(page.locator('.sp-script > .card-footer #sr-generate')).to_be_visible()
        assert page.locator('#sr-generate').count() == 1
        assert page.locator('#sr-generate').evaluate('button => button.form.id') == 'sr-form'
        assert page.locator('#soundroom').evaluate("root => root.children[0].matches('.page-header') && root.children[1].getAttribute('aria-label') === 'Voice generation modes'")
        playwright.expect(page.locator('h1')).to_have_text('Voice generation')
        assert not page.locator('.sp-review, .sp-intro, .eyebrow').count()
        for mode in ('studio', 'voices'):
            page.locator(f'[data-tab="{mode}"]').click()
            measured = page.evaluate(MEASURE)
            assert not measured['overflow'], (width, mode)
            assert not measured['namelessFields'], measured['namelessFields']
            assert not measured['mismatches'], measured['mismatches']
            assert not measured['invalidControls'], measured['invalidControls']
        page.locator('[data-tab="studio"]').click()
        playwright.expect(page.get_by_role('heading', name='The listening desk', exact=True)).to_be_visible()
        page.locator('#script-example').select_option('dialogue')
        page.screenshot(path=str(tmp_path / f'speech-{width}-{appearance}.png'), full_page=True)
        if width == 1500:
            cast = page.locator('.sp-cast-area').bounding_box()
            script = page.locator('.sp-editor-area').bounding_box()
            controls = page.locator('.sp-controls-area').bounding_box()
            assert cast['x'] < script['x'] < controls['x']
            assert abs(cast['width'] - controls['width']) < 1
        payload['voices'] = []
        page.evaluate('window.soundroomRefresh()')
        playwright.expect(page.locator('#sr-generate')).to_be_disabled()
        playwright.expect(page.locator('#sr-voice-help')).to_contain_text('Save a voice')
        page.locator('#sr-model').select_option('builtin')
        playwright.expect(page.locator('#sr-generate')).to_be_enabled()
        payload['models'] = []
        page.evaluate('window.soundroomRefresh()')
        playwright.expect(page.locator('#sr-availability')).to_contain_text('No speech model')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        browser.close()
