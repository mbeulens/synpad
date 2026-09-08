"""dialogs.py under GTK4 — headless.

Guards the four Gtk.Dialog -> Gtk.Window + explicit-buttons migrations in
this module (File Types, Color Scheme, Custom Colors, and the
ConnectDialog-driven settings dialog), each of them a "custom content"
dialog per the migration plan's gotcha (real body widgets, not a plain
heading/body/response confirm, so no Adw.AlertDialog here). Every one of
`.run()`'s blocking-return-value branches becomes a button-click callback;
this file checks the decision logic survived unchanged and that Escape is
explicitly wired to the same cancel path as the Cancel button (a bare
Gtk.Window has no built-in Escape-closes behavior, unlike Gtk.Dialog).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GtkSource, Gdk, Adw

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)
Adw.init()

import dialogs
from dialogs import DialogsMixin

# Never touch the real ~/.config/synpad/config.json from a headless test —
# dialogs.py calls the module-level `save_config` name it imported, so patch
# that name in dialogs' own namespace (patching config.save_config would not
# affect the reference dialogs.py already bound at import time).
save_config_calls = []
dialogs.save_config = lambda cfg: save_config_calls.append(dict(cfg))

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


class Host(DialogsMixin, Gtk.Window):
    """A real Gtk.Window subclass — every dialog here uses
    `transient_for=self`, so the host must be an actual window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.config = {
            'editor_extensions': ['py', 'js'],
            'color_scheme': 'oblivion',
            'dark_theme': True,
        }
        self.tabs = {}
        self.status = []
        self.errors = []
        self.rebuild_calls = 0
        self.apply_calls = 0

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _rebuild_quick_menu(self): self.rebuild_calls += 1
    def _apply_scheme_to_all(self): self.apply_calls += 1


def find_all(widget, cls):
    """Walk a widget's child tree collecting instances of `cls`, depth-first
    in append order."""
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            found.append(child)
        found.extend(find_all(child, cls))
        child = child.get_next_sibling()
    return found


def labeled_buttons(win):
    """A bare Gtk.Window's child tree contains an internal CSD close
    button with no label — filter those out."""
    return [b for b in find_all(win, Gtk.Button) if b.get_label()]


def capture_window(build_fn):
    """Capture the Gtk.Window a mixin method builds, by temporarily
    subclassing Gtk.Window at the shared gi.repository module level —
    dialogs.py's `Gtk` name is that same module object."""
    captured = []
    class _CapturingWindow(Gtk.Window):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
    _Real = Gtk.Window
    Gtk.Window = _CapturingWindow
    try:
        build_fn()
    finally:
        Gtk.Window = _Real
    return captured[0] if captured else None


def escape_key_controllers(win):
    return [c for c in win.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)]


def press_escape(win):
    for kc in escape_key_controllers(win):
        kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)


# =====================================================================
# _on_open_settings / ConnectDialog.choose() async continuation
# =====================================================================

class FakeConnectDialog:
    """Stands in for connection.ConnectDialog so this file tests only
    dialogs.py's own decision logic (what happens with the values on
    'ok' vs 'cancel'), not ConnectDialog's construction — that belongs to
    test_connection_gtk4.py."""
    last = None

    def __init__(self, parent, config, start_new=False):
        self.parent = parent
        self.config = config
        self.start_new = start_new
        self._values = {}
        self._callback = None
        FakeConnectDialog.last = self

    def choose(self, callback, *user_data):
        self._callback = callback
        self._user_data = user_data

    def get_values(self):
        return self._values


h = Host()
_orig_ConnectDialog = dialogs.ConnectDialog
dialogs.ConnectDialog = FakeConnectDialog
try:
    h._on_open_settings(None)
    dlg = FakeConnectDialog.last
    check("ConnectDialog constructed with start_new=True", dlg.start_new is True)
    check("_on_open_settings calls choose(), not run()", dlg._callback is not None)

    # 'cancel' response: nothing saved, but quick-connect always rebuilt.
    dlg._values = {
        'remember': True, 'host': 'h1', 'port': 22, 'username': 'u',
        'password': 'p', 'max_upload_size_mb': 5, 'protocol': 'sftp',
        'ssh_key_path': '', 'home_directory': '', 'server_guid': 'g1',
    }
    h.rebuild_calls = 0
    dlg._callback(dlg, 'cancel')
    check("cancel does not save config", 'host' not in h.config, h.config)
    check("cancel still rebuilds quick-connect menu (unconditional)",
          h.rebuild_calls == 1, h.rebuild_calls)

    # 'ok' + remember=True: fields copied into config exactly as before.
    h.rebuild_calls = 0
    dlg._callback(dlg, 'ok')
    check("ok+remember saves host", h.config.get('host') == 'h1', h.config)
    check("ok+remember saves last_server from server_guid",
          h.config.get('last_server') == 'g1', h.config)
    check("ok also rebuilds quick-connect menu", h.rebuild_calls == 1)

    # 'ok' + remember=False: values read but not written to config.
    h.config.pop('host', None)
    dlg._values = dict(dlg._values, remember=False, host='h2')
    dlg._callback(dlg, 'ok')
    check("ok without remember does not save", 'host' not in h.config, h.config)
