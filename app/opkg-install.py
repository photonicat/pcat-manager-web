#!/usr/bin/env python3
"""Install a package file, a URL, or an archive of packages.

    opkg-install.py install <file-or-url> [--verify-tls]

Despite the name this drives whichever package manager the firmware has:
opkg for .ipk on 26.04 images, apk for .apk on master images (see
pcat_util/pkg_format.py). A device only accepts its own format.

Archives (tar, tar.gz, tar.xz, tar.bz2) are unpacked and every package of the
device's format inside is installed in one transaction.

HTTPS certificates are NOT verified unless --verify-tls is given: devices
often boot with a wrong clock (no RTC sync yet) and package mirrors are often
on self-signed or internal hosts, both of which fail verification.

The result lines are matched by templates/update.html, so keep
"Package installed successfully.", "installation completed with warnings"
and "installation failed" stable.
"""

import shutil
import pathlib
import sys
import os
import subprocess

from pcat_util import pkg_format

# Overridable so tests can run the script off-device.
workspace = os.environ.get("PCAT_PKG_WORKSPACE", "/cmd-shell")
MAX_SIZE_MB = 500.0


def run_install(files):
  cmd = pkg_format.install_command(files)
  print("Running command:", " ".join(cmd))
  print("=" * 50)
  process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, bufsize=1)
  for line in process.stdout:
    print(line.rstrip())
  return_code = process.wait()
  print("=" * 50)
  print(f"Command completed with return code: {return_code}")
  # opkg exits 255 when some packages installed and others did not.
  if return_code == 0:
    print("Package installed successfully.")
  elif return_code == 255 and pkg_format.detect() == pkg_format.IPK:
    print(f"Package installation completed with warnings (return code: {return_code})")
  else:
    print(f"Package installation failed with return code: {return_code}")


def download(url, verify_tls):
  print("Start downloading " + url)
  if not verify_tls:
    print("(HTTPS certificate verification is off)")
  try:
    r = pkg_format.http_get(url, verify_tls=verify_tls, allow_redirects=True, timeout=60)
    print("Download result: " + str(r.status_code))
    r.raise_for_status()
  except Exception as e:
    print("Failed to download: {}".format(e))
    print("Package installation failed")
    exit(2)

  save_dir_path = pathlib.Path(__file__).parent.resolve().joinpath("temp")
  try:
    save_dir_path.mkdir(parents=True, exist_ok=True)
  except Exception:
    print("Failed to create temp dir")
    exit(3)
  save_file_path = save_dir_path.joinpath(os.path.basename(url.split("?")[0]))
  save_file_path.write_bytes(r.content)
  print("File downloaded.")
  return str(save_file_path)


def main(argv):
  # stdout is a pipe to the web app, which streams it to the page line by
  # line; block buffering would hold the log back until the install ends.
  sys.stdout.reconfigure(line_buffering=True)
  if len(argv) < 2:
    print("Missing cmd argument! Available cmd: install")
    exit(1)
  if argv[1] != "install":
    print("Not support cmd '{}'".format(argv[1]))
    exit(1)
  args = [a for a in argv[2:] if not a.startswith("--")]
  verify_tls = "--verify-tls" in argv[2:]
  if not args:
    print("Missing file argument!")
    exit(1)

  fmt_label = pkg_format.label()
  print("Package format on this device: {} ({})".format(
      fmt_label, " ".join(pkg_format.install_command([])[:2])))

  try:
    pathlib.Path(workspace + "/out").mkdir(parents=True, exist_ok=True)
  except Exception:
    print("Failed to create dir in {}".format(workspace))
    exit(2)
  os.chdir(workspace)
  print("Current working dir {}".format(os.getcwd()))

  filename = args[0]
  if filename.startswith("http://") or filename.startswith("https://"):
    filename = download(filename, verify_tls)

  if not os.path.exists(filename):
    print("File '{}' not found!".format(filename))
    exit(2)

  shutil.rmtree('out', ignore_errors=True)

  file_size = os.path.getsize(filename) / 1024.0 / 1024.0
  print("File size {} M".format(round(file_size, 2)))
  if file_size > MAX_SIZE_MB:
    print("Not support upload file size larger than {} M".format(int(MAX_SIZE_MB)))
    print("Package installation failed")
    exit(3)

  if pkg_format.is_package(filename):
    print("Install {} file".format(fmt_label))
    run_install([filename])
  elif pkg_format.is_archive(filename):
    print("Process archive file ...")
    extract_dir = "out"
    try:
      shutil.unpack_archive(filename, extract_dir)
    except Exception as e:
      print("Failed to unpack archive: {}".format(e))
      print("Package installation failed")
      exit(4)
    ext = pkg_format.extension()
    files = sorted(os.getcwd() + "/" + str(p)
                   for p in pathlib.Path(extract_dir).rglob("*" + ext))
    print(f"Found {len(files)} {fmt_label} files in archive:")
    for i, file in enumerate(files, 1):
      print(f"  {i}. {os.path.basename(file)}")
    print()
    if files:
      run_install(files)
    else:
      print("No {} files found in archive. Package installation failed".format(fmt_label))
    try:
      print("Clean temp files ...")
      shutil.rmtree(workspace, ignore_errors=True)
    except Exception:
      print("Failed to clean extracted files")
    try:
      print("Remove", filename)
      os.remove(filename)
    except Exception:
      print("Failed to remove", filename)
    print("Done.")
  else:
    other = pkg_format.APK if pkg_format.detect() == pkg_format.IPK else pkg_format.IPK
    if pkg_format.is_package(filename, other):
      print("This firmware installs {} packages; {} files are not supported."
            .format(fmt_label, pkg_format.label(other)))
    else:
      print("Not support file format!")
    print("Package installation failed")
    exit(4)


if __name__ == "__main__":
  main(sys.argv)
