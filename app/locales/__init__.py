# -*- coding: utf-8 -*-
"""UI string tables, extracted verbatim out of pcat_manager_web.py.

Two data modules (``cn.py`` / ``en.py``) each hold a ``LOCALES`` dict keyed by
page name, plus a shared ``"_base"`` entry. The web handlers used to inline a
~150-line ``if c_locale == "CN": {...} else: {...}`` block per page; they now
call :func:`get_locales` / :func:`get_base_locales`.

Locale selection matches the historical branch exactly: the old code did
``if c_locale == "CN": <CN> else: <EN>`` — i.e. only the literal string ``"CN"``
selected Chinese; anything else (``"EN"``, ``"zh_CN"``, …) fell through to
English. :func:`_table` reproduces that so behavior is unchanged.

Callers must return a COPY (these handlers mutate the dict via ``.update(...)``),
so both accessors return a fresh ``dict`` every call.
"""

from . import cn as _cn
from . import en as _en


def _table(locale):
    """Pick the CN or EN LOCALES table. Only the literal 'CN' means Chinese,
    reproducing the original ``if c_locale == "CN"`` branch precisely."""
    return _cn.LOCALES if locale == "CN" else _en.LOCALES


def get_locales(page, locale):
    """Return a fresh copy of the page-specific string dict for ``locale``.

    ``page`` is the logical page key (e.g. "home", "device_control"). Unknown
    pages (or pages that historically had no dict, like login) return ``{}`` so
    the caller's ``locales.update(base_locales())`` still works."""
    return dict(_table(locale).get(page, {}))


def get_base_locales(locale):
    """Return a fresh copy of the shared base/header/nav dict for ``locale``,
    with the dynamic ``current_locale`` key injected (base_locales() set this
    to the raw locale name — "CN"/"EN"/"zh_CN" — so preserve that value)."""
    base = dict(_table(locale).get("_base", {}))
    base["current_locale"] = locale
    return base