finally:
    dialogs.ConnectDialog = _orig_ConnectDialog


# =====================================================================
# _on_edit_file_types
# =====================================================================

h = Host()
win = capture_window(lambda: h._on_edit_file_types(None))
check("file-types window created", win is not None)
check("file-types window title", win.get_title() == "File Types — Editor Extensions",
      win.get_title())

btns = labeled_buttons(win)
labels = [b.get_label() for b in btns]
check("Add/Remove/Apply/Cancel present", set(labels) == {"Add", "Remove", "Apply", "Cancel"},
      labels)
by_label = {b.get_label(): b for b in btns}
apply_i, cancel_i = labels.index("Apply"), labels.index("Cancel")
check("button visual order has Apply before Cancel (matches old pack_end reversal)",
      apply_i < cancel_i, labels)

entries = find_all(win, Gtk.Entry)
check("extension entry present", len(entries) == 1, entries)
ext_entry = entries[0]

trees = find_all(win, Gtk.TreeView)
ext_view = trees[0]

# Add a new extension via the entry + Add button.
ext_entry.set_text('tsx')
by_label["Add"].emit('clicked')
model = ext_view.get_model()
values = [row[0] for row in model]
check("Add inserts the new extension", 'tsx' in values, values)
check("Add clears the entry", ext_entry.get_text() == '', ext_entry.get_text())

# Adding a duplicate is a no-op.
before = [row[0] for row in model]
ext_entry.set_text('tsx')
by_label["Add"].emit('clicked')
check("Add ignores duplicates", [row[0] for row in model] == before)

# Cancel makes no config change.
h.config['editor_extensions'] = ['py', 'js']
by_label["Cancel"].emit('clicked')
check("Cancel does not touch config", h.config['editor_extensions'] == ['py', 'js'])
check("Cancel closes the window", win.get_visible() is False)

# Re-open, Apply saves the sorted extension list.
win2 = capture_window(lambda: h._on_edit_file_types(None))
btns2 = {b.get_label(): b for b in labeled_buttons(win2)}
tv2 = find_all(win2, Gtk.TreeView)[0]
entries2 = find_all(win2, Gtk.Entry)[0]
entries2.set_text('rb')
btns2["Add"].emit('clicked')
btns2["Apply"].emit('clicked')
check("Apply saves the (sorted) extension list",
      h.config['editor_extensions'] == sorted(['py', 'js', 'rb']),
      h.config['editor_extensions'])
check("Apply closes the window", win2.get_visible() is False)

# Escape cancels without saving (was Gtk.Dialog's built-in RESPONSE_DELETE_EVENT).
win3 = capture_window(lambda: h._on_edit_file_types(None))
key_ctrls3 = escape_key_controllers(win3)
check("file-types window has at least one key controller (ours + GTK's own)",
      len(key_ctrls3) >= 1, key_ctrls3)
h.config['editor_extensions'] = ['keep']
press_escape(win3)
check("Escape does not save config", h.config['editor_extensions'] == ['keep'])
check("Escape closes the window", win3.get_visible() is False)


# =====================================================================
# _on_pick_scheme
# =====================================================================

h = Host()
h.config['color_scheme'] = 'oblivion'
win = capture_window(lambda: h._on_pick_scheme(None))
check("color-scheme window created", win is not None)
check("color-scheme window title", win.get_title() == "Color Scheme", win.get_title())

btns = labeled_buttons(win)
labels = [b.get_label() for b in btns]
check("Cancel and OK present", set(labels) == {"Cancel", "OK"}, labels)
check("button visual order is [Cancel, OK] (matches add_buttons call order)",
      labels == ["Cancel", "OK"], labels)
by_label = {b.get_label(): b for b in btns}

trees = find_all(win, Gtk.TreeView)
tv = trees[0]
model = tv.get_model()
scheme_ids = [row[0] for row in model]
check("scheme list is populated", len(scheme_ids) > 0, scheme_ids)
check("current scheme pre-selected",
      model[tv.get_selection().get_selected()[1]][0] == 'oblivion')

# Pick a different scheme, then Cancel: config unchanged, but the live
# preview is reverted (old code's `else` branch).
other = next(s for s in scheme_ids if s != 'oblivion')
for row in model:
    if row[0] == other:
        tv.get_selection().select_iter(row.iter)
        break
h.apply_calls = 0
by_label["Cancel"].emit('clicked')
check("Cancel does not change config['color_scheme']",
      h.config['color_scheme'] == 'oblivion', h.config['color_scheme'])
check("Cancel still reverts the live preview", h.apply_calls == 1, h.apply_calls)
check("Cancel closes the window", win.get_visible() is False)

