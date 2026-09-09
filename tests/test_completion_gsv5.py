"""Completion under GtkSourceView 5.

GSV5's CompletionProvider cannot be implemented from Python here: its only
population entry point is the async populate_async/populate_finish vfunc
pair, and a provider implementing them segfaults GtkSourceView on the first
keystroke -- reproduced with a minimal do-nothing provider. Completion
therefore uses the built-in GtkSource.CompletionWords, seeded with the
language word list via register().
"""
import os, sys, subprocess, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FAKE = tempfile.mkdtemp(); os.environ['HOME'] = _FAKE
os.environ['XDG_CONFIG_HOME'] = os.path.join(_FAKE, '.config')

import gi
gi.require_version('Gtk', '4.0'); gi.require_version('GtkSource', '5')
from gi.repository import Gtk, GtkSource

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import config
assert '/home/beuner/.config' not in config.CONFIG_DIR, "NOT ISOLATED"

from completion import (make_completion_providers, COMPLETION_LANGS,
                        PHP_COMPLETIONS, JS_COMPLETIONS)

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)

# --- providers are the built-in C ones, never a Python subclass ------------
buf = GtkSource.Buffer()
provs, keep = make_completion_providers(PHP_COMPLETIONS, buf)
check("one CompletionWords provider, both buffers registered",
      len(provs) == 1, len(provs))
check("both are built-in CompletionWords",
      all(isinstance(p, GtkSource.CompletionWords) for p in provs),
      [type(p).__name__ for p in provs])
check("no Python-implemented provider is used",
      all(type(p).__module__.startswith('gi.') for p in provs),
      [type(p).__module__ for p in provs])
check("seeded language buffer is kept alive", len(keep) == 1 and isinstance(keep[0], GtkSource.Buffer))
check("provider has a sane minimum word size",
      provs[0].get_property('minimum-word-size') == 2)

# --- unknown language still gets document completion -----------------------
p2, k2 = make_completion_providers(None, GtkSource.Buffer())
check("unknown language: provider present, no seed buffer",
      len(p2) == 1 and k2 == [])

# --- the seed buffer really carries the language's words -------------------
seed_text = keep[0].get_text(keep[0].get_start_iter(), keep[0].get_end_iter(), False)
for word in ('array_map', 'str_replace', 'foreach', 'function'):
    check(f"seed contains {word!r}", word in seed_text.split())
check("seed covers the whole table", len(seed_text.split()) == len(PHP_COMPLETIONS),
      (len(seed_text.split()), len(PHP_COMPLETIONS)))

# --- language tables intact (signature_help.py reads these) ----------------
check("COMPLETION_LANGS covers ts/tsx (v1.21.1)",
      {'php','js','jsx','ts','tsx'} <= set(COMPLETION_LANGS))
check("PHP signatures still present (used by signature help)",
      PHP_COMPLETIONS.get('array_map') and '(' in PHP_COMPLETIONS['array_map'])
check("JS table non-empty", len(JS_COMPLETIONS) > 0)

# --- regression: typing must not segfault ----------------------------------
# This is the actual bug. Run it out-of-process so a crash is observable
# rather than taking this suite down with it.
prog = '''
import sys
sys.path.insert(0, %r)
import gi
gi.require_version('Gtk','4.0'); gi.require_version('GtkSource','5'); gi.require_version('Adw','1')
from gi.repository import Gtk, GtkSource, Adw, GLib
Adw.init(); Gtk.init_check()
from completion import make_completion_providers, PHP_COMPLETIONS
app = Adw.Application(application_id='synpad.test.typing')
def act(a):
    win = Gtk.Window(application=a); win.set_default_size(500,300)
    b = GtkSource.Buffer(); v = GtkSource.View.new_with_buffer(b)
    win.set_child(v); win.present()
    provs, keep = make_completion_providers(PHP_COMPLETIONS, b)
    v._keep = keep
    for p in provs: v.get_completion().add_provider(p)
    def go():
        b.set_text("hello helper\\n"); b.place_cursor(b.get_end_iter())
        for ch in "arr":
            b.insert_at_cursor(ch)
            for _ in range(120):
                if not GLib.MainContext.default().pending(): break
                GLib.MainContext.default().iteration(False)
        print("SURVIVED"); a.quit(); return False
    GLib.timeout_add(400, go)
app.connect('activate', act); app.run([])
''' % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
f = os.path.join(_FAKE, 'typing_probe.py'); open(f,'w').write(prog)
env = dict(os.environ)
r = subprocess.run([sys.executable, f], capture_output=True, text=True, timeout=90, env=env)
check("typing with completion active does not crash (rc 0, was SIGSEGV)",
      r.returncode == 0 and 'SURVIVED' in r.stdout,
      f"rc={r.returncode} {'SIGSEGV' if r.returncode==-11 or r.returncode==139 else ''}")

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
