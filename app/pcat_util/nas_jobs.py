"""
Generic background-job engine for the NAS feature.

Every long-running NAS operation — format, partition, resize, cross-device
copy, big delete, rsync run, SD backup, anything that outlives an HTTP
request — runs through this one table, so the UI has a single job shape to
render and a single reattach story after a page reload.

Design points, in the firmware-upgrade pattern's lineage
(pcat_manager_web.py's status dict + event-bus publish + polling endpoint +
/tmp status file):

- Runners are plain daemon threads, so a job survives its browser tab.
  Progress reaches the UI two ways: a "nas_job_status" event-bus publish on
  every state change (throttled to ~1/s for progress-only updates), and
  GET /api/v1/nas/jobs.json for reattach.
  The emitter is injected (set_emitter) so this module imports clean in unit
  tests and never depends on flask.

- exclusive_key serializes jobs that must not overlap ("disk:nvme0n1": one
  partition/format/resize per disk at a time). submit() refuses with the
  conflicting job id rather than queueing blind, so the UI can say WHY.

- Job state (minus log tails) is persisted atomically to
  /tmp/pcat_nas_jobs.json on every transition. After an app restart,
  startup() reloads the file and marks anything "running" as error
  "interrupted by restart" — a thread cannot be resumed, and pretending
  otherwise would leave a format job showing running forever.

- Destructive jobs (format/partition/resize) are capped at ONE concurrently
  across the whole system; copy-class jobs at two. The router shares one CPU
  with the network stack.
"""

import collections
import json
import os
import secrets
import subprocess
import threading
import time

from pcat_util import pcat_logger

PERSIST_PATH = "/tmp/pcat_nas_jobs.json"

DESTRUCTIVE_KINDS = ("format", "partition", "resize")
_MAX_DESTRUCTIVE = 1
_MAX_COPY_CLASS = 2
_LOG_TAIL = 20
_FINISHED_KEEP = 20
_EMIT_MIN_INTERVAL = 1.0

_lock = threading.RLock()
_jobs = {}          # id -> job dict (internal: includes _cancel Event, _log)
_emitter = None     # callable(job_public_dict) -> None


def set_emitter(fn):
    """Wire the event-bus push (called once from pcat_manager_web at import).
    Unit tests pass a recorder; None disables emission."""
    global _emitter
    _emitter = fn


def _now():
    return time.time()


def _public(job):
    """The API/socket shape — everything the UI contract promises, nothing
    internal."""
    return {
        "id": job["id"],
        "kind": job["kind"],
        "state": job["state"],
        "pct": job["pct"],
        "label": job["label"],
        "label_cn": job["label_cn"],
        "message": job["message"],
        "bytes_done": job["bytes_done"],
        "bytes_total": job["bytes_total"],
        "speed_bps": job["speed_bps"],
        "eta_sec": job["eta_sec"],
        "items_done": job["items_done"],
        "items_total": job["items_total"],
        "meta": job["meta"],
        "log_tail": list(job["_log"]),
        "created": job["created"],
        "started": job["started"],
        "finished": job["finished"],
        "error": job["error"],
        "cancellable": job["cancellable"],
    }


def _persist_locked():
    """Atomic snapshot of job state (sans logs) so a restarted app can tell
    the UI what happened. Same tmp+rename idiom as pcat_config."""
    snapshot = []
    for job in _jobs.values():
        pub = _public(job)
        pub["log_tail"] = []
        snapshot.append(pub)
    tmp = PERSIST_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, PERSIST_PATH)
    except OSError as e:
        pcat_logger.logger.warning("nas_jobs: persist failed: {}".format(e))


def _emit(job, force=False):
    if _emitter is None:
        return
    now = _now()
    if not force and now - job.get("_last_emit", 0) < _EMIT_MIN_INTERVAL:
        return
    job["_last_emit"] = now
    try:
        _emitter(_public(job))
    except Exception as e:
        pcat_logger.logger.warning("nas_jobs: emit failed: {}".format(e))


def _transition(job, state, error=None):
    with _lock:
        job["state"] = state
        if state == "running":
            job["started"] = _now()
        if state in ("done", "error", "cancelled"):
            job["finished"] = _now()
            job["error"] = error
            # A rate from the last live sample would otherwise sit frozen on
            # a finished job.
            job["speed_bps"] = 0
            job["eta_sec"] = None
        _persist_locked()
    _emit(job, force=True)


def _prune_locked():
    finished = sorted(
        (j for j in _jobs.values()
         if j["state"] in ("done", "error", "cancelled")),
        key=lambda j: j["finished"] or 0)
    for job in finished[:-_FINISHED_KEEP] if len(finished) > _FINISHED_KEEP else []:
        del _jobs[job["id"]]


