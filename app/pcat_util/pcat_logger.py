#@Author    : qichunren

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
import pcat_config

# Log level is configurable via PCAT_LOG_LEVEL (DEBUG/INFO/WARNING/ERROR).
# Default is INFO: DEBUG formats and writes hot-path messages to flash and
# stdout constantly, which costs measurable CPU on the target ARM device.
_log_level = getattr(logging, os.environ.get('PCAT_LOG_LEVEL', 'INFO').upper(), logging.INFO)

class ColoredFormatter(logging.Formatter):
    """Colored log formatter."""
    
    # ANSI color codes
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[34m',      # Blue
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m'   # Magenta
    }
    RESET = '\033[0m'  # Reset to default color
    
    def format(self, record):
        # Get the original formatted message
        log_message = super().format(record)
        
        # Add color based on log level
        log_color = self.COLORS.get(record.levelname, '')
        return f"{log_color}{log_message}{self.RESET}"

class SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the log owner-only (0600). The stock
    handler creates it 0644 (world-readable), but the log can hold network
    config, IPs and operational detail that other local users on the router
    should not read -- defence in depth alongside the web layer's credential
    scrubbing. Chmod on every _open() so the active file and each post-rotation
    file are both locked down; rotated backups inherit the mode via rename."""
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream

# create logger
logger = logging.getLogger('pcat_logger')

# set logging level
logger.setLevel(_log_level)

# create rotating file handler (owner-only mode)
handler = SecureRotatingFileHandler(pcat_config.log_file, maxBytes=1024*1024*10, backupCount=2)
handler.setLevel(_log_level)

# Lock down any pre-existing backups left 0644 by an older build.
for _suffix in (".1", ".2"):
    try:
        os.chmod(pcat_config.log_file + _suffix, 0o600)
    except OSError:
        pass

# create formatter
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s')

# add formatter to rotating file handler
handler.setFormatter(formatter)

# add ch to logger
logger.addHandler(handler)

# create colored stdout handler - opt-in via PCAT_LOG_STDOUT=1 (dev-start.sh
# sets it). Production default is file-only: formatting and writing every log
# line to stdout as well costs CPU and the service stdout goes nowhere useful.
if os.environ.get('PCAT_LOG_STDOUT', '0') == '1':
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(_log_level)
    colored_formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stdout_handler.setFormatter(colored_formatter)
    logger.addHandler(stdout_handler)