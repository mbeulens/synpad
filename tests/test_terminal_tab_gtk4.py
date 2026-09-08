"""terminal_tab.py under GTK4 — headless.

Guards the Vte 2.91 -> 3.91 bump (real spawn_async call), the
Gtk.EventBox removal (label rename uses a plain Box slot + GestureClick),
the button/key/focus event-controller migrations, and the
Gtk.MessageDialog.run() -> Adw.AlertDialog async .choose() migration for
the busy-terminal close confirmation.
"""
import os, sys, signal, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, GLib, Adw

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import terminal_tab
from terminal_tab import TerminalMixin, HAS_VTE

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)

check("Vte 3.91 available in this environment", HAS_VTE)
if not HAS_VTE:
    print("SKIP: Vte not available, cannot exercise spawn_async"); sys.exit(0)


class Host(TerminalMixin):
    def __init__(self):
        self._terminal_init()
        self._console_notebook = Gtk.Notebook()
        self._console_visible = True
        self.errors = []
        self.toggles = 0
        self.tabs = {}
        self.notebook = Gtk.Notebook()

    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _on_toggle_console(self):
        self.toggles += 1
        self._console_visible = not self._console_visible


h = Host()
_host_win = Gtk.Window()
_host_win.set_child(h._console_notebook)
_host_win.present()

# --- default cwd: falls back to $HOME with no tab/local-path context -----
check("_terminal_default_cwd falls back to $HOME",
      h._terminal_default_cwd() == os.path.expanduser('~'))

# --- add-terminal button: icon, tooltip, no Gtk.IconSize needed -----------
btn = h._terminal_make_add_button()
check("add button uses list-add-symbolic icon",
      btn.get_icon_name() == 'list-add-symbolic', btn.get_icon_name())
check("add button is flat (was set_relief(NONE))",
      btn.has_css_class('flat'))
check("add button tooltip preserved", btn.get_tooltip_text() == "New terminal")

# --- real spawn_async under Vte 3.91 --------------------------------------
os.environ['SHELL'] = '/bin/bash'
n_pages_before = h._console_notebook.get_n_pages()
h._terminal_add_new()
check("a terminal was registered", len(h._terminals) == 1, h._terminals)
scroll = next(iter(h._terminals))
check("notebook page added", h._console_notebook.get_n_pages() == n_pages_before + 1)

ctx = GLib.MainContext.default()
t0 = time.time()
while time.time() - t0 < 5 and h._terminals[scroll]['pid'] is None:
    ctx.iteration(True)
check("spawn_async fired the callback with a real pid",
      isinstance(h._terminals[scroll]['pid'], int) and h._terminals[scroll]['pid'] > 0,
      h._terminals[scroll])
check("no spawn error reported", h.errors == [], h.errors)

spawned_pid = h._terminals[scroll]['pid']

# --- tab label structure: icon + label-slot + close button, no EventBox --
label_box = h._console_notebook.get_tab_label(scroll)
def children(w):
    out = []
    c = w.get_first_child()
    while c is not None:
        out.append(c)
        c = c.get_next_sibling()
    return out

kids = children(label_box)
check("tab label has 3 children (icon, label-slot, close button)",
      len(kids) == 3, [type(k).__name__ for k in kids])
check("first child is an image (terminal icon)", isinstance(kids[0], Gtk.Image))
# Gtk.EventBox doesn't exist under GTK4 at all (confirmed: not
# hasattr(Gtk, 'EventBox')), so there is no class left to instance-check
# against — the real, falsifiable guarantee is that the label lives in a
# plain Gtk.Box slot with a GestureClick attached directly, asserted below.
check("Gtk.EventBox class does not exist under GTK4 (removed, not just unused)",
      not hasattr(Gtk, 'EventBox'))

info = h._terminals[scroll]
label_slot = info['label_slot']
label = info['label']
check("label slot's only child is the label", label_slot.get_first_child() is label)
slot_controllers = [type(c).__name__ for c in label_slot.observe_controllers()]
check("label slot has a GestureClick (double-click-to-rename)",
      "GestureClick" in slot_controllers, slot_controllers)

# --- double-click begins rename; single click does not -------------------
h._terminal_on_label_press(None, 1, 0.0, 0.0, scroll)
check("single click does not start rename", info['renaming'] is False)

