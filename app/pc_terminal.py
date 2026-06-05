"""
PTY Terminal Session Manager

Manages persistent PTY sessions for web terminal access.
Sessions persist until the Flask server restarts or the device power cycles.
"""

import os
import pty
import select
import signal
import struct
import fcntl
import termios
import subprocess
import threading
import time
from pcat_util import pcat_logger

class PtySession:
    """A single PTY session with a shell process."""

    SCROLLBACK_MAX = 32768  # Keep last 32KB of output for reconnect replay

    def __init__(self, session_id, cols=80, rows=24):
        self.session_id = session_id
        self.master_fd = None
        self.pid = None
        self.cols = cols
        self.rows = rows
        self.created_at = time.time()
        self.last_active = time.time()
        self._alive = False
        self._scrollback = b''
        self._scrollback_lock = threading.Lock()
        self._spawn()

    def _spawn(self):
        """Fork a child process with a PTY."""
        pid, fd = pty.fork()
        if pid == 0:
            # Child process
            env = os.environ.copy()
            env['TERM'] = 'xterm-256color'
            env['COLUMNS'] = str(self.cols)
            env['LINES'] = str(self.rows)
            env.setdefault('HOME', '/root')
            env.setdefault('PATH', '/usr/sbin:/usr/bin:/sbin:/bin')
            shell = '/bin/sh'
            if os.path.exists('/bin/bash'):
                shell = '/bin/bash'
            elif os.path.exists('/bin/ash'):
                shell = '/bin/ash'
            os.execvpe(shell, [shell, '-l'], env)
        else:
            # Parent process
            self.pid = pid
            self.master_fd = fd
            self._alive = True
            # Set initial window size
            self.resize(self.cols, self.rows)
            # Set non-blocking
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            pcat_logger.logger.info(f"Terminal session {self.session_id} spawned, pid={pid}")

    def resize(self, cols, rows):
        """Resize the PTY window."""
        self.cols = cols
        self.rows = rows
        if self.master_fd is not None:
            try:
                winsize = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def write(self, data):
        """Write data to the PTY master (input from user)."""
        self.last_active = time.time()
        if self.master_fd is not None and self._alive:
            try:
                os.write(self.master_fd, data.encode('utf-8') if isinstance(data, str) else data)
            except OSError:
                self._alive = False

    def read(self, timeout=0.05):
        """Read available data from the PTY master (output from shell)."""
        if self.master_fd is None or not self._alive:
            return None
        try:
            ready, _, _ = select.select([self.master_fd], [], [], timeout)
            if ready:
                data = os.read(self.master_fd, 65536)
                if data:
                    self.last_active = time.time()
                    return data
                else:
                    self._alive = False
                    return None
            return b''
        except (OSError, ValueError):
            self._alive = False
            return None

    def append_scrollback(self, data):
        """Append output data to the scrollback buffer."""
        with self._scrollback_lock:
            self._scrollback += data
            if len(self._scrollback) > self.SCROLLBACK_MAX:
                self._scrollback = self._scrollback[-self.SCROLLBACK_MAX:]

    def get_scrollback(self):
        """Return the full scrollback buffer."""
        with self._scrollback_lock:
            return self._scrollback

    @property
    def is_alive(self):
        """Check if the child process is still running."""
        if not self._alive:
            return False
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid != 0:
                self._alive = False
                return False
            return True
        except ChildProcessError:
            self._alive = False
            return False

    def kill(self):
        """Terminate the session."""
        self._alive = False
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        pcat_logger.logger.info(f"Terminal session {self.session_id} killed")


class TerminalManager:
    """Manages multiple PTY sessions, keyed by session_id."""

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id, cols=80, rows=24):
        """Get an existing session or create a new one."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess and sess.is_alive:
                return sess
            # Clean up dead session if it exists
            if sess:
                sess.kill()
            sess = PtySession(session_id, cols, rows)
            self._sessions[session_id] = sess
            return sess

    def get(self, session_id):
        """Get a session by ID, or None if not found/dead."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess and sess.is_alive:
                return sess
            if sess:
                sess.kill()
                del self._sessions[session_id]
            return None

    def kill(self, session_id):
        """Kill and remove a session."""
        with self._lock:
            sess = self._sessions.pop(session_id, None)
            if sess:
                sess.kill()

    def list_sessions(self):
        """Return list of active session IDs."""
        with self._lock:
            alive = []
            dead = []
            for sid, sess in self._sessions.items():
                if sess.is_alive:
                    alive.append(sid)
                else:
                    dead.append(sid)
            for sid in dead:
                self._sessions[sid].kill()
                del self._sessions[sid]
            return alive

    def cleanup(self):
        """Kill all sessions."""
        with self._lock:
            for sess in self._sessions.values():
                sess.kill()
            self._sessions.clear()


# Global terminal manager instance
terminal_manager = TerminalManager()
