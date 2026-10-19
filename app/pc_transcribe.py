"""
TranscribeService - on-device speech-to-text for voicemail and call
recordings, via whisper.cpp on the CPU.

The engine is NOT part of the firmware: a static aarch64-musl `whisper-cli`
binary is expected at /root/whisper/whisper-cli (cross-compiled with zig cc,
no shared-library dependencies, runs on any of our aarch64 boxes). Models are
multilingual ggml files downloaded on demand from hf-mirror.com (reachable
from CN) with huggingface.co as fallback.

Model choice scales with the box: units ship with 4GB or 16GB of RAM, so the
default is picked from /proc/meminfo (base for small boxes, small for big
ones) and the user can override it. Whisper's multilingual models handle ~99
languages; language is auto-detected unless pinned in the config.

Jobs run strictly one at a time on 4 threads at nice 10, so a transcription
never starves the router's real work. A 1-minute voicemail takes roughly a
minute on the base model on the RK3576's A72 cores.
"""

import os
import queue
import subprocess
import threading
import time

import pcat_config
import pc_call_audio
from pcat_util import pcat_logger

WHISPER_DIR = "/root/whisper"
ENGINE_BIN = os.path.join(WHISPER_DIR, "whisper-cli")
MODELS_DIR = os.path.join(WHISPER_DIR, "models")
RECORD_DIR = pc_call_audio.RECORD_DIR

# Multilingual quantized models from ggerganov/whisper.cpp. ram_mb is the
# approximate working-set while transcribing; a model is only offered when
# the box's total RAM clears it with headroom.
MODELS = {
    "tiny":   {"file": "ggml-tiny-q5_1.bin",   "size_mb": 32,  "ram_mb": 390},
    "base":   {"file": "ggml-base-q5_1.bin",   "size_mb": 60,  "ram_mb": 500},
    "small":  {"file": "ggml-small-q5_1.bin",  "size_mb": 190, "ram_mb": 860},
    "medium": {"file": "ggml-medium-q5_0.bin", "size_mb": 539, "ram_mb": 1700},
}
# Own mirror first (dl.photonicat.com hosts the same files, uploaded from the
# pcat-whisper-dist repo), public sources as fallback.
MODEL_URL_BASES = [
    "https://dl.photonicat.com/ai-engines/whisper/models/",
    "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/",
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/",
]
# Static aarch64-musl whisper-cli, ~24MB; only our server carries it.
ENGINE_URLS = [
    "https://dl.photonicat.com/ai-engines/whisper/whisper-cli-linux-aarch64-musl",
]
ENGINE_SIZE_MB = 24
LANGS = ["auto", "en", "zh", "ja", "ko", "de", "fr", "es", "ru", "pt",
         "it", "ar", "hi", "th", "vi"]
THREADS = 4
JOB_TIMEOUT = 15 * 60
TRANSCRIPT_MAX_CHARS = 4000


def _log():
    return pcat_logger.logger


def total_ram_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 4096


def recommended_model():
    """base for 4GB-class boxes, small for 16GB-class ones."""
    return "base" if total_ram_mb() < 6144 else "small"


def transcript_path(name):
    return os.path.join(RECORD_DIR, name + ".txt")


def read_transcript(name):
    try:
        with open(transcript_path(name), encoding="utf-8") as fh:
            return fh.read().strip()[:TRANSCRIPT_MAX_CHARS]
    except OSError:
        return None


