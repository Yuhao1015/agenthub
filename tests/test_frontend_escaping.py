"""HTML escaping in the two frontends (2026-08-24 复核轮), structural +
behavioral guard.

The bug this locks down: esc() escaped only & < >, but its output is in
several places concatenated INTO an HTML attribute value --
settings.html's run-history "复制目标" button does
`data-copy="'+esc(r.goal)+'"`. A goal containing a double quote therefore
closed the attribute and could add arbitrary ones, e.g.

    做个网页" onmouseover="alert(1)" x="
    -> <button ... data-copy="做个网页" onmouseover="alert(1)" x="">

and goal text is not user-typed-only: POST /chat spawns the engine with
the MANAGER LLM's proposed goal, so this is agent-influenced content
reaching the settings page's DOM. That matters because this project
already treats agent output as untrusted elsewhere -- the deliverable
preview iframe is deliberately given allow-scripts WITHOUT
allow-same-origin so an agent-written page cannot touch the host DOM.

Both frontends are pure inline JS and the repo has no JS test
infrastructure (no package.json, no headless browser), matching the
reasoning in tests/test_desktop_notifications.py. So: text-structure
assertions always, plus a real behavioral check through `node` when it
happens to be installed (skipped honestly otherwise, same as
tests/test_git_diff.py's real-git test).

No framework, plain asserts. Zero LLM calls.

Run: python3 tests/test_frontend_escaping.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTENDS = ("dashboard.html", "settings.html")


def _src(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _esc_decl(src: str) -> str:
    m = re.search(r"const esc=s=>String.*?\}\[c\]\)\);", src, re.S)
    assert m, "could not find the esc() definition"
    return m.group(0)


def test_esc_covers_quotes_in_both_frontends():
    """The character class must include both quote styles -- esc() output
    lands inside attribute values, where & < > alone is not enough."""
    for name in FRONTENDS:
        decl = _esc_decl(_src(name))
        for ch, ent in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                        ('"', "&quot;"), ("'", "&#39;")):
            assert ent in decl, f"{name}: esc() does not emit {ent} (for {ch!r})"
        # the regex class itself, not just the lookup table
        m = re.search(r"replace\(/\[([^\]]+)\]/g", decl)
        assert m, f"{name}: unexpected esc() shape: {decl}"
        cls = m.group(1)
        assert '"' in cls and "'" in cls, \
            f"{name}: esc() regex class {cls!r} misses a quote character"


# Functions allowed to build an attribute value without esc(), because
# each is itself an attribute-safe producer. Neither is taken on trust:
#   fileIcon  -> test_constant_only_attribute_helpers_stay_constant
#                (may only return literals from the FILE_ICON table)
#   safeHref  -> test_markdown_renderer_still_only_emits_safe_attributes
#                (scheme allowlist; anything else becomes "#")
# so a future edit that makes one of them echo its input fails THAT test
# rather than being silently covered by this allowlist.
_CONSTANT_ATTR_HELPERS = ("fileIcon", "safeHref")


def test_attribute_sites_all_go_through_esc():
    """Every place that concatenates into an HTML attribute value must use
    esc(). Catches a NEW unescaped attribute site being added later."""
    pat = re.compile(r'=\\?"\'\s*\+\s*([A-Za-z_$][\w$.]*)\s*\(')
    allowed = ("esc", "encodeURIComponent") + _CONSTANT_ATTR_HELPERS
    for name in FRONTENDS:
        for lineno, line in enumerate(_src(name).splitlines(), 1):
            for m in pat.finditer(line):
                fn = m.group(1)
                assert fn in allowed, (
                    f"{name}:{lineno} builds an attribute value with {fn}() "
                    f"instead of esc()/encodeURIComponent(): {line.strip()[:120]}")


def test_constant_only_attribute_helpers_stay_constant():
    """fileIcon(p) feeds data-lucide="..." unescaped. That is only safe
    while every value it can return is a literal in the FILE_ICON table
    (plus the "file" fallback) -- it must never return any part of `p`."""
    src = _src("dashboard.html")
    m = re.search(r"const fileIcon=p=>\{(.*?)\};", src, re.S)
    assert m, "fileIcon not found (or its shape changed -- re-verify by hand)"
    body = m.group(1)
    # every `return` in the body returns either a table cell or a literal
    returns = re.findall(r"return\s+([^;]+);", body)
    assert returns, body
    for r in returns:
        r = r.strip()
        assert re.fullmatch(r'r\[\d+\]', r) or re.fullmatch(r'"[A-Za-z0-9_-]*"', r), \
            f"fileIcon may now return non-constant {r!r}; it is used UNESCAPED in an attribute"
    table = re.search(r"FILE_ICON\s*=\s*\[(.*?)\];", src, re.S)
    assert table, "FILE_ICON table not found"
    for val in re.findall(r",\s*\"([^\"]*)\"\s*\]", table.group(1)):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", val), \
            f"FILE_ICON icon name {val!r} is not attribute-safe"


def _node() -> str | None:
    return shutil.which("node")


def _run_js(snippet: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(snippet)
        path = f.name
    try:
        r = subprocess.run([_node(), path], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout
    finally:
        os.unlink(path)


def test_esc_behavior_blocks_attribute_breakout():
    """Behavioral: run the REAL esc() from each file and confirm a quoted
    payload can no longer close the attribute."""
    if not _node():
        print("== test_esc_behavior_blocks_attribute_breakout == (skipped: no node)")
        return
    payload = '做个网页" onmouseover="alert(1)" x="'
    for name in FRONTENDS:
        out = _run_js(
            _esc_decl(_src(name))
            + "\nconst payload=" + json.dumps(payload) + ";"
            + '\nconst html=' + "'<button data-copy=\"'+esc(payload)+'\">x</button>';"
            + "\nconsole.log(html);\n")
        html = out.strip()
        # exactly two raw quotes remain: the ones we wrote around the value
        assert html.count('"') == 2, f"{name}: attribute breakout still possible: {html}"
        assert "&quot;" in html, f"{name}: payload quotes were not entity-encoded: {html}"
        # and no event-handler attribute exists on the produced element.
        # NB: match whole name="value" pairs so the scanner consumes the
        # quoted value -- a naive \s(name)= regex would "find"
        # onmouseover= INSIDE the (now entity-encoded, inert) value and
        # report a breakout that isn't there.
        tag = re.match(r"<button[^>]*>", html).group(0)
        attrs = {a.lower() for a in re.findall(r'([A-Za-z-]+)\s*=\s*"[^"]*"', tag)}
        assert attrs == {"data-copy"}, f"{name}: unexpected attributes {attrs}: {html}"


def test_markdown_renderer_still_only_emits_safe_attributes():
    """esc() feeds mdToHtml's inline() pipeline, so the escaping change
    must not have loosened the deliverable-preview markdown renderer.
    Asserts the ONLY attributes it can ever emit are href/target/rel, and
    that non-http schemes are still neutralised to '#'."""
    if not _node():
        print("== test_markdown_renderer_still_only_emits_safe_attributes == (skipped: no node)")
        return
    src = _src("dashboard.html")
    md = re.search(r"function mdToHtml\(src\)\{.*?\n\}\nfunction pvNote", src, re.S)
    assert md, "could not find mdToHtml"
    md_src = md.group(0)[: -len("\nfunction pvNote")]
    cases = [
        '[x](https://e.com/")onmouseover="alert(1))',
        "<img src=x onerror=alert(1)>",
        '[a](x" onclick="alert(1))',
        "[a](javascript:alert(1))",
        "[a](data:text/html,<script>alert(1)</script>)",
        "[ok](https://example.com/a?b=1&c=2)",
    ]
    js = (_esc_decl(src) + "\n" + md_src + "\n"
          + "const cases=" + json.dumps(cases) + ";\n"
          + r"""
for(const src of cases){
  const out=mdToHtml(src);
  const attrs=new Set();
  for(const tag of out.match(/<[a-zA-Z][^>]*>/g)||[]){
    // whole name="value" pairs, so the quoted value is consumed and its
    // (inert, entity-encoded) contents can't be mistaken for attributes
    for(const m of tag.matchAll(/([a-zA-Z-]+)\s*=\s*"[^"]*"/g)) attrs.add(m[1].toLowerCase());
  }
  console.log(JSON.stringify({attrs:[...attrs],
    hrefs:(out.match(/href="([^"]*)"/g)||[])}));
}
""")
        # noqa: E501
    for line, case in zip(_run_js(js).strip().splitlines(), cases):
        got = json.loads(line)
        assert set(got["attrs"]) <= {"href", "target", "rel"}, (case, got)
        for h in got["hrefs"]:
            url = h[len('href="'):-1]
            assert url == "#" or re.match(r"^(https?:|/|#|\.)", url), (case, url)
    # the safe link survived unchanged (no over-escaping regression)
    last = json.loads(_run_js(js).strip().splitlines()[-1])
    assert any("example.com" in h for h in last["hrefs"]), last


if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll frontend-escaping tests passed.")
