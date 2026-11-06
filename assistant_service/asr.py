from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import httpx


def repetitive_text(text: str) -> bool:
    """Only flag extreme loops; natural stutters and short repeated phrases remain intact."""
    if "\ufffd" in text:
        return True
    return any(len(m.group()) >= 24 for m in re.finditer(r"(.{1,24}?)\1{7,}", text[:8000]))


def repetitive_segments(segments: list[dict]) -> bool:
    if any(repetitive_text(s["text"]) for s in segments):
        return True
    # Whisper can split a token loop into individually plausible sentences.
    # Require impossible phrase durations as well as an extreme joined loop,
    # so a teacher deliberately repeating a sentence is not discarded.
    tiny_phrases = sum(len(re.sub(r"\W", "", s["text"])) >= 4
                       and float(s["end"]) - float(s["start"]) <= 0.08 for s in segments)
    joined = "".join(re.sub(r"\W", "", s["text"]) for s in segments)
    return tiny_phrases >= 2 and repetitive_text(joined)


class ASRProcessExit(Exception):
    pass


class Transcriber:
    def __init__(self, settings, *, isolated=True):
        self.settings = settings
        self.isolated = isolated
        self.model = None
        self.signature = None
        self.lock = threading.RLock()
        self.dll_handles = []
        self.dll_paths = set()
        self.loaded_device = None
        self.fallback_reason = ""
        self.worker_process = None
        self.worker_signature = None
        self.worker_log = None
        self.gpu_disabled = False
        self.requested_signature = None
        self.api_client = None

    def diagnostics(self):
        data = self.settings.data
        try:
            api_ready = bool(self.settings.openai_key())
        except (ValueError, AttributeError):
            api_ready = False
        return {"engine": data["engine"], "api_ready": api_ready,
                "model_exists": (Path(data["model_path"]) / "model.bin").is_file(),
                "exe_exists": Path(data["engine_path"]).is_file(), "ffmpeg_exists": Path(data["ffmpeg_path"]).is_file(),
                "loaded": self.model is not None or self.worker_process is not None and self.worker_process.poll() is None,
                "device": self.loaded_device or data["device"],
                "requested_device": data["device"], "fallback_reason": self.fallback_reason}

    def close(self):
        process = self.worker_process
        self.worker_process = None
        self.worker_signature = None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self.worker_log:
            self.worker_log.close()
            self.worker_log = None
        if self.api_client:
            self.api_client.close()
            self.api_client = None

    def _request_worker(self, path: Path, data: dict, device: str):
        config = {key: data[key] for key in ("engine", "engine_path", "model_path", "ffmpeg_path", "device", "compute_type", "language", "hotwords")}
        config["device"] = device
        if device == "cpu":
            config["compute_type"] = "int8"
        signature = json.dumps(config, sort_keys=True)
        if self.worker_signature != signature or not self.worker_process or self.worker_process.poll() is not None:
            self.close()
            self.worker_log = (self.settings.root / "asr-worker.log").open("ab")
            try:
                self.worker_process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "assistant_service.asr_worker"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.worker_log,
                    cwd=Path(__file__).resolve().parents[1],
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.worker_process.stdin.write((json.dumps({"config": config, "root": str(self.settings.root)}) + "\n").encode("utf-8"))
                self.worker_process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                self.close()
                raise ASRProcessExit("无法启动独立识别进程") from exc
            self.worker_signature = signature
            self.loaded_device = device
        process = self.worker_process
        try:
            process.stdin.write((json.dumps({"path": str(path)}) + "\n").encode("utf-8"))
            process.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            self.close()
            raise ASRProcessExit("识别进程已退出") from exc
        answer = queue.Queue(maxsize=1)
        def read_response():
            try:
                while line := process.stdout.readline():
                    if line.startswith(b"@SCUT-ASR@"):
                        answer.put(line[len(b"@SCUT-ASR@"):])
                        return
            except OSError:
                pass
            answer.put(b"")
        threading.Thread(target=read_response, daemon=True, name="scut-asr-response").start()
        try:
            line = answer.get(timeout=900)
        except queue.Empty as exc:
            self.close()
            raise ASRProcessExit("识别进程超时") from exc
        if not line:
            self.close()
            raise ASRProcessExit("识别进程异常退出")
        try:
            response = json.loads(line)
        except (ValueError, TypeError) as exc:
            self.close()
            raise ASRProcessExit("识别进程返回无效数据") from exc
        if not isinstance(response, dict):
            self.close()
            raise ASRProcessExit("识别进程返回无效数据")
        if not response.get("ok"):
            if device == "cuda" and response.get("error_type") in {"RuntimeError", "OSError"}:
                self.close()
                raise ASRProcessExit("CUDA 识别进程无法继续")
            raise ValueError(response.get("error") or "本地语音识别失败；请检查模型设置")
        if not isinstance(response.get("segments"), list):
            self.close()
            raise ASRProcessExit("识别进程返回无效字幕")
        return response["segments"]

    def _isolated_transcribe(self, path: Path, data: dict):
        requested = tuple(data[key] for key in ("model_path", "device", "compute_type"))
        if self.requested_signature != requested:
            self.requested_signature = requested
            self.gpu_disabled = False
            self.fallback_reason = ""
        device = "cpu" if self.gpu_disabled else data["device"]
        try:
            return self._request_worker(path, data, device)
        except ASRProcessExit as exc:
            if device != "cuda":
                raise ValueError("独立语音识别进程退出；音频已保留，可检查模型后重试") from exc
            self.gpu_disabled = True
            self.fallback_reason = "CUDA 识别进程异常退出，已自动切换到 CPU；本地服务保持连接"
            try:
                return self._request_worker(path, data, "cpu")
            except ASRProcessExit as fallback:
                raise ValueError("独立语音识别进程退出；音频已保留，可检查模型后重试") from fallback

    def load(self):
        data = self.settings.data
        if data["engine"] == "api":
            if not self.settings.openai_key():
                raise ValueError("未保存 ChatGPT API Key；请在智能分析中填写，或切换回本地识别")
            self.signature = None
            return
        signature = tuple(data[k] for k in ("model_path", "device", "compute_type"))
        if self.model is not None and signature == self.signature:
            return
        model_path = Path(data["model_path"])
        if not (model_path / "model.bin").is_file():
            raise ValueError("未找到本地模型 model.bin，请检查设置中的模型目录；不会自动下载模型")
        if os.name == "nt":
            engine = Path(data["engine_path"]).parent
            # Read-only reuse of the installed PotPlayer CUDA runtime. Keep the
            # loader handles alive, but never grow PATH on repeated load failures.
            for path in (engine / "_xxl_data" / "torch" / "lib",):
                normalized = str(path.resolve()).casefold()
                if path.is_dir() and normalized not in self.dll_paths:
                    self.dll_handles.append(os.add_dll_directory(str(path)))
                    self.dll_paths.add(normalized)
        try:
            import ctranslate2
            if not hasattr(ctranslate2, "StorageView"):
                raise ImportError("ctranslate2 installation is incomplete")
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ValueError("常驻识别依赖不完整；请重新运行启动课堂助手.cmd，仍失败时重新安装识别依赖") from exc
        self.model = None
        device, compute_type = data["device"], data["compute_type"]
        try:
            self.model = WhisperModel(str(model_path), device=device, compute_type=compute_type,
                                      local_files_only=True, cpu_threads=6, num_workers=1)
            self.fallback_reason = ""
        except RuntimeError as exc:
            message = str(exc).lower()
            if device != "cuda" or not any(term in message for term in ("no cuda-capable device", "cuda driver", "cuda failed")):
                raise
            device, compute_type = "cpu", "int8"
            self.model = WhisperModel(str(model_path), device=device, compute_type=compute_type,
                                      local_files_only=True, cpu_threads=6, num_workers=1)
            self.fallback_reason = "CUDA 当前不可用，已自动使用 CPU；速度会较慢"
        self.signature = signature
        self.loaded_device = device

    def transcribe(self, path: Path) -> list[dict]:
        with self.lock:
            data = self.settings.data.copy()
        if data["engine"] == "api":
            # Each cloud request is independent, so API chunks run in parallel
            # while the local engines keep serializing on the model lock.
            return self.openai(path, data)
        with self.lock:
            if data["engine"] == "xxl":
                return self.xxl(path, data)
            if self.isolated and self.model is None:
                return self._isolated_transcribe(path, data)
            self.load()
            options = dict(
                language=None if data["language"] == "auto" else data["language"],
                beam_size=5, vad_filter=True, vad_parameters={"min_silence_duration_ms": 450},
                # CT2 4.4's Windows CUDA sampler can crash when freeing curand states.
                # Deterministic beam search avoids that sampler (including fallback temperatures).
                temperature=0.0,
                condition_on_previous_text=False, initial_prompt=data["hotwords"] or None,
                word_timestamps=False,
            )
            def decode(extra=None):
                generated, info = self.model.transcribe(str(path), **(options | (extra or {})))
                return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip(),
                         "language": info.language, "language_probability": round(info.language_probability, 4),
                         "avg_logprob": round(s.avg_logprob, 4)}
                        for s in generated if s.text.strip() and s.no_speech_prob < 0.85]

            first = decode()
            if not repetitive_segments(first):
                return first
            attempts = [first]
            # A fixed language can force English audio into Chinese token loops. Detect
            # the language again before introducing any repetition penalty.
            retries = [{"language": None}] if options["language"] is not None else []
            retries.append({"language": None, "repetition_penalty": 1.15, "no_repeat_ngram_size": 4})
            selected = None
            for retry in retries:
                candidate = decode(retry)
                attempts.append(candidate)
                # A repetition penalty can force fluent but unrelated words. After a
                # pathological decode, an ambiguous language is still a review case.
                if candidate and not repetitive_segments(candidate) and all(s["language_probability"] >= 0.8 for s in candidate):
                    selected = [s | {"quality": "retried"} for s in candidate]
                    break
            if selected is None:
                selected = [{"start": min(s["start"] for s in first), "end": max(s["end"] for s in first),
                             "text": "〔此段识别不可靠，已保留音频待复核〕", "quality": "uncertain"}]
            path.with_suffix(".asr.json").write_text(json.dumps({"audio": path.name, "reason": "extreme_repetition",
                "attempts": attempts, "selected": selected}, ensure_ascii=False, indent=2), encoding="utf-8")
            return selected

    def xxl(self, path, data):
        executable = Path(data["engine_path"])
        if not executable.is_file():
            raise ValueError("Faster-Whisper-XXL 可执行文件不存在")
        output = path.parent / (path.stem + "-xxl")
        output.mkdir(exist_ok=True)
        command = [str(executable), str(path), "--model", data["model_path"], "--model_dir", data["model_path"],
                   "--output_dir", str(output), "--device", data["device"], "--compute_type", data["compute_type"],
                   "--beam_size", "3", "--beep_off", "--print_progress", "--output_format", "json"]
        if data["language"] != "auto":
            command += ["--language", data["language"]]
        if data["hotwords"]:
            command += ["--initial_prompt", data["hotwords"]]
        result = subprocess.run(command, cwd=self.settings.root, capture_output=True, timeout=900,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise ValueError("XXL 识别失败；请检查模型、CUDA 与显存，或切换 CPU 模式")
        files = list(output.glob("*.json"))
        if not files:
            raise ValueError("XXL 未生成 JSON 字幕")
        body = json.loads(files[0].read_text(encoding="utf-8-sig"))
        return [{"start": float(x["start"]), "end": float(x["end"]), "text": x["text"].strip()}
                for x in body.get("segments", []) if x.get("text", "").strip()]

    def _openai_client(self) -> httpx.Client:
        client = self.api_client
        if client is None:
            with self.lock:
                if self.api_client is None:
                    # OpenAI is overseas: honor env/system proxies, unlike the domestic endpoints.
                    self.api_client = httpx.Client(trust_env=True, timeout=180)
                client = self.api_client
        return client

    def _drop_openai_client(self, client: httpx.Client) -> None:
        """Dead proxy tunnels strand connections in the pool; rebuild it."""
        with self.lock:
            if self.api_client is client:
                self.api_client = None
        client.close()

    def openai(self, path: Path, data: dict) -> list[dict]:
        """Cloud speech recognition through the official OpenAI transcription API."""
        key = self.settings.openai_key()
        if not key:
            raise ValueError("未保存 ChatGPT API Key；请在智能分析中填写，或切换回本地识别")
        model = data["asr_api_model"]
        # Segment timestamps are only available through verbose_json (whisper-1);
        # gpt-transcribe family returns plain json, so the whole chunk becomes one segment.
        verbose = model.startswith("whisper")
        fields = {"model": model, "response_format": "verbose_json" if verbose else "json"}
        if verbose:
            fields["timestamp_granularities[]"] = "segment"
        else:
            fields["chunking_strategy"] = "auto"
        if data["language"] != "auto":
            fields["language"] = data["language"]
        if data["hotwords"]:
            fields["prompt"] = data["hotwords"][:800]
        body, last_error = {}, None
        for attempt in range(3):
            client = self._openai_client()
            try:
                response = client.post("https://api.openai.com/v1/audio/transcriptions",
                                                headers={"Authorization": "Bearer " + key}, data=fields,
                                                files={"file": (path.name, path.read_bytes(), "audio/wav")})
            except httpx.HTTPError as exc:
                last_error = exc
                if isinstance(exc, httpx.PoolTimeout):
                    self._drop_openai_client(client)
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                message = {401: "ChatGPT API Key 认证失败，请检查智能分析中的 OpenAI Key",
                           403: "ChatGPT 账户没有语音识别权限",
                           400: "语音识别请求被拒绝；请检查模型名与语言设置",
                           413: "音频超过接口大小限制"}.get(response.status_code)
                raise ValueError(message or f"ChatGPT 语音识别 HTTP {response.status_code}；请检查模型名或切换回本地识别")
            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                last_error = exc
                time.sleep(2 ** attempt)
                continue
            break
        else:
            raise ValueError("ChatGPT 语音识别暂时不可用，请稍后重试或切换回本地识别") from last_error
        if isinstance(body.get("segments"), list):
            return [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip(),
                     **({"language": body["language"]} if body.get("language") else {})}
                    for s in body["segments"] if isinstance(s, dict) and str(s.get("text", "")).strip()
                    and float(s.get("end", 0)) > float(s.get("start", 0))]
        text = str(body.get("text", "")).strip()
        if not text:
            return []
        return [{"start": 0.0, "end": wav_seconds(path), "text": text,
                 **({"language": body["language"]} if body.get("language") else {})}]


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError("无法解析 WAV 音频") from exc


def validate_wav(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError("音频必须是 16kHz / 单声道 / PCM16 WAV")
            if not 0 < duration <= 120:
                raise ValueError("每段音频须为 0–120 秒")
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise ValueError("音频数据不完整")
            return duration
    except (wave.Error, EOFError) as exc:
        raise ValueError("无法解析 WAV 音频") from exc
