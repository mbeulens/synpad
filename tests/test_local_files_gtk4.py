"""local_files.py under GTK4 — headless.

Guards the Gtk.Menu -> Gio.Menu/Gtk.PopoverMenu migration (every item and
label preserved), the button-press-event -> Gtk.GestureClick migration for
right-click, and the permissions dialog's .run() -> Gtk.Window + explicit
buttons restructuring.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

from local_files import LocalFilesMixin

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


class Host(LocalFilesMixin, Gtk.Window):
    """A real Gtk.Window subclass — the permissions dialog uses
    `transient_for=self`, so the host must be an actual window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.status = []
        self.errors = []
        self.calls = []
        self.config = {}
        self._local_path_entry = Gtk.Entry()
        self._local_path_entry.set_text('/tmp')
        self._local_store = Gtk.TreeStore(str, str, str, bool, bool)

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _icon_for_file(self, name): return 'text-x-generic'
    def _ask_name(self, *a, **kw): return None
    def _confirm_delete(self, what): return True
    def _git_show_history_local(self, target): self.calls.append(('git_history', target))
    def _on_local_new_file(self, parent_dir, parent_iter):
        self.calls.append(('new_file', parent_dir, parent_iter))
    def _on_local_new_dir(self, parent_dir, parent_iter):
        self.calls.append(('new_dir', parent_dir, parent_iter))
    def _on_local_rename(self, local_path, name, tree_iter):
        self.calls.append(('rename', local_path, name))
    def _on_local_permissions(self, local_path, name):
        self.calls.append(('permissions', local_path, name))
    def _on_local_delete(self, local_path, name, tree_iter, is_dir=False):
        self.calls.append(('delete', local_path, name, is_dir))


h = Host()
view = Gtk.TreeView(model=h._local_store)
# Popovers require a realized toplevel ancestor to popup() without
# crashing; host the tree view in a plain window like the other ported
# modules' tests do.
_host_win = Gtk.Window()
_host_win.set_child(view)
_host_win.present()

# --- controller attach: right-click arrives via GestureClick on button 3 --
def kinds(w):
    return [type(c).__name__ for c in w.observe_controllers()]

before = kinds(view)
h._local_attach_tree_controllers(view)
check("GestureClick installed", "GestureClick" in kinds(view), kinds(view))
gestures = [c for c in view.observe_controllers() if isinstance(c, Gtk.GestureClick)]
check("click gesture restricted to button 3 (was event.button != 3)",
      any(g.get_button() == 3 for g in gestures))


def menu_item_labels(menu_model):
    """Flatten a Gio.Menu (with sections) into an ordered list of labels."""
    labels = []
    for i in range(menu_model.get_n_items()):
        link = menu_model.get_item_link(i, Gio.MENU_LINK_SECTION)
        if link is not None:
            labels.extend(menu_item_labels(link))
        else:
            val = menu_model.get_item_attribute_value(i, 'label', None)
            labels.append(val.get_string() if val else None)
    return labels


def action_names(menu_model, prefix='localctx.'):
    names = []
    for i in range(menu_model.get_n_items()):
        link = menu_model.get_item_link(i, Gio.MENU_LINK_SECTION)
        if link is not None:
            names.extend(action_names(link, prefix))
            continue
        val = menu_model.get_item_attribute_value(i, 'action', None)
        s = val.get_string() if val else None
        if s and s.startswith(prefix):
            names.append(s[len(prefix):])
    return names


def activate(view, action_name):
    ok = view.activate_action(f'localctx.{action_name}', None)
    if not ok:
        raise RuntimeError(f"action localctx.{action_name} not found on {view}")


# --- empty space: no path_info -> "New File...", "New Directory..." ------
h._on_local_tree_right_click(gestures[0], 1, 9999.0, 9999.0)  # off any row
popover = h._local_ctx_popover
check("popover created", isinstance(popover, Gtk.PopoverMenu))
menu = popover.get_menu_model()
labels = menu_item_labels(menu)
check("empty-space menu: New File...", "New File..." in labels, labels)
check("empty-space menu: New Directory...", "New Directory..." in labels, labels)
check("empty-space menu has exactly 2 items", len(labels) == 2, labels)

activate(view, action_names(menu)[0])
check("empty-space New File... wired to _on_local_new_file",
      h.calls and h.calls[-1][0] == 'new_file', h.calls)

