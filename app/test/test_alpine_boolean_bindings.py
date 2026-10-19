"""Lint: Alpine boolean-attribute bindings must evaluate to a real boolean.

Alpine 3 (app/static/alpine-3.15.12.js) removes a bound attribute only when
the value is exactly false / null / undefined:

    [null, void 0, !1].includes(r) && pi(e) ? el.removeAttribute(e)
                                            : (isBooleanAttr(e) && (r = e), set(el, e, r))

Every other value — crucially the empty string — SETS the attribute, and for a
boolean attribute the value becomes the attribute name (disabled="disabled").

So `:disabled="expect && typed != expect"` is a trap: when `expect` is "" the
expression yields "" (not false) and the button is permanently disabled. That
shipped in the shared confirm modal and greyed out Confirm on every dialog
that asks for no typed confirmation.

The check: for each binding, substitute every operand with a falsy value OF
ITS DECLARED TYPE (read from the `Alpine.data(...)` literals, so a boolean
flag stays a boolean and only genuine string/number guards get "" / 0), then
assert the expression still yields a strict boolean. The fix for a violation
is normally `!!( ... )`, or reordering so a comparison ends the chain.
"""

import json
import os
import re
import shutil
import subprocess
import unittest

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "templates")

# The boolean attributes Alpine special-cases that we actually bind.
BOOL_ATTRS = ("disabled", "checked", "readonly", "required", "selected",
              "multiple", "open")

BINDING_RE = re.compile(
    r'(?::|x-bind:)(' + "|".join(BOOL_ATTRS) + r')="([^"]*)"')

# Identifier paths, e.g. `foo`, `foo.bar`, `foo.bar.baz`.
PATH_RE = re.compile(r"\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\b")

JS_RESERVED = {
    "true", "false", "null", "undefined", "typeof", "instanceof", "in",
    "new", "this", "void", "delete", "return", "function", "await",
    "Math", "Number", "String", "Boolean", "Array", "Object", "JSON",
}

# `key: "..."` / `key: 0` / `key: false` inside an Alpine.data literal. Only
# scalar defaults are useful here; anything else is left "unknown".
DECL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:\s*(\"[^\"]*\"|'[^']*'"
                     r"|-?\d+(?:\.\d+)?|true|false)\s*[,\n}]")
# `foo: {` opens a nested literal, so `foo.bar: ""` can be attributed.
NEST_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:\s*\{")


def _declared_types():
    """{'confirm.expect': 'string', 'busy': 'string', ...} across templates.

    A deliberately small parser: it walks each template's text tracking a
    one-level object-literal prefix, which is exactly the shape the Alpine
    data blocks in this codebase use ( `confirm: { expect: "", ... }` ).
    Conflicting declarations across pages resolve to 'string', because a
    guard that is a string ANYWHERE is a guard that can leak a string.
    """
    types = {}

    def note(path, literal):
        if literal in ("true", "false"):
            kind = "boolean"
        elif literal[0] in "\"'":
            kind = "string"
        else:
            kind = "number"
        if types.get(path) in (None, "boolean") or kind == "string":
            types[path] = kind

    for fname in sorted(os.listdir(TEMPLATES)):
        if not fname.endswith(".html"):
            continue
        with open(os.path.join(TEMPLATES, fname), encoding="utf-8") as fh:
            text = fh.read()
        for m in DECL_RE.finditer(text):
            key, literal = m.group(1), m.group(2)
            note(key, literal)
            # Attribute it to the nearest enclosing `parent: {` too.
            head = text.rfind("{", 0, m.start())
            if head != -1:
                nest = NEST_RE.search(text, max(0, head - 80), head + 1)
                if nest:
                    note("{}.{}".format(nest.group(1), key), literal)
    return types


def _collect_bindings():
    out = []
    for fname in sorted(os.listdir(TEMPLATES)):
        if not fname.endswith(".html"):
            continue
        with open(os.path.join(TEMPLATES, fname), encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for attr, expr in BINDING_RE.findall(line):
                    out.append((fname, lineno, attr, expr))
    return out


def _falsy_for(path, types):
    kind = types.get(path)
    if kind == "string":
        return '""'
    if kind == "number":
        return "0"
    return "false"          # declared boolean, or unknown: assume boolean


def _scope_js(expr, types):
    """JS declaring every path in `expr` with a falsy value of its type.

    A name used as a call (`mcuBusy()`) becomes a function returning false: a
    helper that gates a button returns a boolean by contract, and we are
    hunting raw string/number guards, not helper return types.
    """
    roots = {}
    for path in PATH_RE.findall(expr):
        head = path.split(".")[0]
        if head in JS_RESERVED:
            continue
        is_call = re.search(re.escape(path) + r"\s*\(", expr) is not None
        entry = roots.setdefault(head, {"paths": set(), "call": False})
        if is_call:
            entry["call"] = True
        else:
            entry["paths"].add(path)

    lines = []
    for head, info in roots.items():
        if info["call"] and not info["paths"]:
            lines.append("var {} = function(){{ return false; }};".format(head))
            continue
        simple = [p for p in info["paths"] if "." not in p]
        nested = sorted(p for p in info["paths"] if "." in p)
        if simple and not nested:
            lines.append("var {} = {};".format(head, _falsy_for(head, types)))
            continue
        lines.append("var {} = {{}};".format(head))
        for path in nested:
            parts = path.split(".")
            for depth in range(1, len(parts)):
                prefix = ".".join(parts[:depth + 1])
                if depth < len(parts) - 1:
                    lines.append("if (!{p}) {p} = {{}};".format(p=prefix))
                else:
                    lines.append("{p} = {v};".format(
                        p=prefix, v=_falsy_for(path, types)))
    return "\n".join(lines)


@unittest.skipIf(shutil.which("node") is None, "node not available")
class TestAlpineBooleanBindings(unittest.TestCase):

    def test_declared_types_are_found(self):
        types = _declared_types()
        # The guard that caused the bug must be seen as a string, else the
        # check below silently passes for the wrong reason.
        self.assertEqual(types.get("confirm.expect"), "string", types)

    def test_bindings_yield_strict_booleans(self):
        types = _declared_types()
        bindings = _collect_bindings()
        self.assertTrue(bindings, "no boolean bindings found — regex broke?")

        cases = [{"file": f, "line": n, "attr": a, "expr": e,
                  "js": _scope_js(e, types)}
                 for f, n, a, e in bindings]

        script = """
const cases = %s;
const bad = [];
for (const c of cases) {
  let result;
  try {
    result = eval("(function(){ " + c.js + " return (" + c.expr + "); })()");
  } catch (e) {
    continue;                       // not evaluable in isolation; skip
  }
  if (typeof result !== "boolean" && result !== null && result !== undefined) {
    bad.push({file: c.file, line: c.line, attr: c.attr, expr: c.expr,
              got: JSON.stringify(result)});
  }
}
console.log(JSON.stringify(bad));
""" % json.dumps(cases)

        proc = subprocess.run(["node", "-e", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bad = json.loads(proc.stdout.strip() or "[]")
        if bad:
            msg = ["{} boolean binding(s) can yield a non-boolean — Alpine "
                   "sets the attribute for any value except false/null/"
                   "undefined, so these stay disabled/checked forever:"
                   .format(len(bad))]
            for b in bad:
                msg.append("  {}:{}  :{}=\"{}\"  ->  {}".format(
                    b["file"], b["line"], b["attr"], b["expr"], b["got"]))
            msg.append("Fix: wrap in !!( ... ) or end the chain with a "
                       "comparison.")
            self.fail("\n".join(msg))


if __name__ == "__main__":
    unittest.main()
