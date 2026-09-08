"""git_history.py under GTK4 — headless.

Guards the event-controller migration: button/motion/leave events no
longer exist on widgets, and cursors moved from GdkWindow to the widget.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0'); gi.require_version('GtkSource', '5')
from gi.repository import Gtk, GtkSource

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import git_history
from git_history import GitHistoryMixin, _parse_remote_url, _commit_url

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)

class Host(GitHistoryMixin):
    def __init__(self): self.status = []
    def _set_status(self, m): self.status.append(m)

h = Host()
buf = Gtk.TextBuffer()
view = Gtk.TextView.new_with_buffer(buf)

def kinds(w):
    return [type(c).__name__ for c in w.observe_controllers()]

before = kinds(view)
h._git_attach_click_handler(view)
added = [k for k in kinds(view) if k not in before or kinds(view).count(k) > before.count(k)]
check("GestureClick installed", "GestureClick" in kinds(view), kinds(view))
check("EventControllerMotion installed", "EventControllerMotion" in kinds(view), kinds(view))

# The click gesture must be restricted to the primary button, since the
# old handler's `event.button != 1` guard no longer exists.
g = [c for c in view.observe_controllers() if isinstance(c, Gtk.GestureClick)]
check("click gesture restricted to button 1", any(x.get_button() == 1 for x in g))

# Cursor now set on the widget; must not raise and must not need a display arg.
try:
    h._git_set_text_cursor(view, 'pointer')
    h._git_set_text_cursor(view, 'text')
    check("set_cursor_from_name() works on the widget", True)
except Exception as e:
    check("set_cursor_from_name() works on the widget", False, repr(e))

# --- hash parsing off a real buffer (unchanged logic, guards the port) ---
buf.set_text("a1b2c3d4  Fix the thing\nnot-a-hash line\n")
h._git_history_buffer = buf
check("commit hash found on line 0", h._git_line_hash(0) == "a1b2c3d4", h._git_line_hash(0))
check("non-hash line yields None", h._git_line_hash(1) is None)
check("negative line yields None", h._git_line_hash(-1) is None)

# --- URL helpers untouched by the port -----------------------------------
base, kind = _parse_remote_url("git@github.com:mbeulens/synpad.git")
check("ssh remote parsed", base and "mbeulens/synpad" in base, (base, kind))
check("commit URL built", "a1b2c3d4" in _commit_url(base, kind, "a1b2c3d4"))

# --- click on a hash line with no remote reports status, does not crash ---
h._git_history_state = {'remote_url': ''}
click = [c for c in view.observe_controllers() if isinstance(c, Gtk.GestureClick)][0]
try:
    h._git_on_history_click(click, 1, 5.0, 5.0)
    check("click handler runs with gesture signature", True)
except Exception as e:
    check("click handler runs with gesture signature", False, repr(e))

# --- motion/leave handlers accept controller signatures ------------------
motion = [c for c in view.observe_controllers() if isinstance(c, Gtk.EventControllerMotion)][0]
try:
    h._git_on_history_motion(motion, 5.0, 5.0)
    h._git_on_history_leave(motion)
    check("motion/leave handlers accept controller signature", True)
except Exception as e:
    check("motion/leave handlers accept controller signature", False, repr(e))

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