# Re-open, select the other scheme, OK commits it.
win2 = capture_window(lambda: h._on_pick_scheme(None))
btns2 = {b.get_label(): b for b in labeled_buttons(win2)}
tv2 = find_all(win2, Gtk.TreeView)[0]
model2 = tv2.get_model()
for row in model2:
    if row[0] == other:
        tv2.get_selection().select_iter(row.iter)
        break
h.apply_calls = 0
btns2["OK"].emit('clicked')
check("OK commits the selected scheme", h.config['color_scheme'] == other, h.config)
check("OK resets custom_colors", h.config.get('custom_colors') == {})
check("OK applies the scheme", h.apply_calls == 1)
check("OK closes the window", win2.get_visible() is False)

# Escape behaves like Cancel: revert preview, no config change.
win3 = capture_window(lambda: h._on_pick_scheme(None))
h.config['color_scheme'] = 'oblivion'
h.apply_calls = 0
press_escape(win3)
check("Escape does not change config['color_scheme']",
      h.config['color_scheme'] == 'oblivion')
check("Escape still reverts the live preview", h.apply_calls == 1, h.apply_calls)
check("Escape closes the window", win3.get_visible() is False)


# =====================================================================
# _on_custom_colors
# =====================================================================

h = Host()
win = capture_window(lambda: h._on_custom_colors(None))
check("custom-colors window created", win is not None)
check("custom-colors window title", win.get_title() == "Custom Colors", win.get_title())

notebooks = find_all(win, Gtk.Notebook)
check("Dark/Light notebook present", len(notebooks) == 1, notebooks)
nb = notebooks[0]
check("notebook has exactly 2 pages", nb.get_n_pages() == 2, nb.get_n_pages())
page_labels = [nb.get_tab_label(nb.get_nth_page(i)).get_label() for i in range(nb.get_n_pages())]
check("tabs are Dark Mode / Light Mode", page_labels == ["Dark Mode", "Light Mode"], page_labels)
check("starts on Dark Mode tab (dark_theme=True)", nb.get_current_page() == 0)

btns = labeled_buttons(win)
labels = [b.get_label() for b in btns]
check("Delete/Save/Reset All/Apply/Cancel all present",
      set(labels) >= {"Delete", "Save", "Reset All", "Apply", "Cancel"}, labels)
reset_i = labels.index("Reset All")
apply_i = labels.index("Apply")
cancel_i = labels.index("Cancel")
check("visual order is Reset All, then Apply, then Cancel "
      "(matches old pack_start(reset)+pack_end(cancel)+pack_end(apply))",
      reset_i < apply_i < cancel_i, labels)
by_label = {b.get_label(): b for b in btns}

# Cancel: no config change.
h.config.pop('custom_colors_dark', None)
by_label["Cancel"].emit('clicked')
check("Cancel does not write custom_colors_dark", 'custom_colors_dark' not in h.config)
check("Cancel closes the window", win.get_visible() is False)

# Apply: writes both color dicts (empty, since nothing was checked) and
# applies the scheme.
win2 = capture_window(lambda: h._on_custom_colors(None))
btns2 = {b.get_label(): b for b in labeled_buttons(win2)}
h.apply_calls = 0
btns2["Apply"].emit('clicked')
check("Apply writes custom_colors_dark", h.config.get('custom_colors_dark') == {})
check("Apply writes custom_colors_light", h.config.get('custom_colors_light') == {})
check("Apply applies the scheme", h.apply_calls == 1)
check("Apply closes the window", win2.get_visible() is False)

# Reset All: clears both dicts and the active scheme name, applies.
h.config['custom_colors_dark'] = {'def:comment': {'fg': '#ff0000'}}
h.config['active_custom_scheme'] = 'mine'
win3 = capture_window(lambda: h._on_custom_colors(None))
btns3 = {b.get_label(): b for b in labeled_buttons(win3)}
h.apply_calls = 0
btns3["Reset All"].emit('clicked')
check("Reset All clears custom_colors_dark", h.config['custom_colors_dark'] == {})
check("Reset All clears active_custom_scheme", h.config['active_custom_scheme'] == '')
check("Reset All applies the scheme", h.apply_calls == 1)
check("Reset All closes the window", win3.get_visible() is False)

# Escape cancels without saving or resetting.
h.config['custom_colors_dark'] = {'def:comment': {'fg': '#00ff00'}}
win4 = capture_window(lambda: h._on_custom_colors(None))
key_ctrls4 = escape_key_controllers(win4)
check("custom-colors window has at least one key controller (ours + GTK's own)",
      len(key_ctrls4) >= 1, key_ctrls4)
h.apply_calls = 0
press_escape(win4)
check("Escape does not touch custom_colors_dark",
      h.config['custom_colors_dark'] == {'def:comment': {'fg': '#00ff00'}})
check("Escape does not apply the scheme", h.apply_calls == 0, h.apply_calls)
check("Escape closes the window", win4.get_visible() is False)


print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
