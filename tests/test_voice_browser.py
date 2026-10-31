"""Browser microphone mute preserves continuous silent PCM without hardware or network."""

from pathlib import Path

import pytest

playwright = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]


def mock_audio(page, configuration):
    """Exercise the real page script with synthetic audio devices and no live microphone."""
    page.evaluate('''configuration => {
        window.testConfiguration = configuration;
        Object.defineProperty(window, 'isSecureContext', {value: true});
        Object.defineProperty(navigator, 'mediaDevices', {value: {
            getSupportedConstraints: () => ({echoCancellation: true}),
            enumerateDevices: async () => [{kind: 'audioinput', deviceId: 'usb', label: 'USB microphone'}],
            getUserMedia: async constraints => {window.testConstraints=constraints; return {
                getTracks: () => [], getAudioTracks: () => [{getSettings: () => ({echoCancellation: true})}]};}
        }});
        window.AudioContext = class {
            audioWorklet = {addModule: async () => {}};
            resume = async () => {}; close = async () => {};
            createMediaStreamSource = () => ({connect() {}, disconnect() {}});
        };
        window.AudioWorkletNode = class {
            constructor() {this.port = {postMessage: value => {window.testGain=value;}}; window.testNode=this;}
            disconnect() {}
        };
        window.echoPlayback = async () => ({close() {}, setVolume: value => {window.testVolume=value;}});
        window.fetch = async (url, options) => {
            if(url.endsWith('/browser/config')) return {ok: true, json: async () => configuration};
            window.testRequest=JSON.parse(options.body);
            return {ok: true, json: async () => ({session_id: 'fixture'})};
        };
        window.WebSocket = class {
            constructor() {this.readyState=1; window.testSocket=this;}
            send() {} close() {this.readyState=3;}
        };
    }''', configuration)


def test_call_controls_send_scoped_choices_and_preserve_startup_errors():
    """Voice choices reach the session API; device, volume and error state survive socket cleanup."""
    configuration = {'modes': ['direct', 'hermes'], 'voices': [{'id': 'voice-one', 'name': 'Test voice'}],
        'default_profile': 'default', 'cloned_profile': 'soundroom', 'models': ['large', 'small'],
        'synthesis_models': ['other-clone', 'clone'], 'recognition_models': ['other-asr', 'asr'], 'speakers': [{'id': 3, 'name': 'CPU voice'}],
        'profiles': [{'id': 'default', 'modes': ['direct', 'hermes'], 'model': 'large', 'speaker': 3},
                     {'id': 'soundroom', 'modes': ['direct'], 'speech': 'audiocpp', 'model': 'small',
                      'synthesis_model': 'clone', 'recognition_model': 'asr'}]}
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('''<form id="login"><select id="mode"><option value="direct">Direct</option><option value="hermes">Hermes</option></select>
            <div id="voice-choice"><select id="voice"></select></div><div id="record-choice"><input id="record" type="checkbox"></div>
            <div id="call-settings" class="voice-call-settings"></div><input id="echo" type="checkbox" checked>
            <button id="connect"></button></form><button id="mute"></button><button id="hangup"></button>
            <button id="finish"></button><button id="interrupt"></button><div id="status"></div><div id="notice"></div>
            <div id="audio-status"></div><div id="transcript"></div>''')
        mock_audio(page, configuration)
        page.add_script_tag(content=(ROOT / 'voice/static/voice.js').read_text())
        page.wait_for_function("document.getElementById('call-profile') || document.getElementById('notice').textContent")
        assert page.locator('#call-profile').count(), page.locator('#notice').inner_text()
        assert page.get_by_label('Conversation model', exact=True).input_value() == 'large'
        page.get_by_label('Speech engine', exact=True).select_option('soundroom')
        assert page.get_by_label('Conversation model', exact=True).input_value() == 'small'
        assert page.get_by_label('Speech model', exact=True).input_value() == 'clone'
        assert page.get_by_label('Recognition model', exact=True).input_value() == 'asr'
        page.get_by_label('Speaking speed', exact=True).select_option('0.75')
        page.get_by_label('Reply length', exact=True).select_option('256')
        page.get_by_label('Microphone', exact=True).select_option('usb')
        page.locator('#call-gain').press('Home')
        for _ in range(15):
            page.locator('#call-gain').press('ArrowRight')
        page.locator('#call-volume').press('Home')
        for _ in range(4):
            page.locator('#call-volume').press('ArrowRight')
        page.locator('#record').check()
        page.evaluate('document.getElementById("login").onsubmit({preventDefault() {}})')
        result = page.evaluate('({request:testRequest, constraints:testConstraints, volume:testVolume, gain:testGain})')
        assert result['request'] == {'conversation_mode': 'direct', 'profile': 'soundroom', 'voice': 'voice-one', 'record': True,
            'options': {'speed': .75, 'model': 'small', 'max_output_tokens': 256, 'synthesis_model': 'clone',
                        'recognition_model': 'asr', 'trailing_silence_ms': 700}}
        assert result['constraints']['audio']['deviceId'] == {'exact': 'usb'}
        assert result['volume'] == .2 and result['gain'] == {'type': 'input.gain', 'value': 2}
        page.evaluate('''async () => {
            testSocket.onmessage({data: JSON.stringify({type:'session.error',code:'speech_service_permissions'})});
            await testSocket.onclose({code:1000});
        }''')
        assert 'cannot load or hold' in page.locator('#notice').inner_text()
        assert page.locator('#status').inner_text() == 'Disconnected'
        page.locator('#mode').select_option('hermes')
        assert page.get_by_label('Speech engine', exact=True).input_value() == 'default'
        assert not page.get_by_label('Conversation model', exact=True).is_visible()
        browser.close()


