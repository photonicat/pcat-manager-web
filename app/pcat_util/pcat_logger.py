#@Author    : qichunren

import sys
import logging
from logging.handlers import RotatingFileHandler
import pcat_config

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

# create logger
logger = logging.getLogger('pcat_logger')

# set logging level
logger.setLevel(logging.DEBUG)

# create rotating file handler and set level to debug
handler = RotatingFileHandler(pcat_config.log_file, maxBytes=1024*1024*10, backupCount=2)
handler.setLevel(logging.DEBUG)

# create formatter
formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(threadName)s : %(message)s')

# add formatter to rotating file handler
handler.setFormatter(formatter)

# add ch to logger
logger.addHandler(handler)

# create colored stdout handler - only if not disabled
import os
if os.environ.get('PCAT_LOG_STDOUT', '1') != '0':
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    colored_formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    stdout_handler.setFormatter(colored_formatter)
    logger.addHandler(stdout_handler)