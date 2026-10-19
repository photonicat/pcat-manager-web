"""Which package format this firmware installs: IPK (opkg) or APK (apk-tools).

photonicatWrt 26.04 images ship opkg and install .ipk; images built from
master ship apk-tools and install .apk. A device only ever has one of the
two, so the /update page, the upload/download endpoints and opkg-install.py
all ask here and accept that one format -- an .ipk cannot be installed by
apk, nor an .apk by opkg.
"""

import os
import re
import shutil

IPK = "ipk"
APK = "apk"

# Archives are unpacked and every package of the device's format inside is
# installed.
ARCHIVE_EXTENSIONS = (".tar", ".gz", ".xz", ".bz2")

_APK_DB = "/lib/apk/db/installed"

_cached = None


def detect():
    """Return IPK or APK for this device. Cached: it cannot change at runtime.

    apk wins when both are present: an apk image may still carry an opkg
    shim, but its package database is apk's."""
    global _cached
    if _cached is None:
        if shutil.which("apk") and os.path.exists(_APK_DB):
            _cached = APK
        else:
            _cached = IPK
    return _cached


def extension(fmt=None):
    return "." + (fmt or detect())


def label(fmt=None):
    """Upper-case name for UI text: "IPK" or "APK"."""
    return (fmt or detect()).upper()


def accepted_extensions(fmt=None):
    """File extensions an upload/download may have on this device."""
    return (extension(fmt),) + ARCHIVE_EXTENSIONS


def is_package(name, fmt=None):
    return name.lower().endswith(extension(fmt))


def is_archive(name):
    return name.lower().endswith(ARCHIVE_EXTENSIONS)


def is_accepted(name, fmt=None):
    return name.lower().endswith(accepted_extensions(fmt))


def install_command(files, fmt=None):
    """argv installing local package files with the device's package manager.

    opkg: --force-depends keeps the old behaviour of installing even when a
    dependency is missing. apk has no equivalent; --allow-untrusted is needed
    because third-party .apk files are not signed with the image's key."""
    if (fmt or detect()) == APK:
        return ["apk", "add", "--allow-untrusted", "--force-overwrite", "--no-network"] + list(files)
    return ["opkg", "install", "--force-overwrite", "--force-depends"] + list(files)


def list_installed_command(fmt=None):
    if (fmt or detect()) == APK:
        return ["apk", "list", "-I"]
    return ["opkg", "list-installed"]


# apk package ids are "<name>-<version>-r<release>"; the name itself may
# contain dashes and digits, so anchor on the version's shape at the end.
_APK_ID = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*-r\d+)$")


def parse_installed(output, fmt=None):
    """Parse list_installed_command() output to [(name, version), ...].

    opkg:  "pcat-manager - 2.0.0-63"
    apk:   "pcat-manager-2.0.0-r63 aarch64_generic {feeds/...} (GPL-3.0) [installed]"
    """
    fmt = fmt or detect()
    packages = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if fmt == APK:
            m = _APK_ID.match(line.split()[0])
            if m:
                packages.append((m.group("name"), m.group("version")))
        else:
            parts = line.split(" - ")
            if len(parts) >= 2:
                packages.append((parts[0], parts[1]))
    return packages


def parse_package_file_name(file_name, fmt=None):
    """(name, version) from a package file name, or None if it is not one of
    this device's packages.

    ipk:  "pcat-manager_1.0.2-0_aarch64_cortex-a53.ipk"
    apk:  "pcat-manager-2.0.0-r63.apk"
    """
    fmt = fmt or detect()
    base = os.path.basename(file_name.strip())
    if not is_package(base, fmt):
        return None
    stem = base[:-len(extension(fmt))]
    if fmt == APK:
        m = _APK_ID.match(stem)
        return (m.group("name"), m.group("version")) if m else None
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    return (parts[0], parts[1])


def http_get(url, verify_tls=False, **kwargs):
    """requests.get for package downloads and version lists.

    HTTPS certificates are not verified unless verify_tls is set: devices
    often come up with a wrong clock before NTP sync, and package mirrors are
    often internal or self-signed hosts -- both fail verification even though
    the user explicitly asked for that URL."""
    import requests
    if not verify_tls:
        import warnings
        from urllib3.exceptions import InsecureRequestWarning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return requests.get(url, verify=False, **kwargs)
    return requests.get(url, verify=True, **kwargs)
