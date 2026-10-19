"""
TtsService - on-device text-to-speech (English + Chinese), used to build a
voicemail greeting from typed text instead of a microphone recording.

Neither synthesizer is part of the firmware; both are downloaded on demand
from dl.photonicat.com and installed under /root/ai. Two engines, because
they sit at opposite ends of the size/quality trade:

  espeak-ng (3 MB, /root/ai/tts)
      Formant synthesizer. No model, faster than real time on any box,
      speaks English/Mandarin/Cantonese -- but audibly robotic.

  kokoro (21 MB engine + 365 MB model, /root/ai/kokoro)
      Kokoro-82M v1.1-zh neural voices run through sherpa-onnx. Natural
      prosody, 100 Chinese speakers plus English, and it handles Chinese and
      English mixed in one sentence. Roughly 3.7x slower than real time on
      an RK3576, which only matters because a greeting is synthesized once
      and saved, not streamed.

The engine bundle carries its own onnxruntime and dependency libraries in
lib/ (the firmware has neither), so it runs via LD_LIBRARY_PATH with nothing
installed system-wide.
"""

import os
import shutil
import subprocess
import threading

import pcat_config
from pcat_util import pcat_logger

AI_DIR = "/root/ai"

# --- espeak-ng ------------------------------------------------------------
TTS_DIR = os.path.join(AI_DIR, "tts")
ENGINE_BIN = os.path.join(TTS_DIR, "espeak-ng")
DATA_DIR = os.path.join(TTS_DIR, "espeak-ng-data")
BUNDLE_URLS = [
    "https://dl.photonicat.com/ai-engines/tts/espeak-ng-linux-aarch64-musl.tar.gz",
]
BUNDLE_SIZE_MB = 4

# Voices exposed in the UI. espeak-ng calls Mandarin "cmn" and Cantonese "yue".
VOICES = [
    {"id": "en",      "label": "English"},
    {"id": "en-us",   "label": "English (US)"},
    {"id": "cmn",     "label": "中文 (普通话)"},
    {"id": "yue",     "label": "中文 (粤语)"},
]
DEFAULT_VOICE = "en"

# --- kokoro (sherpa-onnx) -------------------------------------------------
KOKORO_DIR = os.path.join(AI_DIR, "kokoro")
KOKORO_BIN = os.path.join(KOKORO_DIR, "sherpa-onnx-offline-tts")
KOKORO_LIB = os.path.join(KOKORO_DIR, "lib")
KOKORO_ENGINE_URLS = [
    "https://dl.photonicat.com/ai-engines/tts/kokoro/kokoro-engine-linux-aarch64-musl.tar.gz",
]
KOKORO_ENGINE_SIZE_MB = 21

# Two model builds of the same voices: int8 is half the size and noticeably
# quicker, fp32 is the reference quality the model was released at.
KOKORO_MODELS = {
    "int8": {"dir": "kokoro-int8-multi-lang-v1_1", "onnx": "model.int8.onnx",
             "size_mb": 147, "label": "Kokoro v1.1 int8"},
    "full": {"dir": "kokoro-multi-lang-v1_1", "onnx": "model.onnx",
             "size_mb": 365, "label": "Kokoro v1.1 full"},
}
KOKORO_MODEL_URL_BASE = "https://dl.photonicat.com/ai-engines/tts/kokoro/"
DEFAULT_KOKORO_MODEL = "full"

# Curated subset of the model's 103 speakers (0-2 English, 3-57 Chinese
# female, 58-102 Chinese male); the full list is in the model metadata.
KOKORO_VOICES = [
    {"id": "3",   "label": "中文 女声 1"},
    {"id": "4",   "label": "中文 女声 2"},
    {"id": "5",   "label": "中文 女声 3"},
    {"id": "58",  "label": "中文 男声 1"},
    {"id": "59",  "label": "中文 男声 2"},
    {"id": "60",  "label": "中文 男声 3"},
    {"id": "0",   "label": "English (US female)"},
    {"id": "1",   "label": "English (US female 2)"},
    {"id": "2",   "label": "English (UK female)"},
]
DEFAULT_KOKORO_VOICE = "3"

MAX_TEXT_CHARS = 500
SYNTH_TIMEOUT = 60
KOKORO_SYNTH_TIMEOUT = 600      # ~3.7x real time, plus a 325MB model load
# Matches the voicemail greeting pipeline (16k mono).
RATE = 16000


def _log():
    return pcat_logger.logger


