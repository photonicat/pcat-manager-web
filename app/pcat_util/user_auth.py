#@Author    : Qichunren

import os
import pam
import pcat_config

try:
  from passlib.hosts import host_context
except:
  print("Failed to: from passlib.hosts import host_context")

# https://passlib.readthedocs.io/en/stable/lib/passlib.hosts.html
# This function need root permission, so flask app need run as root

def unix_user_auth(user_name, raw_password):
  if pcat_config.skip_auth:
    return True
    
  if user_name is None or raw_password is None:
    return False

  return pam.authenticate(user_name, raw_password)