h._terminal_on_label_press(None, 2, 0.0, 0.0, scroll)
check("double click starts rename", info['renaming'] is True)
entry = label_slot.get_first_child()
check("label slot now holds an entry", isinstance(entry, Gtk.Entry))
check("entry pre-filled with current name", entry.get_text() == f"Terminal 1", entry.get_text())

entry_controllers = [type(c).__name__ for c in entry.observe_controllers()]
check("rename entry has EventControllerFocus (was focus-out-event)",
      "EventControllerFocus" in entry_controllers, entry_controllers)
check("rename entry has EventControllerKey (was key-press-event)",
      "EventControllerKey" in entry_controllers, entry_controllers)

# Escape cancels the rename without committing.
h._terminal_rename_keypress(None, Gdk.KEY_Escape, 0, 0, scroll, entry)
check("Escape cancels rename", info['renaming'] is False)
check("label restored after cancel", label_slot.get_first_child() is label)
check("label text unchanged after cancelled rename", label.get_text() == "Terminal 1")

# Committing a rename (Enter / activate) updates the label text.
h._terminal_on_label_press(None, 2, 0.0, 0.0, scroll)
entry = label_slot.get_first_child()
entry.set_text("my-shell")
h._terminal_commit_rename(scroll, entry)
check("rename commits new label text", label.get_text() == "my-shell", label.get_text())
check("slot holds the label again after commit", label_slot.get_first_child() is label)
check("renaming flag cleared after commit", info['renaming'] is False)

# --- close a non-busy terminal: no confirmation dialog --------------------
h._terminal_close(scroll)
check("non-busy close removes the terminal immediately",
      scroll not in h._terminals, h._terminals)
check("non-busy close removes the notebook page",
      h._console_notebook.get_n_pages() == n_pages_before)

try:
    os.kill(spawned_pid, 0)
    os.kill(spawned_pid, signal.SIGKILL)
except ProcessLookupError:
    pass

# --- busy terminal: Adw.AlertDialog.choose() replaces MessageDialog.run --
class FakePty:
    def get_fd(self): return 99

class FakeTerm:
    def get_pty(self): return FakePty()

scroll2 = Gtk.ScrolledWindow()
h._console_notebook.append_page(scroll2, Gtk.Label(label="busy"))
h._terminals[scroll2] = {
    'term': FakeTerm(), 'pid': 4242,
    'label_slot': None, 'label': None, 'renaming': False,
}

captured = {}
def fake_choose(self, parent, cancellable, callback, user_data):
    captured['dlg'] = self
    captured['callback'] = callback
    captured['user_data'] = user_data

_orig_choose = Adw.AlertDialog.choose
Adw.AlertDialog.choose = fake_choose

_orig_tcgetpgrp = os.tcgetpgrp
os.tcgetpgrp = lambda fd: 9999  # != pid -> busy

try:
    h._terminal_close(scroll2)
finally:
    os.tcgetpgrp = _orig_tcgetpgrp

check("busy terminal opens a confirmation dialog", 'dlg' in captured, captured)
dlg = captured.get('dlg')
if dlg is not None:
    check("dialog heading preserved", dlg.get_heading() == "Close terminal?",
          dlg.get_heading())
    check("dialog body preserved",
          dlg.get_body() ==
          "A process is still running in this terminal. Close anyway?",
          dlg.get_body())
check("terminal not removed while dialog is pending",
      scroll2 in h._terminals)

# 'No' leaves the terminal open.
class _Result:
    pass

dlg.choose_finish = lambda result: 'no'
h._terminal_close_response(dlg, _Result(), scroll2)
check("'No' response leaves the terminal open", scroll2 in h._terminals)

# 'Yes' sends SIGTERM and removes it — same decision logic as the old
# `resp == Gtk.ResponseType.YES` check, now reached via the async callback.
kill_calls = []
_orig_kill = os.kill
os.kill = lambda pid, sig: kill_calls.append((pid, sig))
dlg.choose_finish = lambda result: 'yes'
try:
    h._terminal_close_response(dlg, _Result(), scroll2)
finally:
    os.kill = _orig_kill
Adw.AlertDialog.choose = _orig_choose

check("'Yes' response sends SIGTERM to the pid",
      kill_calls == [(4242, signal.SIGTERM)], kill_calls)
check("'Yes' response removes the terminal", scroll2 not in h._terminals)

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