class TranscribeService:
    def __init__(self):
        self._q = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self.current = None          # name being transcribed right now
        self._worker = None
        # engine/model download state ("kind": "engine" | "model")
        self.download = {"kind": None, "model": None, "done": 0, "total": 0,
                         "error": None}
        self._downloading = False

    # ------------------------------------------------------------- config

    def engine_present(self):
        return os.access(ENGINE_BIN, os.X_OK)

    def model(self):
        m = pcat_config.get_user_config_key("phone_transcribe_model", None)
        return m if m in MODELS else recommended_model()

    def lang(self):
        l = pcat_config.get_user_config_key("phone_transcribe_lang", "auto")
        return l if l in LANGS else "auto"

    def auto_vm(self):
        return bool(pcat_config.get_user_config_key("phone_vm_transcribe", True))

    def model_installed(self, model):
        info = MODELS.get(model)
        if not info:
            return False
        try:
            path = os.path.join(MODELS_DIR, info["file"])
            return os.path.getsize(path) > info["size_mb"] * 1024 * 1024 // 2
        except OSError:
            return False

    def ready(self):
        return self.engine_present() and self.model_installed(self.model())

    def status(self):
        ram = total_ram_mb()
        models = []
        for name, info in MODELS.items():
            models.append({
                "name": name,
                "size_mb": info["size_mb"],
                "ram_mb": info["ram_mb"],
                "installed": self.model_installed(name),
                # leave ~2GB for the router itself
                "fits": info["ram_mb"] < ram - 2048,
                "recommended": name == recommended_model(),
            })
        with self._lock:
            queued = len(self._queued)
        return {
            "engine": self.engine_present(),
            "ready": self.ready(),
            "model": self.model(),
            "lang": self.lang(),
            "langs": LANGS,
            "auto_vm": self.auto_vm(),
            "ram_mb": ram,
            "models": models,
            "busy": self.current is not None,
            "current": self.current,
            "queued": queued,
            "downloading": self._downloading,
            "download": dict(self.download),
        }

    # ------------------------------------------------------- model download

    def start_download(self, model):
        info = MODELS.get(model)
        if not info:
            return False, "Unknown model"
        if self._downloading:
            return False, "A download is already running"
        if self.model_installed(model):
            return True, "Already installed"
        urls = [base + info["file"] for base in MODEL_URL_BASES]
        dest = os.path.join(MODELS_DIR, info["file"])
        self._begin_download("model", model, info["size_mb"], urls, dest)
        return True, "Download started"

    def start_engine_download(self):
        if self._downloading:
            return False, "A download is already running"
        if self.engine_present():
            return True, "Already installed"
        self._begin_download("engine", None, ENGINE_SIZE_MB,
                             list(ENGINE_URLS), ENGINE_BIN)
        return True, "Download started"

    def _begin_download(self, kind, model, size_mb, urls, dest):
        self._downloading = True
        self.download = {"kind": kind, "model": model, "done": 0,
                         "total": size_mb * 1024 * 1024, "error": None}
        threading.Thread(target=self._download_worker,
                         args=(kind, urls, dest), daemon=True).start()

    def _download_worker(self, kind, urls, dest):
        import requests
        part = dest + ".part"
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            last_err = "no source reachable"
            for url in urls:
                try:
                    with requests.get(url, stream=True, timeout=(10, 60),
                                      allow_redirects=True) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("Content-Length", 0))
                        if total:
                            self.download["total"] = total
                        done = 0
                        with open(part, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                fh.write(chunk)
                                done += len(chunk)
                                self.download["done"] = done
                    os.rename(part, dest)
                    if kind == "engine":
                        os.chmod(dest, 0o755)
                    _log().info("transcribe: %s downloaded (%d MB) from %s",
                                kind, done >> 20, url)
                    return
                except Exception as e:
                    last_err = str(e)
                    _log().error("transcribe: %s download from %s failed: %s",
                                 kind, url, e)
            self.download["error"] = last_err
        finally:
            try:
                os.unlink(part)
            except OSError:
                pass
            self._downloading = False

    # --------------------------------------------------------------- jobs

    def enqueue(self, name):
        """Queue a recording (call_/vm_ wav or opus) for transcription."""
        if not self.ready():
            return False, "Engine or model not installed"
        with self._lock:
            if name in self._queued or name == self.current:
                return True, "Already queued"
            self._queued.add(name)
        self._q.put(name)
        self._ensure_worker()
        return True, "Queued"

    def maybe_auto_enqueue(self, name):
        """Auto-transcribe hook for freshly saved voicemail messages."""
        if self.auto_vm() and self.ready():
            self.enqueue(name)

    def is_pending(self, name):
        with self._lock:
            return name in self._queued or name == self.current

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work_loop, daemon=True)
            self._worker.start()

    def _work_loop(self):
        while True:
            try:
                name = self._q.get(timeout=300)
            except queue.Empty:
                return
            self.current = name
            try:
                self._transcribe(name)
            except Exception as e:
                _log().error("transcribe: %s failed: %s", name, e)
            finally:
                self.current = None
                with self._lock:
                    self._queued.discard(name)

    def _resolve_audio(self, name):
        """The queued name may still be mid-compression (wav -> opus). Wait
        briefly for the opus to land; fall back to whichever file exists."""
        stem = name[:-5] if name.endswith(".opus") else name[:-4]
        opus = os.path.join(RECORD_DIR, stem + ".opus")
        wav = os.path.join(RECORD_DIR, stem + ".wav")
        for _ in range(30):
            if os.path.exists(opus):
                return opus
            if os.path.exists(wav):
                # give a possibly-running compression a moment to finish
                if time.time() - os.path.getmtime(wav) > 15:
                    return wav
            time.sleep(1)
        return opus if os.path.exists(opus) else (wav if os.path.exists(wav) else None)

    def _transcribe(self, name):
        src = self._resolve_audio(name)
        if src is None:
            _log().error("transcribe: %s vanished before transcription", name)
            return
        real_name = os.path.basename(src)
        model_file = os.path.join(MODELS_DIR, MODELS[self.model()]["file"])
        tmp_wav = os.path.join("/tmp", "transcribe-" + str(os.getpid()) + ".wav")
        out_base = os.path.join(RECORD_DIR, real_name)
        t0 = time.monotonic()
        try:
            # whisper wants 16k mono wav; recordings are already 16k mono so
            # this is a cheap remux for wav and a decode for opus
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                 "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tmp_wav],
                timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                _log().error("transcribe: decode of %s failed: %s", real_name,
                             r.stderr.decode(errors="replace")[:200])
                return
            cmd = [ENGINE_BIN, "-m", model_file, "-f", tmp_wav,
                   "-l", self.lang(), "-t", str(THREADS),
                   "-np", "-nt", "-otxt", "-of", out_base]
            r = subprocess.run(
                cmd, timeout=JOB_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                preexec_fn=lambda: os.nice(10))
            if r.returncode != 0:
                _log().error("transcribe: whisper on %s failed: %s", real_name,
                             r.stderr.decode(errors="replace")[:300])
                return
            _log().info("transcribe: %s done in %.0fs (model %s)",
                        real_name, time.monotonic() - t0, self.model())
        except subprocess.TimeoutExpired:
            _log().error("transcribe: %s timed out", real_name)
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass


transcribe_service = TranscribeService()
