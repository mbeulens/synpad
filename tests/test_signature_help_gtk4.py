"""signature_help.py under GTK4 — headless.

Covers the three GTK4 migrations: popover set_child/set_parent, autohide
instead of modal, and focus-out via Gtk.EventControllerFocus.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
from gi.repository import Gtk, GtkSource

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

from signature_help import SignatureHelpMixin, CONTROL_KEYWORDS

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)

class Host(SignatureHelpMixin):
    pass

h = Host()
buf = GtkSource.Buffer()
view = GtkSource.View.new_with_buffer(buf)
win = Gtk.Window(); win.set_child(view)

# --- attach wires controllers without touching removed GTK3 signals ------
def focus_ctrls(w):
    return sum(1 for c in w.observe_controllers()
               if type(c).__name__ == "EventControllerFocus")

before = focus_ctrls(view)          # GtkSourceView installs its own
h._sighelp_attach(view, buf)
after = focus_ctrls(view)
check("attach() adds exactly one EventControllerFocus",
      after == before + 1, f"{before} -> {after}")

# --- popover built with the GTK4 API -------------------------------------
h._sighelp_ensure_popover(view)
pop = h._sighelp_popover
check("popover created", isinstance(pop, Gtk.Popover))
check("label set via set_child()", isinstance(pop.get_child(), Gtk.Label))
check("autohide off (was set_modal(False))", pop.get_autohide() is False)
check("parented to the view via set_parent()", pop.get_parent() is view)

# --- re-anchoring to a second view must unparent first -------------------
buf2 = GtkSource.Buffer()
view2 = GtkSource.View.new_with_buffer(buf2)
win2 = Gtk.Window(); win2.set_child(view2)
h._sighelp_ensure_popover(view2)
check("re-anchor moves parent cleanly", h._sighelp_popover.get_parent() is view2)
check("popover instance is reused, not recreated", h._sighelp_popover is pop)

# --- analysis logic still works (pure, but guards the port) --------------
buf.set_text("<?php array_map(")
res = h._sighelp_analyze(buf)
check("analyze() finds the open call", res is not None and res[0] == "array_map", res)
buf.set_text("<?php if (")
check("control keywords rejected", h._sighelp_analyze(buf) is None)
check("CONTROL_KEYWORDS intact", "foreach" in CONTROL_KEYWORDS)

# --- positioning uses GTK4-valid coordinate API --------------------------
buf.set_text("<?php array_map(")
h._sighelp_ensure_popover(view)
h._sighelp_position(view)
check("set_pointing_to() accepted", h._sighelp_popover.get_pointing_to()[0] is not False)

# --- hide is safe before any popup ---------------------------------------
h._sighelp_hide()
check("hide() safe when not visible", True)

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