@pytest.mark.parametrize('backpressure', [False, True])
def test_muted_browser_sends_silence_with_the_same_frame_size(backpressure):
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True, args=['--disable-gpu'])
        page = browser.new_page()
        # No remote page, real microphone, gateway or inference is accessed.
        page.set_content('''<form id="login"></form><select id="mode"><option value="direct">Direct</option></select>
            <input id="echo" type="checkbox"><button id="connect"></button><button id="mute"></button>
            <button id="hangup"></button><button id="finish"></button><button id="interrupt"></button>
            <div id="status"></div><div id="notice"></div><div id="audio-status"></div><div id="transcript"></div>''')
        page.evaluate('''() => {
            Object.defineProperty(window, 'isSecureContext', {value: true});
            Object.defineProperty(navigator, 'mediaDevices', {value: {
                getUserMedia: async () => ({getTracks: () => [], getAudioTracks: () => [{getSettings: () => ({})}]})
            }});
            window.AudioContext = class {
                audioWorklet = {addModule: async () => {}};
                resume = async () => {}; close = async () => {};
                createMediaStreamSource = () => ({connect() {}, disconnect() {}});
            };
            window.AudioWorkletNode = class {
                constructor() {this.port = {postMessage() {}}; window.testNode = this;}
                disconnect() {}
            };
            window.echoPlayback = async () => ({close() {}});
            window.fetch = async () => ({ok: true, json: async () => ({session_id: 'fixture', modes: ['direct']})});
            window.WebSocket = class {
                constructor() {this.readyState = 1; this.bufferedAmount = 0; this.sent = []; window.testSocket = this;}
                send(value) {this.sent.push(value instanceof ArrayBuffer ? [...new Uint8Array(value)] : JSON.parse(value));}
                close() {this.readyState = 3;}
            };
        }''')
        page.add_script_tag(content=(ROOT / 'voice/static/voice.js').read_text())
        result = page.evaluate('''async backpressure => {
            await document.getElementById('login').onsubmit({preventDefault() {}});
            const frame = new Uint8Array(640).fill(23).buffer;
            const input = () => testNode.port.onmessage({data: {type: 'input', pcm: frame}});
            input(); // No capture before session.ready.
            testSocket.onmessage({data: JSON.stringify({type: 'session.ready'})});
            input();
            document.getElementById('mute').onclick();
            input(); input(); // Muting replaces every frame; it does not pause the sample clock.
            if (backpressure) {
                testSocket.bufferedAmount = 16001; input();
            } else {
                document.getElementById('mute').onclick(); input();
            }
            return {frames: testSocket.sent.filter(Array.isArray), original: [...new Uint8Array(frame)],
                    closed: testSocket.readyState === 3, notice: document.getElementById('notice').textContent};
        }''', backpressure)
        assert result['frames'][:3] == [[23] * 640, [0] * 640, [0] * 640]
        assert result['original'] == [23] * 640
        if backpressure:
            assert len(result['frames']) == 3 and result['closed']
            assert 'Connection too slow' in result['notice']
        else:
            assert result['frames'][3] == [23] * 640 and not result['closed']
        browser.close()