class TtsService:
    def __init__(self):
        self.download = {"kind": None, "done": 0, "total": 0, "error": None}
        self._downloading = False

    # ------------------------------------------------------------- status

    def engine_present(self):
        return os.access(ENGINE_BIN, os.X_OK) and os.path.isdir(DATA_DIR)

    def kokoro_engine_present(self):
        return os.access(KOKORO_BIN, os.X_OK) and os.path.isdir(KOKORO_LIB)

    def kokoro_model_dir(self, model):
        info = KOKORO_MODELS.get(model)
        return os.path.join(AI_DIR, info["dir"]) if info else None

    def kokoro_model_present(self, model):
        """Every file synthesis needs, not just the .onnx -- see the staging
        note in _download_worker."""
        info = KOKORO_MODELS.get(model)
        if not info:
            return False
        d = self.kokoro_model_dir(model)
        return all(os.path.exists(os.path.join(d, f)) for f in (
            info["onnx"], "voices.bin", "tokens.txt",
            "lexicon-us-en.txt", "lexicon-zh.txt", "espeak-ng-data", "dict"))

    def kokoro_model(self):
        m = pcat_config.get_user_config_key("phone_tts_kokoro_model",
                                            DEFAULT_KOKORO_MODEL)
        return m if m in KOKORO_MODELS else DEFAULT_KOKORO_MODEL

    def kokoro_ready(self):
        return self.kokoro_engine_present() and \
            self.kokoro_model_present(self.kokoro_model())

    def engine(self):
        """Which synthesizer to use. Falls back to espeak whenever kokoro is
        selected but not fully installed, so the greeting box never breaks."""
        e = pcat_config.get_user_config_key("phone_tts_engine", "espeak")
        if e == "kokoro" and not self.kokoro_ready():
            return "espeak"
        return "kokoro" if e == "kokoro" else "espeak"

    def voice(self):
        v = pcat_config.get_user_config_key("phone_tts_voice", DEFAULT_VOICE)
        return v if any(x["id"] == v for x in VOICES) else DEFAULT_VOICE

    def kokoro_voice(self):
        v = str(pcat_config.get_user_config_key("phone_tts_kokoro_voice",
                                                DEFAULT_KOKORO_VOICE))
        return v if any(x["id"] == v for x in KOKORO_VOICES) else DEFAULT_KOKORO_VOICE

    def status(self):
        return {
            "engine": self.engine_present(),
            "voice": self.voice(),
            "voices": VOICES,
            "downloading": self._downloading,
            "download": dict(self.download),
            "selected_engine": self.engine(),
            "kokoro": {
                "engine": self.kokoro_engine_present(),
                "ready": self.kokoro_ready(),
                "model": self.kokoro_model(),
                "voice": self.kokoro_voice(),
                "voices": KOKORO_VOICES,
                "models": [
                    {"id": k, "label": v["label"], "size_mb": v["size_mb"],
                     "installed": self.kokoro_model_present(k)}
                    for k, v in KOKORO_MODELS.items()
                ],
                "engine_size_mb": KOKORO_ENGINE_SIZE_MB,
            },
        }

    # ------------------------------------------------------------ install

    def start_download(self):
        """espeak-ng bundle."""
        if self._downloading:
            return False, "A download is already running"
        if self.engine_present():
            return True, "Already installed"
        self._begin("espeak", BUNDLE_SIZE_MB, list(BUNDLE_URLS), TTS_DIR,
                    ENGINE_BIN)
        return True, "Download started"

    def start_kokoro_engine_download(self):
        if self._downloading:
            return False, "A download is already running"
        if self.kokoro_engine_present():
            return True, "Already installed"
        self._begin("kokoro-engine", KOKORO_ENGINE_SIZE_MB,
                    list(KOKORO_ENGINE_URLS), KOKORO_DIR, KOKORO_BIN)
        return True, "Download started"

    def start_kokoro_model_download(self, model):
        info = KOKORO_MODELS.get(model)
        if not info:
            return False, "Unknown model"
        if self._downloading:
            return False, "A download is already running"
        if self.kokoro_model_present(model):
            return True, "Already installed"
        url = KOKORO_MODEL_URL_BASE + info["dir"] + ".tar.bz2"
        # the model tarball already contains its own top-level directory
        self._begin("kokoro-model", info["size_mb"], [url], AI_DIR, None)
        return True, "Download started"

    def _begin(self, kind, size_mb, urls, dest_dir, chmod_target):
        self._downloading = True
        self.download = {"kind": kind, "done": 0,
                         "total": size_mb * 1024 * 1024, "error": None}
        threading.Thread(target=self._download_worker,
                         args=(kind, urls, dest_dir, chmod_target),
                         daemon=True).start()

    def _download_worker(self, kind, urls, dest_dir, chmod_target):
        import requests
        import tarfile
        tmp = os.path.join("/tmp", "tts-" + kind + ".tar")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            last_err = "no source reachable"
            for url in urls:
                try:
                    with requests.get(url, stream=True, timeout=(10, 120),
                                      allow_redirects=True) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("Content-Length", 0))
                        if total:
                            self.download["total"] = total
                        done = 0
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_content(chunk_size=1 << 16):
                                fh.write(chunk)
                                done += len(chunk)
                                self.download["done"] = done
                    # Unpack to a staging dir and only then move into place.
                    # Extracting straight into dest_dir would publish files as
                    # they are written, and the *_present() checks would call a
                    # half-unpacked model ready -- which fails at synthesis
                    # time with a missing voices.bin.
                    staging = os.path.join(dest_dir, ".incoming")
                    shutil.rmtree(staging, ignore_errors=True)
                    os.makedirs(staging, exist_ok=True)
                    with tarfile.open(tmp, "r:*") as tf:
                        # never unpack outside the staging dir
                        safe = [m for m in tf.getmembers()
                                if not (m.name.startswith("/") or ".." in m.name)]
                        tf.extractall(staging, members=safe)
                    for entry in os.listdir(staging):
                        target = os.path.join(dest_dir, entry)
                        if os.path.isdir(target):
                            shutil.rmtree(target, ignore_errors=True)
                        elif os.path.exists(target):
                            os.unlink(target)
                        shutil.move(os.path.join(staging, entry), target)
                    shutil.rmtree(staging, ignore_errors=True)
                    if chmod_target and os.path.exists(chmod_target):
                        os.chmod(chmod_target, 0o755)
                    _log().info("tts: %s installed (%d MB) from %s",
                                kind, done >> 20, url)
                    return
                except Exception as e:
                    last_err = str(e)
                    _log().error("tts: %s download from %s failed: %s",
                                 kind, url, e)
            self.download["error"] = last_err
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._downloading = False

    # ---------------------------------------------------------- synthesis

    def synth(self, text, voice, out_wav, engine=None):
        """Render text to a 16k mono WAV. Returns (ok, error)."""
        text = (text or "").strip()
        if not text:
            return False, "Text is required"
        if len(text) > MAX_TEXT_CHARS:
            return False, "Text too long"

        engine = engine or self.engine()
        if engine == "kokoro" and self.kokoro_ready():
            ok, err = self._synth_kokoro(text, voice, out_wav)
        else:
            ok, err = self._synth_espeak(text, voice, out_wav)
        if not ok:
            return False, err
        self._to_16k_mono(out_wav)
        return True, None

    def _synth_espeak(self, text, voice, out_wav):
        if not self.engine_present():
            return False, "TTS engine not installed"
        if not any(x["id"] == voice for x in VOICES):
            voice = self.voice()
        try:
            # text goes in on stdin, never as an argv element, so no amount of
            # punctuation in it can be read as an option
            r = subprocess.run(
                [ENGINE_BIN, "--path", TTS_DIR, "-v", voice,
                 "-s", "150", "-w", out_wav, "--stdin"],
                input=text.encode("utf-8"), timeout=SYNTH_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0 or not os.path.exists(out_wav):
                err = r.stderr.decode(errors="replace")[:200]
                _log().error("tts: espeak synthesis failed: %s", err)
                return False, err or "Synthesis failed"
        except subprocess.TimeoutExpired:
            return False, "Synthesis timed out"
        except Exception as e:
            return False, str(e)
        return True, None

    def _synth_kokoro(self, text, voice, out_wav):
        model = self.kokoro_model()
        info = KOKORO_MODELS[model]
        d = self.kokoro_model_dir(model)
        sid = voice if any(x["id"] == str(voice) for x in KOKORO_VOICES) \
            else self.kokoro_voice()
        env = dict(os.environ, LD_LIBRARY_PATH=KOKORO_LIB)
        cmd = [
            KOKORO_BIN,
            "--kokoro-model=" + os.path.join(d, info["onnx"]),
            "--kokoro-voices=" + os.path.join(d, "voices.bin"),
            "--kokoro-tokens=" + os.path.join(d, "tokens.txt"),
            "--kokoro-data-dir=" + os.path.join(d, "espeak-ng-data"),
            "--kokoro-dict-dir=" + os.path.join(d, "dict"),
            "--kokoro-lexicon=" + ",".join([
                os.path.join(d, "lexicon-us-en.txt"),
                os.path.join(d, "lexicon-zh.txt")]),
            "--num-threads=4",
            "--sid=" + str(sid),
            "--output-filename=" + out_wav,
            text,
        ]
        try:
            r = subprocess.run(
                cmd, env=env, timeout=KOKORO_SYNTH_TIMEOUT,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                preexec_fn=lambda: os.nice(10))
            if r.returncode != 0 or not os.path.exists(out_wav):
                err = r.stderr.decode(errors="replace")[:300]
                _log().error("tts: kokoro synthesis failed: %s", err)
                return False, err or "Synthesis failed"
        except subprocess.TimeoutExpired:
            return False, "Synthesis timed out"
        except Exception as e:
            return False, str(e)
        return True, None

    def _to_16k_mono(self, wav):
        """espeak writes 22.05k and kokoro 24k; the greeting is played into
        the modem's 16k PCM link, so normalise. Best effort -- a failure here
        leaves a still-playable file."""
        try:
            tmp = wav + ".16k.wav"
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", wav,
                 "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", tmp],
                timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode == 0:
                os.replace(tmp, wav)
            else:
                os.unlink(tmp)
        except Exception as e:
            _log().error("tts: resample failed (keeping original): %s", e)


tts_service = TtsService()