# --- popover reused (not recreated) and reparented cleanly ----------------
view2 = Gtk.TreeView(model=h._local_store)
_host_win2 = Gtk.Window()
_host_win2.set_child(view2)
h._local_ensure_ctx_popover(view2)
check("popover instance reused across views", h._local_ctx_popover is popover)
check("popover reparented to the new view", popover.get_parent() is view2)

# --- directory row (non-git) menu: New/Rename/Permissions/Delete ----------
h.calls.clear()
d_iter = h._local_store.append(None, ['subdir', 'folder', '/tmp/subdir', True, True])
tree_path = h._local_store.get_path(d_iter)

class FakeGesture:
    def __init__(self, widget): self._w = widget
    def get_widget(self): return self._w

view.expand_all()
# get_path_at_pos needs a realized widget; drive the handler directly using
# the tree_iter lookups it performs, by monkeypatching get_path_at_pos.
orig_get_path_at_pos = view.get_path_at_pos
view.get_path_at_pos = lambda x, y: (tree_path, None, 0, 0)
h._on_local_tree_right_click(FakeGesture(view), 1, 5.0, 5.0)
view.get_path_at_pos = orig_get_path_at_pos

menu = h._local_ctx_popover.get_menu_model()
labels = menu_item_labels(menu)
check("dir menu: New File...", "New File..." in labels, labels)
check("dir menu: New Directory...", "New Directory..." in labels, labels)
check("dir menu: Rename 'subdir'...", "Rename 'subdir'..." in labels, labels)
check("dir menu: Permissions 'subdir'...", "Permissions 'subdir'..." in labels, labels)
check("dir menu: Delete Directory 'subdir'", "Delete Directory 'subdir'" in labels, labels)
check("dir menu: no git-history item (no .git child)",
      "Show git history" not in labels, labels)

names = action_names(menu)
activate(view, [n for n in names if 'delete' in n][0])
check("Delete Directory wired to _on_local_delete(is_dir=True)",
      h.calls and h.calls[-1] == ('delete', '/tmp/subdir', 'subdir', True), h.calls)

# --- directory row that IS a repo root (.git child) -> git-history item ---
os.makedirs('/tmp/synpad_test_repo/.git', exist_ok=True)
h._local_store.clear()
d_iter = h._local_store.append(None, ['synpad_test_repo', 'folder',
                                       '/tmp/synpad_test_repo', True, True])
tree_path = h._local_store.get_path(d_iter)
view.get_path_at_pos = lambda x, y: (tree_path, None, 0, 0)
h._on_local_tree_right_click(FakeGesture(view), 1, 5.0, 5.0)
view.get_path_at_pos = orig_get_path_at_pos
menu = h._local_ctx_popover.get_menu_model()
labels = menu_item_labels(menu)
check("repo-root dir menu includes 'Show git history'",
      "Show git history" in labels, labels)
names = action_names(menu)
git_action = [n for n in names if 'git' in n]
check("git-history action present", bool(git_action), names)
if git_action:
    h.calls.clear()
    activate(view, git_action[0])
    check("git-history action targets the .git path",
          h.calls == [('git_history', '/tmp/synpad_test_repo/.git')], h.calls)

import shutil
shutil.rmtree('/tmp/synpad_test_repo', ignore_errors=True)

# --- file row menu: Rename/Permissions/Delete, no "Directory" wording ----
f_iter = h._local_store.append(None, ['file.txt', 'text-x-generic',
                                       '/tmp/file.txt', False, False])
tree_path = h._local_store.get_path(f_iter)
view.get_path_at_pos = lambda x, y: (tree_path, None, 0, 0)
h._on_local_tree_right_click(FakeGesture(view), 1, 5.0, 5.0)
view.get_path_at_pos = orig_get_path_at_pos
menu = h._local_ctx_popover.get_menu_model()
labels = menu_item_labels(menu)
check("file menu: Rename 'file.txt'...", "Rename 'file.txt'..." in labels, labels)
check("file menu: Permissions 'file.txt'...", "Permissions 'file.txt'..." in labels, labels)
check("file menu: Delete 'file.txt' (not 'Delete Directory')",
      "Delete 'file.txt'" in labels, labels)
check("file menu has exactly 3 items (no separators/new-file/git)",
      len(labels) == 3, labels)

# --- permissions dialog: checkbox state, octal round-trip, Apply/Cancel --
# Walk a widget's child tree collecting instances of `cls`, depth-first in
# attach/append order.
def find_all(widget, cls):
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            found.append(child)
        found.extend(find_all(child, cls))
        child = child.get_next_sibling()
    return found

