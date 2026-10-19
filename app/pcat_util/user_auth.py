#@Author    : Qichunren

import logging
import os
import pam
import pcat_config

try:
  from passlib.hosts import host_context
except:
  print("Failed to: from passlib.hosts import host_context")

# https://passlib.readthedocs.io/en/stable/lib/passlib.hosts.html
# This function need root permission, so flask app need run as root


def _auth_logger():
  """The project logger, imported lazily: pcat_util.pcat_logger opens the log
  file at import time, which the unit tests (stubbed pcat_config) cannot do.
  Fall back to a plain stdlib logger of the same name in that case."""
  try:
    from pcat_util import pcat_logger
    return pcat_logger.logger
  except Exception:
    return logging.getLogger("pcat_logger")


def _log_pam_error(exc):
  """Record a PAM failure with its traceback. Never include the password."""
  _auth_logger().error("PAM authentication raised %s: %s",
                       type(exc).__name__, exc, exc_info=True)


def unix_user_auth(user_name, raw_password):
  if pcat_config.skip_auth:
    return True

  if user_name is None or raw_password is None:
    return False

  # Fail closed. python-pam raises instead of returning False for some inputs
  # (ValueError on a NUL byte, decode errors on the PAM reason string, ctypes
  # errors from the underlying libpam). Left uncaught, that escapes the login
  # view and the user sees a 500 page instead of "invalid password". Log the
  # traceback so a genuinely broken PAM stack is still diagnosable, then deny.
  try:
    return pam.authenticate(user_name, raw_password)
  except Exception as exc:
    _log_pam_error(exc)
    return False