class JobHandle:
    """What a runner sees: progress setters + cancellation + logging.
    Every setter triggers a (throttled) emit so the UI tracks live."""

    def __init__(self, job):
        self._job = job

    @property
    def cancelled(self):
        return self._job["_cancel"].is_set()

    def check_cancel(self):
        if self.cancelled:
            raise JobCancelled()

    def set_progress(self, pct=None, message=None, bytes_done=None,
                     bytes_total=None, items_done=None, items_total=None,
                     speed_bps=None, eta_sec=None):
        job = self._job
        if pct is not None:
            job["pct"] = max(0, min(100, int(pct)))
        if message is not None:
            job["message"] = message
        if bytes_done is not None:
            job["bytes_done"] = int(bytes_done)
        if bytes_total is not None:
            job["bytes_total"] = int(bytes_total)
        if speed_bps is not None:
            job["speed_bps"] = max(0, int(speed_bps))
        if eta_sec is not None:
            job["eta_sec"] = max(0, int(eta_sec))
        if items_done is not None:
            job["items_done"] = int(items_done)
        if items_total is not None:
            job["items_total"] = int(items_total)
        _emit(job)

    def log(self, line):
        self._job["_log"].append(line[-500:] if len(line) > 500 else line)
        _emit(self._job)

    def watch_subprocess(self, argv, line_cb=None, stdin_data=None, env=None):
        """Run a subprocess streaming each stdout line into the job log
        (background_install_thread idiom); SIGTERM it on cancel. Returns the
        exit code. line_cb(line) may parse progress."""
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            text=True, bufsize=1, env=env)
        self._job["_proc"] = proc
        try:
            if stdin_data is not None:
                try:
                    proc.stdin.write(stdin_data)
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line:
                    self.log(line)
                    if line_cb:
                        line_cb(line)
                if self.cancelled:
                    proc.terminate()
            return proc.wait()
        finally:
            self._job["_proc"] = None


class JobCancelled(Exception):
    pass


def submit(kind, label, label_cn, runner, meta=None, exclusive_key=None,
           cancellable=True):
    """Register and start a job. `runner(handle)` runs on a daemon thread;
    its return value is ignored, its exception message becomes job.error.
    Returns (job_id, None) or (None, {"code": ..., "conflict": job_id})."""
    with _lock:
        for job in _jobs.values():
            if job["state"] not in ("queued", "running"):
                continue
            if exclusive_key and job.get("_excl") == exclusive_key:
                return None, {"code": "busy", "conflict": job["id"]}
        active = [j for j in _jobs.values()
                  if j["state"] in ("queued", "running")]
        if kind in DESTRUCTIVE_KINDS and sum(
                1 for j in active if j["kind"] in DESTRUCTIVE_KINDS) >= _MAX_DESTRUCTIVE:
            return None, {"code": "busy_destructive", "conflict": None}
        if kind not in DESTRUCTIVE_KINDS and sum(
                1 for j in active if j["kind"] not in DESTRUCTIVE_KINDS) >= _MAX_COPY_CLASS:
            return None, {"code": "busy_queue", "conflict": None}

        job = {
            "id": "j-" + secrets.token_hex(4),
            "kind": kind,
            "state": "queued",
            "pct": None,
            "label": label,
            "label_cn": label_cn,
            "message": "",
            "bytes_done": 0, "bytes_total": 0,
            "speed_bps": 0, "eta_sec": None,
            "items_done": 0, "items_total": 0,
            "meta": meta or {},
            "created": _now(), "started": None, "finished": None,
            "error": None,
            "cancellable": bool(cancellable),
            "_log": collections.deque(maxlen=_LOG_TAIL),
            "_cancel": threading.Event(),
            "_proc": None,
            "_excl": exclusive_key,
            "_last_emit": 0.0,
        }
        _jobs[job["id"]] = job
        _prune_locked()
        _persist_locked()

    def _run():
        _transition(job, "running")
        try:
            runner(JobHandle(job))
        except JobCancelled:
            _transition(job, "cancelled")
            return
        except Exception as e:
            pcat_logger.logger.warning(
                "nas_jobs: {} ({}) failed: {}".format(job["id"], kind, e))
            _transition(job, "error", error=str(e))
            return
        if job["_cancel"].is_set():
            _transition(job, "cancelled")
        else:
            job["pct"] = 100 if job["pct"] is not None else None
            _transition(job, "done")

    threading.Thread(target=_run, daemon=True,
                     name="nas-job-" + job["id"]).start()
    _emit(job, force=True)
    return job["id"], None


def get(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def list_jobs():
    """Running + queued first, then the newest finished."""
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return [_public(j) for j in jobs]


def cancel(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"code": "not_found"}
        if job["state"] not in ("queued", "running"):
            return {"code": "not_running"}
        if not job["cancellable"]:
            return {"code": "not_cancellable"}
        job["_cancel"].set()
        proc = job.get("_proc")
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass
    return {"code": "ok"}


def dismiss(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"code": "not_found"}
        if job["state"] in ("queued", "running"):
            return {"code": "still_running"}
        del _jobs[job_id]
        _persist_locked()
    return {"code": "ok"}


def active_count():
    with _lock:
        return sum(1 for j in _jobs.values()
                   if j["state"] in ("queued", "running"))


def startup():
    """Reload the persisted table; a 'running' job from a previous process
    is dead by definition — surface that instead of a stuck spinner."""
    try:
        with open(PERSIST_PATH) as f:
            snapshot = json.load(f)
    except (OSError, ValueError):
        return
    with _lock:
        for pub in snapshot:
            if not isinstance(pub, dict) or "id" not in pub:
                continue
            job = dict(pub)
            if job.get("state") in ("queued", "running"):
                job["state"] = "error"
                job["error"] = "interrupted by restart"
                job["finished"] = _now()
            job["_log"] = collections.deque(job.pop("log_tail", []) or [],
                                            maxlen=_LOG_TAIL)
            job["_cancel"] = threading.Event()
            job["_proc"] = None
            job["_excl"] = None
            job["_last_emit"] = 0.0
            job.setdefault("cancellable", False)
            # Fields added after a release: a table persisted by an older
            # build lacks them, and _public() indexes them directly.
            job.setdefault("speed_bps", 0)
            job.setdefault("eta_sec", None)
            _jobs[job["id"]] = job
        _prune_locked()
        _persist_locked()