# Capture the Gtk.Window the mixin builds (it doesn't stash a reference on
# self) by temporarily subclassing Gtk.Window at the shared gi.repository
# module level — local_files.py's `Gtk` name is that same module object.
captured = []
class _CapturingWindow(Gtk.Window):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        captured.append(self)

_RealWindow = Gtk.Window
Gtk.Window = _CapturingWindow
try:
    h._show_local_permissions_dialog('/tmp/file.txt', 'file.txt', 0o644)
finally:
    Gtk.Window = _RealWindow

check("permissions dialog window created", len(captured) == 1, captured)
win = captured[0]
check("dialog title includes file name", win.get_title() == "Permissions — file.txt",
      win.get_title())

checks = find_all(win, Gtk.CheckButton)
check("9 permission checkboxes (3 x rwx)", len(checks) == 9, len(checks))
# Order: Owner(r,w,x), Group(r,w,x), Others(r,w,x) — matches 0o644 = rw-r--r--
expected = [True, True, False, True, False, False, True, False, False]
actual = [c.get_active() for c in checks]
check("checkboxes reflect 0o644 (rw-r--r--)", actual == expected, actual)

entries = find_all(win, Gtk.Entry)
octal_entry = entries[0]
check("octal entry initialized to 644", octal_entry.get_text() == "644",
      octal_entry.get_text())

buttons = find_all(win, Gtk.Button)
by_label = {b.get_label(): b for b in buttons if b.get_label()}
check("Cancel and Apply buttons present",
      set(by_label) == {"Cancel", "Apply"}, list(by_label))

# Toggling a checkbox updates the octal entry (checkbox -> octal sync).
checks[1].set_active(False)  # clear Owner-write -> 0o644 becomes 0o444
check("checkbox toggle updates octal entry", octal_entry.get_text() == "444",
      octal_entry.get_text())
checks[1].set_active(True)  # restore

# Editing the octal entry updates the checkboxes (octal -> checkbox sync).
octal_entry.set_text("755")
check("octal entry edit updates checkboxes",
      [c.get_active() for c in checks] ==
      [True, True, True, True, False, True, True, False, True],
      [c.get_active() for c in checks])
octal_entry.set_text("644")

# Apply calls os.chmod with the entered value and reports status; Cancel
# does neither. This is the .run()-return-value logic restructured into
# button-click callbacks per the plan's Adw.AlertDialog/.choose() gotcha
# (same restructuring rationale applies to a plain Gtk.Window here).
chmod_calls = []
_orig_chmod = os.chmod
os.chmod = lambda path, mode: chmod_calls.append((path, mode))
try:
    by_label["Cancel"].emit('clicked')
    check("Cancel does not call chmod", chmod_calls == [], chmod_calls)

    # Re-open for the Apply case (Cancel already closed the first window).
    captured.clear()
    Gtk.Window = _CapturingWindow
    try:
        h._show_local_permissions_dialog('/tmp/file.txt', 'file.txt', 0o644)
    finally:
        Gtk.Window = _RealWindow
    win2 = captured[0]
    buttons2 = find_all(win2, Gtk.Button)
    by_label2 = {b.get_label(): b for b in buttons2}
    by_label2["Apply"].emit('clicked')
    check("Apply calls os.chmod with the octal value",
          chmod_calls == [('/tmp/file.txt', 0o644)], chmod_calls)
    check("Apply reports status", h.status and 'Permissions set' in h.status[-1],
          h.status)
finally:
    os.chmod = _orig_chmod

# Invalid octal reports an error, not a crash.
h.errors.clear()
captured.clear()
Gtk.Window = _CapturingWindow
try:
    h._show_local_permissions_dialog('/tmp/file.txt', 'file.txt', 0o644)
finally:
    Gtk.Window = _RealWindow
win3 = captured[0]
entries3 = find_all(win3, Gtk.Entry)
buttons3 = find_all(win3, Gtk.Button)
by_label3 = {b.get_label(): b for b in buttons3}
entries3[0].set_text("not-octal")
try:
    by_label3["Apply"].emit('clicked')
    check("invalid octal reports error without raising",
          h.errors and h.errors[-1][0] == "Invalid Permissions", h.errors)
except Exception as e:
    check("invalid octal reports error without raising", False, repr(e))

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
