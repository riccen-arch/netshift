#!/usr/bin/env python3
"""Übersetzungsdateien aktualisieren.

Sammelt alle _("…")-Texte aus den Programmdateien und ergänzt sie in po/<sprache>.po.
Vorhandene Übersetzungen bleiben erhalten, entfallene Texte werden als veraltet (#~) markiert.

    python3 tools/update-po.py            # alle vorhandenen po/*.po aktualisieren
    python3 tools/update-po.py fr         # neue Sprache anlegen: po/fr.po
    python3 tools/update-po.py --check    # nur prüfen: fehlende Übersetzungen auflisten
"""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SOURCES = ["netshift.py", "netshift-gtk.py", "netshift-gui.py"]


def collect():
    msgs = {}
    for f in SOURCES:
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                msgs.setdefault(node.args[0].value, []).append(f"{f}:{node.lineno}")
    return msgs


def po_quote(s):
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if "\n" in s:
        parts = s.split("\n")
        lines = [p + "\\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        return '""\n' + "\n".join(f'"{l}"' for l in lines)
    return f'"{s}"'


def load(path):
    sys.path.insert(0, ROOT)
    import importlib.util
    spec = importlib.util.spec_from_file_location("ns", os.path.join(ROOT, "netshift.py"))
    ns = importlib.util.module_from_spec(spec)
    os.environ["NETSHIFT_LANG"] = "de"
    spec.loader.exec_module(ns)
    return ns._load_po(path) if os.path.exists(path) else {}


def write(path, lang, msgs, old):
    out = [f'# netshift – Übersetzung: {lang}', '# Ausgangssprache ist Deutsch (msgid).',
           'msgid ""', 'msgstr ""', '"Content-Type: text/plain; charset=UTF-8\\n"',
           f'"Language: {lang}\\n"', '']
    for msgid, refs in msgs.items():
        out.append("#: " + " ".join(refs[:4]))
        if "{" in msgid:
            out.append("#, python-brace-format")
        out.append("msgid " + po_quote(msgid))
        out.append("msgstr " + po_quote(old.get(msgid, "")))
        out.append("")
    stale = [m for m in old if m not in msgs]
    for m in stale:
        out.append("#~ msgid " + po_quote(m).replace("\n", "\n#~ "))
        out.append("#~ msgstr " + po_quote(old[m]).replace("\n", "\n#~ "))
        out.append("")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    missing = [m for m in msgs if not old.get(m)]
    return missing, stale


def main():
    msgs = collect()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    os.makedirs(os.path.join(ROOT, "po"), exist_ok=True)
    langs = args or [os.path.basename(p)[:-3] for p in glob.glob(os.path.join(ROOT, "po", "*.po"))]
    for lang in langs:
        path = os.path.join(ROOT, "po", lang + ".po")
        old = load(path)
        if "--check" in sys.argv:
            missing = [m for m in msgs if not old.get(m)]
        else:
            missing, stale = write(path, lang, msgs, old)
            print(f"{path}: {len(msgs)} Texte, {len(stale)} veraltet")
        print(f"{lang}: {len(missing)} ohne Übersetzung")
        for m in missing[:20]:
            print("   ", m[:100].replace("\n", "⏎"))
    return 1 if "--check" in sys.argv and any(True for _x in []) else 0


if __name__ == "__main__":
    sys.exit(main())
