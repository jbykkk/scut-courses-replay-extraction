from pathlib import Path
from types import SimpleNamespace
import concurrent.futures
import io
import json
import tempfile
import threading
import unittest
import wave
from unittest.mock import patch

import httpx

from assistant_service.asr import ASRProcessExit, Transcriber, repetitive_text, repetitive_segments
from assistant_service.settings import Settings
from assistant_service.manager import append_segments
from assistant_service.analysis import rule_events


def wav_bytes(seconds=1, rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(seconds * rate))
    return buffer.getvalue()


class RecognitionTests(unittest.TestCase):
    def test_cuda_worker_crash_falls_back_without_exiting_service(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder))
            settings.data.update(engine="resident", device="cuda", compute_type="int8_float16")
            engine = Transcriber(settings)
            with patch.object(engine, "_request_worker", side_effect=[ASRProcessExit("CUDA crash"), [{"start": 0, "end": 1, "text": "保留的字幕"}]]) as call:
                result = engine.transcribe(Path(folder) / "audio.wav")
            self.assertEqual(result[0]["text"], "保留的字幕")
            self.assertEqual([args.args[2] for args in call.call_args_list], ["cuda", "cpu"])
            self.assertTrue(engine.gpu_disabled)
            self.assertIn("CPU", engine.fallback_reason)

    def test_isolated_worker_returns_model_error_without_crashing_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder))
            settings.data.update(engine="resident", device="cpu", compute_type="int8", model_path=str(Path(folder) / "absent-model"))
            engine = Transcriber(settings)
            try:
                with self.assertRaisesRegex(ValueError, "model.bin"):
                    engine.transcribe(Path(folder) / "audio.wav")
                self.assertTrue(engine.worker_process and engine.worker_process.poll() is None)
            finally:
                engine.close()

    def test_extreme_loops_are_distinct_from_natural_repetition(self):
        for text in ['多' * 100, '网络' * 50, '有些认识，' * 12, '组织\ufffd']:
            self.assertTrue(repetitive_text(text))
        for text in ['对对对，这里很重要。', '网络网络网络，这个词要记住。', '注意，注意，注意这个条件。', 'very very very important']:
            self.assertFalse(repetitive_text(text))

    def test_split_loops_require_impossible_phrase_timing(self):
        rows = [{'start': n, 'end': n + 0.02, 'text': '我们的硬性素费'} for n in range(8)]
        self.assertTrue(repetitive_segments(rows))
        # Eight intentional, normally timed repetitions must not be censored.
        normal = [s | {'end': s['start'] + 1} for s in rows]
        self.assertFalse(repetitive_segments(normal))
        self.assertFalse(repetitive_segments(rows[:3]))

    def test_split_loop_triggers_bounded_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine, calls = self.transcriber(root, [('A valid sentence.', 'en')], 'auto')
            base = engine.model.transcribe
            def transcribe(path, **options):
                generated, info = base(path, **options)
                if len(calls) == 1:
                    generated = iter([SimpleNamespace(start=n, end=n + .02, text='我们的硬性素费', no_speech_prob=.1, avg_logprob=-.3) for n in range(8)])
                return generated, info
            engine.model.transcribe = transcribe
            result = engine.transcribe(root / 'sample.wav')
            self.assertEqual(len(calls), 2)
            self.assertEqual(result[0]['quality'], 'retried')
            self.assertEqual(result[0]['text'], 'A valid sentence.')

    def transcriber(self, root, results, language='zh'):
        settings = Settings(root)
        settings.data['language'] = language
        calls = []
        class Model:
            def transcribe(self, path, **options):
                calls.append(options)
                text, detected = results[min(len(calls) - 1, len(results) - 1)]
                return iter([SimpleNamespace(start=0, end=8, text=text, no_speech_prob=0.1, avg_logprob=-0.3)]), SimpleNamespace(language=detected, language_probability=0.99)
        transcriber = Transcriber(settings)
        transcriber.model = Model()
        transcriber.load = lambda: None
        return transcriber, calls

    def test_language_retry_recovers_original_english_without_global_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine, calls = self.transcriber(root, [('织' * 100, 'zh'), ('These neurons are connected with each other.', 'en')])
            result = engine.transcribe(root / 'sample.wav')
            self.assertEqual(len(calls), 2)
            self.assertIsNone(calls[1]['language'])
            self.assertNotIn('no_repeat_ngram_size', calls[0])
            self.assertEqual(result[0]['language'], 'en')
            self.assertEqual(result[0]['quality'], 'retried')
            audit = json.loads((root / 'sample.asr.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['attempts'][0][0]['text'], '织' * 100)
            self.assertTrue(all(c['temperature'] == 0 for c in calls))

    def test_unrecoverable_audio_is_flagged_not_silently_invented(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine, calls = self.transcriber(root, [('网络' * 100, 'zh')], 'auto')
            result = engine.transcribe(root / 'sample.wav')
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]['no_repeat_ngram_size'], 4)
            self.assertEqual(result[0]['quality'], 'uncertain')
            self.assertFalse(repetitive_text(result[0]['text']))
            self.assertEqual(rule_events([{'text': '扫码签到', 'quality': 'uncertain'}]), [])

    def test_normal_teacher_repetition_is_kept_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine, calls = self.transcriber(root, [('对对对，注意，注意这个条件。', 'zh')], 'auto')
            result = engine.transcribe(root / 'sample.wav')
            self.assertEqual(result[0]['text'], '对对对，注意，注意这个条件。')
            self.assertEqual(len(calls), 1)
            self.assertFalse((root / 'sample.asr.json').exists())

    def test_retry_with_ambiguous_language_is_still_flagged_for_review(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine, calls = self.transcriber(root, [('网络' * 100, 'zh'), ('看似流畅的错误替代词', 'zh')], 'auto')
            base = engine.model.transcribe
            def transcribe(path, **options):
                generated, info = base(path, **options)
                info.language_probability = .69
                return generated, info
            engine.model.transcribe = transcribe
            result = engine.transcribe(root / 'sample.wav')
            self.assertEqual(result[0]['quality'], 'uncertain')
            self.assertEqual(len(calls), 2)

    def test_repairing_an_earlier_chunk_never_deduplicates_against_future_speech(self):
        existing = [{'id': 'a:0', 'chunk': 'a', 'start': 0, 'end': 8, 'text': '错误字幕'},
                    {'id': 'b:0', 'chunk': 'b', 'start': 14, 'end': 22, 'text': '同样一句话'}]
        repaired = append_segments(existing, [{'start': 0, 'end': 8, 'text': '同样一句话', 'language': 'zh'}], 'a', 0, 8)
        self.assertEqual([s['start'] for s in repaired], [0, 14])
        self.assertEqual(repaired[0]['language'], 'zh')

    def api_engine(self, root, handler, model='whisper-1', language='zh'):
        settings = Settings(Path(root))
        settings.data.update(engine='api', asr_api_model=model, language=language, hotwords='操作系统')
        engine = Transcriber(settings)
        engine.api_client = httpx.Client(transport=httpx.MockTransport(handler))
        return engine

    def test_api_engine_requests_segments_from_the_official_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / 'audio.wav'
            wav.write_bytes(wav_bytes())
            def handler(request):
                self.assertEqual(str(request.url), 'https://api.openai.com/v1/audio/transcriptions')
                self.assertIn(b'whisper-1', request.content)
                self.assertIn(b'verbose_json', request.content)
                self.assertIn(b'timestamp_granularities', request.content)
                return httpx.Response(200, json={'language': 'zh', 'segments': [
                    {'start': 0.1, 'end': 2.4, 'text': ' 同学们好 '},
                    {'start': 2.4, 'end': 2.4, 'text': ''}]})
            with patch.object(Settings, 'openai_key', return_value='sk-test'):
                result = self.api_engine(folder, handler).transcribe(wav)
            self.assertEqual(result, [{'start': 0.1, 'end': 2.4, 'text': '同学们好', 'language': 'zh'}])

    def test_api_engine_plain_json_becomes_one_chunk_segment(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / 'audio.wav'
            wav.write_bytes(wav_bytes(seconds=4))
            def handler(request):
                body = request.content.decode('utf-8', 'ignore')
                self.assertIn('gpt-transcribe', body)
                self.assertNotIn('verbose_json', body)
                self.assertNotIn('timestamp_granularities', body)
                return httpx.Response(200, json={'text': '今天讲进程调度'})
            with patch.object(Settings, 'openai_key', return_value='sk-test'):
                result = self.api_engine(folder, handler, model='gpt-transcribe', language='auto').transcribe(wav)
            self.assertEqual(result, [{'start': 0.0, 'end': 4.0, 'text': '今天讲进程调度'}])

    def test_api_engine_transcribes_chunks_in_parallel(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / 'audio.wav'
            wav.write_bytes(wav_bytes())
            barrier = threading.Barrier(2, timeout=15)
            calls = []
            def handler(request):
                calls.append(request)
                # Both requests must be in flight together; a serialized
                # transcriber deadlocks on this barrier and fails the test.
                barrier.wait()
                return httpx.Response(200, json={'text': '同学们好'})
            engine = self.api_engine(folder, handler)
            with patch.object(Settings, 'openai_key', return_value='sk-test'):
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(engine.transcribe, [wav, wav]))
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(result[0]['text'] == '同学们好' for result in results))

    def test_api_engine_needs_a_saved_key_and_hides_upstream_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            wav = Path(folder) / 'audio.wav'
            wav.write_bytes(wav_bytes())
            with patch.object(Settings, 'openai_key', return_value=''):
                with self.assertRaisesRegex(ValueError, 'ChatGPT API Key'):
                    self.api_engine(folder, lambda r: httpx.Response(200)).transcribe(wav)
            handler = lambda r: httpx.Response(401, text='upstream secret detail')
            with patch.object(Settings, 'openai_key', return_value='sk-test'):
                with self.assertRaises(ValueError) as ctx:
                    self.api_engine(folder, handler).transcribe(wav)
            self.assertNotIn('upstream secret', str(ctx.exception))
            self.assertNotIn('sk-test', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
