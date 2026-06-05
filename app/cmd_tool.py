#@Author    : Qichunren

import subprocess
import threading
import time


class CmdTool (threading.Thread):
  
  def __init__(self):
    threading.Thread.__init__(self)
    self.commands = []

  def commands_length(self):
    return len(self.commands)

  # cmd item is an array
  def prepare(self, cmd):
    if cmd:
      self.commands.append(cmd)

  def execute(self):
    if len(self.commands) == 0:
      return
    for cmd in self.commands:
      print("Exec:", cmd)
      try:
        subprocess.run(cmd)
      except Exception as e:
        print("Got cmd error")
        print(e)
      time.sleep(0.2)
    print("Command finished.")


  # When invoke Thread#start function, the 'run' function will be executed.
  def run(self):
    self.execute()

