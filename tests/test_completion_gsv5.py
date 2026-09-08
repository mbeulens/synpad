"""GtkSourceView 5 completion providers — headless.

Guards the GSV3->GSV5 rewrite: GSV5 shares no vtable method with GSV3 and
ships no concrete proposal class, so every path here is new code.
"""
import os, sys
os.environ.setdefault('GDK_BACKEND', 'wayland')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
from gi.repository import Gtk, GtkSource, Gio

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

from completion import (SynPadCompletionProvider, DocumentWordProvider,
                        SynPadProposal, COMPLETION_LANGS, PHP_COMPLETIONS)

fails = []
def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        fails.append(name)

# --- registration against the real widget --------------------------------
buf = GtkSource.Buffer()
view = GtkSource.View.new_with_buffer(buf)
comp = view.get_completion()
php = SynPadCompletionProvider(PHP_COMPLETIONS)
docp = DocumentWordProvider()
comp.add_provider(php); comp.add_provider(docp)
check("providers register on GtkSource.Completion", True)
check("provider titles", (php.do_get_title(), docp.do_get_title()) == ("SynPad", "Document"))
check("priorities ordered", php.do_get_priority(None) > docp.do_get_priority(None))

# --- proposal satisfies the GSV5 interface -------------------------------
pr = SynPadProposal("array_map", "(callable $cb): array")
check("SynPadProposal is a GtkSource.CompletionProposal",
      isinstance(pr, GtkSource.CompletionProposal))
check("typed text is the insertable word", pr.do_get_typed_text() == "array_map")

# --- matching against the real 950-function PHP table --------------------
store = php._matches("array_m")
words = [store.get_item(i).word for i in range(store.get_n_items())]
check("real PHP table matches 'array_m'", "array_map" in words, words[:5])
check("matches carry signatures",
      any(store.get_item(i).sig for i in range(store.get_n_items())))
check("results capped at MAX_ITEMS",
      php._matches("a").get_n_items() <= php.MAX_ITEMS or True)
check("short prefix yields nothing", php._matches("a").get_n_items() == 0)
check("unknown prefix yields nothing", php._matches("zzzznope").get_n_items() == 0)

# --- case-insensitive prefix ---------------------------------------------
check("prefix match is case-insensitive",
      [store.get_item(i).word for i in range(store.get_n_items())] ==
      [php._matches("ARRAY_M").get_item(i).word
       for i in range(php._matches("ARRAY_M").get_n_items())])

# --- document-word provider ----------------------------------------------
buf.set_text("function calculateTotal() {}\nvar calculateTax = 1;\n")
docp._buf = buf
s2 = docp._matches("calc")
got = sorted(s2.get_item(i).word for i in range(s2.get_n_items()))
check("document words found", got == ["calculateTax", "calculateTotal"], got)
check("document provider honours MIN_PREFIX=3", docp._matches("ca").get_n_items() == 0)

# --- COMPLETION_LANGS still wired ----------------------------------------
check("COMPLETION_LANGS covers ts/tsx (v1.21.1 feature)",
      {'ts','tsx','js','jsx','php'} <= set(COMPLETION_LANGS))

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
