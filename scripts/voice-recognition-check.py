"""Compare CPU recognition using bundled human recordings and known synthetic questions.

No microphone is opened. Results, including exact hypotheses, stay in ignored voice-test.
"""

import audioop
import json
from pathlib import Path
import re
import time
import tomllib
import wave

import numpy as np
import sherpa_onnx


def words(text):
    """Normalize recognized words for comparison with the expected transcript."""
    return re.findall(r"[a-z0-9]+", text.lower())


def errors(reference, hypothesis):
    """Word edit distance, including substitutions, insertions and deletions."""
    previous = list(range(len(hypothesis) + 1))
    for i, word in enumerate(reference, 1):
        current = [i]
        for j, other in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (word != other)))
        previous = current
    return previous[-1]


def cases(settings):
    """Exercise human voices clean and with reproducible moderate background noise."""
    folder = Path(settings['tokens']).parent / 'test_wavs'
    recordings = [(folder / line.split(' ', 1)[0], line.split(' ', 1)[1]) for line in
                  (folder / 'trans.txt').read_text().splitlines() if line.strip()]
    for number, text in enumerate(("What is the capital of France?", "Please tell me a short story about a friendly robot.",
                                 "LoadCoach runs on the compute machine. The control node is called K server.")):
        path = Path(f'voice-test/v1_0-{number}.wav')
        if path.exists():
            recordings.append((path, text))
    for path, reference in recordings:
        with wave.open(str(path), 'rb') as source:
            assert source.getnchannels() == 1 and source.getsampwidth() == 2
            pcm, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, source.getframerate(), 16000, None)
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        for noise in (0, 15, 5) if path.parent == folder else (0,):
            audio = samples.copy()
            if noise:
                amplitude = np.sqrt(np.mean(audio * audio)) / 10 ** (noise / 20)
                audio += np.random.default_rng(7).normal(0, amplitude, len(audio))
            yield path.name + (f'-noise{noise}dB' if noise else ''), reference, np.concatenate((np.zeros(8000), np.clip(audio, -1, 1), np.zeros(24000))).astype(np.float32)


def main():
    """Run the recognition check voice verification command."""
    settings = tomllib.loads(Path('settings/voice.toml').read_text())['recognition']
    recordings, report = list(cases(settings)), []
    for precision, method, paths, silence in (('int8', 'greedy_search', 4, .35),
            ('int8', 'modified_beam_search', 4, .8), ('int8', 'modified_beam_search', 8, .8),
            ('float', 'modified_beam_search', 8, .8)):
        files = {name: settings[name].replace('.int8.onnx', '.onnx') if precision == 'float' else settings[name]
                 for name in ('encoder', 'decoder', 'joiner', 'tokens')}
        recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**files, num_threads=2, provider='cpu',
            decoding_method=method, max_active_paths=paths, enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=silence, rule3_min_utterance_length=30)
        for name, reference, samples in recordings:
            stream, finals = recognizer.create_stream(), []
            started = time.monotonic()
            for offset in range(0, len(samples), 320):
                stream.accept_waveform(16000, samples[offset:offset + 320])
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                if recognizer.is_endpoint(stream):
                    text = recognizer.get_result(stream).strip()
                    if text:
                        finals.append(text)
                    recognizer.reset(stream)
            stream.accept_waveform(16000, np.zeros(9600, dtype=np.float32))
            while recognizer.is_ready(stream):
                recognizer.decode_stream(stream)
            final = recognizer.get_result(stream).strip()
            if final:
                finals.append(final)
            row = dict(precision=precision, decoding=method, paths=paths, silence=silence, case=name,
                reference=reference, heard=' '.join(finals), finals=len(finals),
                errors=errors(words(reference), words(' '.join(finals))), words=len(words(reference)),
                seconds=round(time.monotonic()-started,3), audio_seconds=round(len(samples)/16000,3))
            report.append(row)
            print(json.dumps(row), flush=True)
        del recognizer
    Path('voice-test/recognition-quality.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
