"""remote.py under GTK4 — headless.

Guards:
- `_on_connect` reusing `connection.ConnectDialog.choose()` per the Task 2
  async dialog API (no second dialog pattern invented).
- `_ask_name` / `_confirm_delete`: these are called *synchronously* (for a
  blocking return value) not only by this module's own tree handlers but
  also by the already-ported local_files.py, which this task may not
  modify — GTK4 has no synchronous dialog API at all, so both methods keep
  their old synchronous signature via a private GLib.MainLoop pumped until
  the dialog resolves. This file drives the *real* dialog widgets (not a
  fake) through that mainloop, by scheduling the simulated click/keypress
  with GLib.idle_add at window/dialog-construction time — since a bare
  GLib.MainLoop() iterates the same default main context idle_add attaches
  to, the scheduled action fires once the nested loop starts pumping.
- `_show_permissions_dialog`: no caller reads a return value, so this
  converts straight to the async button-callback pattern (Shape B) with no
  mainloop shim — mirrors local_files.py's `_show_local_permissions_dialog`.
- The tree right-click context menu and the quick-connect menu's
  Gtk.Menu/Gtk.MenuItem -> Gio.Menu + Gtk.PopoverMenu / Gio.SimpleAction
  migration, preserving every item and label.

Never touches the real ~/.config/synpad/config.json or the OS keyring —
save_config and secrets_store are monkeypatched at module level before any
dialog is exercised (remote.py calls save_config() from code paths these
tests exercise, per the migration plan's dialog-side-effect gotcha).
"""
import os, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Gio, GLib, Adw

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)
Adw.init()

import remote
from remote import RemoteMixin, SFTPManager
from connection import ConnectDialog as _RealConnectDialog

# Never touch the real config file or OS keyring from a headless test.
save_config_calls = []
remote.save_config = lambda cfg: save_config_calls.append(dict(cfg))

secret_calls = []
remote.secrets_store = type('FakeSecretsStore', (), {
    'get_password': staticmethod(lambda guid: secret_calls.append(('get', guid)) or None),
    'set_password': staticmethod(lambda guid, pwd: secret_calls.append(('set', guid, pwd)) or False),
    'delete_password': staticmethod(lambda guid: secret_calls.append(('delete', guid))),
})()

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


def find_all(widget, cls):
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


def click_button(win, label):
    for b in labeled_buttons(win):
        if b.get_label() == label:
            b.emit('clicked')
            return
    raise RuntimeError(f"no button labeled {label!r} in {[b.get_label() for b in labeled_buttons(win)]}")


def escape_key_controllers(win):
    return [c for c in win.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)]


def press_escape(win):
    for kc in escape_key_controllers(win):
        kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)


def run_with_scheduled_action(fn, action_fn):
    """Call fn() (expected to build exactly one Gtk.Window via the shared
    module-level Gtk.Window constructor and then block in a nested
    GLib.MainLoop), scheduling action_fn(window) via GLib.idle_add at
    window-construction time. A bare GLib.MainLoop() iterates the same
    default main context idle_add attaches to, so the scheduled action
    fires once the nested loop inside fn() starts pumping, and fn() then
    returns normally once the action resolves the dialog — this drives the
    real synchronous-return mainloop shim end to end, not just its
    construction."""
    captured = []
    class _CapturingWindow(Gtk.Window):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
            GLib.idle_add(lambda: (action_fn(self), False)[1])
    _Real = Gtk.Window
    Gtk.Window = _CapturingWindow
    try:
        result = fn()
    finally:
        Gtk.Window = _Real
    return result, (captured[0] if captured else None)


def run_confirm_with_response(fn, response):
    """Same idea as run_with_scheduled_action, but for _confirm_delete's
    Adw.AlertDialog: fakes Adw.AlertDialog.choose() to schedule the
    callback via GLib.idle_add (queued before the nested loop.run() call,
    so it fires once that loop starts pumping) instead of calling it
    synchronously in-line, so the mainloop-shim is genuinely exercised."""
    captured = {}
    class _Result:
        pass
    def fake_choose(self, parent_win, cancellable, callback):
        captured['dlg'] = self
        self.choose_finish = lambda res: response
        GLib.idle_add(lambda: (callback(self, _Result()), False)[1])
    _orig = Adw.AlertDialog.choose
    Adw.AlertDialog.choose = fake_choose
    try:
        result = fn()
    finally:
        Adw.AlertDialog.choose = _orig
    return result, captured.get('dlg')


class FakeWidget:
    def __init__(self):
        self.sensitive = []
    def set_sensitive(self, v): self.sensitive.append(v)


class FakeHeader:
    def __init__(self): self.subtitle = None
    def set_subtitle(self, s): self.subtitle = s


class FakeNotebook:
    def __init__(self, current=0): self._current = current
    def get_current_page(self): return self._current
    def set_current_page(self, n): self._current = n


class FakeFtpMgr:
    """Stands in for FTPManager/SFTPManager for tests that don't need a
    real connection."""
    def __init__(self, connected=True):
        self.connected = connected
        self.home_dir = '/home/user'
        self.calls = []


class Host(RemoteMixin, Gtk.Window):
    """A real Gtk.Window subclass — every dialog/window here uses
    `transient_for=self`, so the host must be an actual window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.config = {}
        self.tabs = {}
        self.ftp_mgr = None
        self.current_server_guid = ''
        self.tree_store = Gtk.TreeStore(str, str, str, bool, bool)
        self.tree_view = Gtk.TreeView(model=self.tree_store)
        self.quick_btn = Gtk.MenuButton()
        self.header = FakeHeader()
        self.btn_connect = FakeWidget()
        self.btn_disconnect = FakeWidget()
        self.btn_refresh = FakeWidget()
        self.item_save = FakeWidget()
        self.notebook = FakeNotebook()
        self.tmp_dir = tempfile.mkdtemp()

        self.status = []
        self.errors = []
        self.console = []
        self.do_connect_calls = []
        self.rebuild_calls = 0

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _is_editor_file(self, path): return True
    def _open_external(self, path): pass
    def _create_editor_tab(self, *a, **kw): pass
    def _debug(self, msg): pass
    def _update_tab_label(self, tab, name): pass
    def _close_tab(self, page_num): pass
    def _on_refresh(self, _btn): pass
    def _git_show_history_sftp(self, path): self.console.append(('git_history', path))
    def _do_connect(self, vals): self.do_connect_calls.append(vals)


# Host the tree view (and quick_btn) in a real, shown window: popovers
# segfault if popup() is called before their widget has a realized
# toplevel ancestor (Gtk.Popover.popup() gotcha from Task 1).
h = Host()
h.set_child(h.tree_view)
h.present()


# =====================================================================
# _rebuild_quick_menu — Gtk.Menu -> Gio.Menu on a Gtk.MenuButton
# =====================================================================

def flatten_labels(menu_model):
    out = []
    for i in range(menu_model.get_n_items()):
        sec = menu_model.get_item_link(i, Gio.MENU_LINK_SECTION)
        sub = menu_model.get_item_link(i, Gio.MENU_LINK_SUBMENU)
        if sec is not None:
            out.extend(flatten_labels(sec))
        elif sub is not None:
            out.extend(flatten_labels(sub))
        else:
            val = menu_model.get_item_attribute_value(i, 'label', None)
            out.append(val.get_string() if val else None)
    return out


def submenu_labels(menu_model):
    result = {}
    for i in range(menu_model.get_n_items()):
        sec = menu_model.get_item_link(i, Gio.MENU_LINK_SECTION)
        sub = menu_model.get_item_link(i, Gio.MENU_LINK_SUBMENU)
        if sec is not None:
            result.update(submenu_labels(sec))
        elif sub is not None:
            val = menu_model.get_item_attribute_value(i, 'label', None)
            result[val.get_string() if val else None] = flatten_labels(sub)
    return result


def action_names(menu_model, prefix):
    names = []
    for i in range(menu_model.get_n_items()):
        sec = menu_model.get_item_link(i, Gio.MENU_LINK_SECTION)
        sub = menu_model.get_item_link(i, Gio.MENU_LINK_SUBMENU)
        if sec is not None:
            names.extend(action_names(sec, prefix))
        elif sub is not None:
            names.extend(action_names(sub, prefix))
        else:
            val = menu_model.get_item_attribute_value(i, 'action', None)
            s = val.get_string() if val else None
            if s and s.startswith(prefix):
                names.append(s[len(prefix):])
    return names


# No servers: quick_btn is hidden.
h.config['servers'] = []
h._rebuild_quick_menu()
check("no servers: quick_btn hidden", h.quick_btn.get_visible() is False)

# Ungrouped + grouped servers.
h.config['servers'] = [
    {'guid': 'g1', 'name': 'Alpha', 'protocol': 'sftp'},
    {'guid': 'g2', 'name': 'Beta', 'protocol': 'ftp', 'group': 'Prod'},
    {'guid': 'g3', 'name': 'Gamma', 'protocol': 'sftp', 'group': 'Prod'},
    {'guid': 'g4', 'name': 'Delta', 'protocol': 'sftp', 'group': 'Dev'},
]
h._rebuild_quick_menu()
check("with servers: quick_btn visible", h.quick_btn.get_visible() is True)
model = h.quick_btn.get_menu_model()
check("menu model set on quick_btn", model is not None)

labels = flatten_labels(model)
check("ungrouped server item present", "Alpha (SFTP)" in labels, labels)
subs = submenu_labels(model)
check("Prod submenu present with its two servers",
      subs.get('Prod') == ['Beta (FTP)', 'Gamma (SFTP)'], subs)
check("Dev submenu present with its one server",
      subs.get('Dev') == ['Delta (SFTP)'], subs)

# Activating an item's action calls _on_quick_connect with the right guid.
names = action_names(model, 'quickconn.')
check("at least one quickconn action present", bool(names), names)
h.do_connect_calls.clear()
alpha_action = None
for i in range(model.get_n_items()):
    sec = model.get_item_link(i, Gio.MENU_LINK_SECTION)
    if sec is not None:
        for j in range(sec.get_n_items()):
            val = sec.get_item_attribute_value(j, 'label', None)
            if val and val.get_string() == 'Alpha (SFTP)':
                act = sec.get_item_attribute_value(j, 'action', None)
                alpha_action = act.get_string()[len('quickconn.'):]
check("found Alpha's action name", alpha_action is not None, alpha_action)
if alpha_action:
    ok = h.quick_btn.activate_action(f'quickconn.{alpha_action}', None)
    check("activating Alpha's action invokes _do_connect via _on_quick_connect",
          ok and h.do_connect_calls and h.do_connect_calls[0].get('server_guid') == 'g1',
          h.do_connect_calls)

# Only ungrouped, or only grouped: single section, no crash either way.
h.config['servers'] = [{'guid': 'g1', 'name': 'Solo', 'protocol': 'sftp'}]
h._rebuild_quick_menu()
check("only-ungrouped: exactly one item", flatten_labels(h.quick_btn.get_menu_model()) ==
      ['Solo (SFTP)'], flatten_labels(h.quick_btn.get_menu_model()))

h.config['servers'] = [{'guid': 'g1', 'name': 'Solo', 'protocol': 'sftp', 'group': 'X'}]
h._rebuild_quick_menu()
check("only-grouped: submenu present with the one server",
      submenu_labels(h.quick_btn.get_menu_model()).get('X') == ['Solo (SFTP)'])


# =====================================================================
# _remote_attach_tree_controllers / _on_tree_right_click
# =====================================================================

before = [type(c).__name__ for c in h.tree_view.observe_controllers()]
h._remote_attach_tree_controllers(h.tree_view)
after = [type(c).__name__ for c in h.tree_view.observe_controllers()]
check("GestureClick installed", "GestureClick" in after, after)
gestures = [c for c in h.tree_view.observe_controllers() if isinstance(c, Gtk.GestureClick)]
check("click gesture restricted to button 3 (was event.button != 3)",
      any(g.get_button() == 3 for g in gestures))


class FakeGesture:
    def __init__(self, widget): self._w = widget
    def get_widget(self): return self._w


# Not connected: right-click is a no-op (no menu built).
h.ftp_mgr = FakeFtpMgr(connected=False)
h._remote_ctx_popover = None
result = h._on_tree_right_click(FakeGesture(h.tree_view), 1, 5.0, 5.0)
check("right-click while disconnected returns False", result is False)
check("right-click while disconnected builds no popover",
      getattr(h, '_remote_ctx_popover', None) is None)

# Connected, empty space -> New File.../New Directory... only.
h.ftp_mgr = FakeFtpMgr(connected=True)
h.config['home_directory'] = ''
orig_get_path = h.tree_view.get_path_at_pos
h.tree_view.get_path_at_pos = lambda x, y: None
h._on_tree_right_click(FakeGesture(h.tree_view), 1, 5.0, 5.0)
h.tree_view.get_path_at_pos = orig_get_path
popover = h._remote_ctx_popover
check("popover created", isinstance(popover, Gtk.PopoverMenu))
menu = popover.get_menu_model()
labels = flatten_labels(menu)
check("empty-space menu: New File...", "New File..." in labels, labels)
check("empty-space menu: New Directory...", "New Directory..." in labels, labels)
check("empty-space menu has exactly 2 items", len(labels) == 2, labels)

# Directory row (non-git, non-SFTP): New/Rename/Permissions/Delete, no git item.
d_iter = h.tree_store.append(None, ['subdir', 'folder', '/home/user/subdir', True, True])
tree_path = h.tree_store.get_path(d_iter)
h.tree_view.expand_all()
h.tree_view.get_path_at_pos = lambda x, y: (tree_path, None, 0, 0)
h._on_tree_right_click(FakeGesture(h.tree_view), 1, 5.0, 5.0)
h.tree_view.get_path_at_pos = orig_get_path
menu = h._remote_ctx_popover.get_menu_model()
labels = flatten_labels(menu)
check("dir menu: New File...", "New File..." in labels, labels)
check("dir menu: New Directory...", "New Directory..." in labels, labels)
check("dir menu: Rename 'subdir'...", "Rename 'subdir'..." in labels, labels)
check("dir menu: Permissions 'subdir'...", "Permissions 'subdir'..." in labels, labels)
check("dir menu: Delete Directory 'subdir'", "Delete Directory 'subdir'" in labels, labels)
check("dir menu: no git-history item (not SFTP / not .git)",
      "Show git history" not in labels, labels)

names = action_names(menu, 'remotectx.')
delete_action = [n for n in names if 'delete' in n]
check("dir menu wires a delete action (invocation covered via Host3 below "
      "— _on_tree_delete_dir calls the real _confirm_delete, which would "
      "otherwise block this test on its own nested mainloop)",
      bool(delete_action), names)

# Directory named '.git' AND ftp_mgr is SFTPManager -> git-history item.
h.ftp_mgr = SFTPManager()  # no .connect() call — no network I/O
h.ftp_mgr.connected = True
git_iter = h.tree_store.append(None, ['.git', 'folder', '/home/user/repo/.git', True, True])
git_path = h.tree_store.get_path(git_iter)
h.tree_view.expand_all()
h.tree_view.get_path_at_pos = lambda x, y: (git_path, None, 0, 0)
h._on_tree_right_click(FakeGesture(h.tree_view), 1, 5.0, 5.0)
h.tree_view.get_path_at_pos = orig_get_path
menu = h._remote_ctx_popover.get_menu_model()
labels = flatten_labels(menu)
check("'.git' dir over SFTP: git-history item present",
      "Show git history" in labels, labels)
names = action_names(menu, 'remotectx.')
git_action = [n for n in names if 'git' in n]
if git_action:
    h.console.clear()
    h.tree_view.activate_action(f'remotectx.{git_action[0]}', None)
    check("git-history action targets the .git path",
          h.console == [('git_history', '/home/user/repo/.git')], h.console)

# File row: Rename/Permissions/Delete, no "Directory" wording, no New items.
f_iter = h.tree_store.append(None, ['file.txt', 'text-x-generic', '/home/user/file.txt', False, False])
f_path = h.tree_store.get_path(f_iter)
h.tree_view.get_path_at_pos = lambda x, y: (f_path, None, 0, 0)
h._on_tree_right_click(FakeGesture(h.tree_view), 1, 5.0, 5.0)
h.tree_view.get_path_at_pos = orig_get_path
menu = h._remote_ctx_popover.get_menu_model()
labels = flatten_labels(menu)
check("file menu: Rename 'file.txt'...", "Rename 'file.txt'..." in labels, labels)
check("file menu: Permissions 'file.txt'...", "Permissions 'file.txt'..." in labels, labels)
check("file menu: Delete 'file.txt' (not 'Delete Directory')",
      "Delete 'file.txt'" in labels, labels)
check("file menu has exactly 3 items", len(labels) == 3, labels)

h.tree_store.clear()
h.ftp_mgr = None


# =====================================================================
# _on_connect / _on_connect_dialog_response — ConnectDialog.choose()
# =====================================================================

class FakeConnectDialog:
    """Stands in for connection.ConnectDialog so this file tests only
    remote.py's own decision logic (what happens with the values on 'ok'
    vs 'cancel'), matching Task 2's worked example for this exact call
    site — not ConnectDialog's own construction (that belongs to
    test_connection_gtk4.py)."""
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


h.rebuild_calls = 0
_orig_ConnectDialog = remote.ConnectDialog
remote.ConnectDialog = FakeConnectDialog
_orig_rebuild = Host._rebuild_quick_menu
Host._rebuild_quick_menu = lambda self: setattr(self, 'rebuild_calls', self.rebuild_calls + 1)
try:
    h._on_connect(None)
    dlg = FakeConnectDialog.last
    check("ConnectDialog constructed (start_new defaults False)", dlg is not None and dlg.start_new is False)
    check("_on_connect calls choose(), not run()", dlg._callback is not None)

    # cancel: no _do_connect, but quick-connect menu still rebuilt.
    h.do_connect_calls.clear()
    h.rebuild_calls = 0
    dlg._callback(dlg, 'cancel')
    check("cancel does not call _do_connect", h.do_connect_calls == [])
    check("cancel still rebuilds the quick-connect menu", h.rebuild_calls == 1)

    # ok: _do_connect called with dialog values, menu rebuilt first.
    dlg._values = {'host': 'h1', 'username': 'u1', 'protocol': 'sftp'}
    h.rebuild_calls = 0
    h.do_connect_calls.clear()
    dlg._callback(dlg, 'ok')
    check("ok calls _do_connect with the dialog's values",
          h.do_connect_calls == [dlg._values], h.do_connect_calls)
    check("ok also rebuilds the quick-connect menu", h.rebuild_calls == 1)
finally:
    remote.ConnectDialog = _orig_ConnectDialog
    Host._rebuild_quick_menu = _orig_rebuild


# =====================================================================
# _ask_name — synchronous return preserved via a private GLib.MainLoop
# =====================================================================

h2 = Host()

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:"),
    lambda w: click_button(w, "Cancel"))
check("_ask_name does not block forever (Cancel path returned)", True)
check("_ask_name Cancel returns None", result is None, result)
check("_ask_name window title", win.get_title() == "New File", win.get_title())
check("_ask_name window closes on Cancel", win.get_visible() is False)

def set_entry_and_click(win, text, label):
    find_all(win, Gtk.Entry)[0].set_text(text)
    click_button(win, label)

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:"),
    lambda w: set_entry_and_click(w, "hello.txt", "Create"))
check("_ask_name Create with text returns the name", result == "hello.txt", result)
check("_ask_name Create closes the window", win.get_visible() is False)

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:"),
    lambda w: set_entry_and_click(w, "   ", "Create"))
check("_ask_name Create with blank/whitespace text returns None", result is None, result)

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("Rename", "New name for 'old.txt':",
                          default_value='old.txt', ok_label='Rename'),
    lambda w: click_button(w, "Rename"))
check("_ask_name pre-fills default_value and Rename returns it unchanged",
      result == 'old.txt', result)

def press_enter(win, text):
    entry = find_all(win, Gtk.Entry)[0]
    entry.set_text(text)
    entry.emit('activate')

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:"),
    lambda w: press_enter(w, 'enter.txt'))
check("_ask_name Enter-in-entry submits like the OK button", result == 'enter.txt', result)

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:"),
    lambda w: press_escape(w))
check("_ask_name Escape returns None (was RESPONSE_DELETE_EVENT)", result is None, result)
check("_ask_name Escape closes the window", win.get_visible() is False)

result, win = run_with_scheduled_action(
    lambda: h2._ask_name("New File", "File name:", ok_label="Create"),
    lambda w: click_button(w, "Cancel"))
labels = [b.get_label() for b in labeled_buttons(win)]
check("_ask_name button visual order is [Create, Cancel] (matches old pack_end reversal)",
      labels == ["Create", "Cancel"], labels)


# =====================================================================
# _confirm_delete — synchronous bool return preserved via a private
# GLib.MainLoop wrapping Adw.AlertDialog's async choose()
# =====================================================================

result, dlg = run_confirm_with_response(
    lambda: h2._confirm_delete('/remote/secret.txt'), 'yes')
check("_confirm_delete does not block forever (resolved via mainloop shim)", True)
check("_confirm_delete returns True on 'Yes'", result is True, result)
check("heading preserved", dlg.get_heading() == "Confirm Delete", dlg.get_heading())
check("body names the path",
      dlg.get_body() == "Are you sure you want to delete:\n\n/remote/secret.txt"
                         "\n\nThis cannot be undone.", dlg.get_body())
check("close_response is 'no' (Escape must not delete)",
      dlg.get_close_response() == 'no', dlg.get_close_response())
check("default_response is 'no' (safe default)",
      dlg.get_default_response() == 'no', dlg.get_default_response())
check("'yes' response is styled destructive",
      dlg.get_response_appearance('yes') == Adw.ResponseAppearance.DESTRUCTIVE)

result, dlg = run_confirm_with_response(
    lambda: h2._confirm_delete('/remote/secret.txt'), 'no')
check("_confirm_delete returns False on 'No'", result is False, result)


# =====================================================================
# _on_tree_new_file / _on_tree_new_dir / _on_tree_rename /
# _on_tree_delete_file / _on_tree_delete_dir — unchanged call-through into
# the now-async-shimmed _ask_name / _confirm_delete (stubbed here, since
# their own real behavior is covered above and by local_files.py's
# established stubbing convention for these same two shared helpers).
# =====================================================================

class Host3(RemoteMixin, Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self)
        self.config = {}
        self.tabs = {}
        self.ftp_mgr = FakeFtpMgr(connected=True)
        self.tree_store = Gtk.TreeStore(str, str, str, bool, bool)
        self.status = []
        self.errors = []
        self.console = []
        self.ask_name_return = None
        self.confirm_return = True
        self.mkfile_calls = []
        self.mkdir_calls = []
        self.rename_calls = []
        self.rmfile_calls = []
        self.rmdir_calls = []
        self.ftp_mgr.mkfile = lambda p: self.mkfile_calls.append(p)
        self.ftp_mgr.mkdir = lambda p: self.mkdir_calls.append(p)
        self.ftp_mgr.rename = lambda a, b: self.rename_calls.append((a, b))
        self.ftp_mgr.rmfile = lambda p: self.rmfile_calls.append(p)
        self.ftp_mgr.rmdir = lambda p: self.rmdir_calls.append(p)

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _ask_name(self, *a, **kw): return self.ask_name_return
    def _confirm_delete(self, what): return self.confirm_return
    def _on_tree_file_created(self, *a, **kw): self.console.append(('created', a))
    def _on_tree_renamed(self, *a, **kw): self.console.append(('renamed', a))
    def _on_tree_item_deleted(self, *a, **kw): self.console.append(('deleted', a))


h3 = Host3()
_orig_thread = remote.threading.Thread
_orig_idle = remote.GLib.idle_add
class SyncThread:
    def __init__(self, target=None, daemon=None): self._t = target
    def start(self): self._t()
remote.threading.Thread = SyncThread
remote.GLib.idle_add = lambda fn, *a: fn(*a)
try:
    # New File: ask_name returns None -> no-op.
    h3.ask_name_return = None
    h3._on_tree_new_file('/remote/dir', None)
    check("New File with no name is a no-op", h3.mkfile_calls == [], h3.mkfile_calls)

    h3.ask_name_return = 'new.txt'
    h3._on_tree_new_file('/remote/dir', None)
    check("New File creates the file at parent_dir/name",
          h3.mkfile_calls == ['/remote/dir/new.txt'], h3.mkfile_calls)

    h3.ask_name_return = 'newdir'
    h3._on_tree_new_dir('/remote/dir', None)
    check("New Directory creates the dir at parent_dir/name",
          h3.mkdir_calls == ['/remote/dir/newdir'], h3.mkdir_calls)

    # Rename: unchanged name is a no-op; changed name renames.
    h3.ask_name_return = 'old.txt'
    h3._on_tree_rename('/remote/old.txt', 'old.txt', None)
    check("Rename to the same name is a no-op", h3.rename_calls == [], h3.rename_calls)

    h3.ask_name_return = 'new.txt'
    h3._on_tree_rename('/remote/old.txt', 'old.txt', None)
    check("Rename to a new name calls ftp_mgr.rename",
          h3.rename_calls == [('/remote/old.txt', '/remote/new.txt')], h3.rename_calls)

    # Delete: confirm False -> no-op; True -> deletes.
    h3.confirm_return = False
    h3._on_tree_delete_file('/remote/x.txt', None)
    check("Delete file without confirmation is a no-op", h3.rmfile_calls == [])
    h3.confirm_return = True
    h3._on_tree_delete_file('/remote/x.txt', None)
    check("Delete file with confirmation calls ftp_mgr.rmfile",
          h3.rmfile_calls == ['/remote/x.txt'], h3.rmfile_calls)

    h3.confirm_return = True
    h3._on_tree_delete_dir('/remote/d', None)
    check("Delete directory with confirmation calls ftp_mgr.rmdir",
          h3.rmdir_calls == ['/remote/d'], h3.rmdir_calls)
finally:
    remote.threading.Thread = _orig_thread
    remote.GLib.idle_add = _orig_idle


# =====================================================================
# _show_permissions_dialog — async Shape B, no synchronous-return shim
# needed (no caller reads a return value)
# =====================================================================

h4 = Host()
h4.ftp_mgr = FakeFtpMgr(connected=True)
chmod_calls = []
h4.ftp_mgr.chmod = lambda path, mode: chmod_calls.append((path, mode))

captured = []
class _CapturingWindow(Gtk.Window):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        captured.append(self)
_RealWindow = Gtk.Window
Gtk.Window = _CapturingWindow
try:
    h4._show_permissions_dialog('/remote/file.txt', 'file.txt', 0o644, 'u', 'g')
finally:
    Gtk.Window = _RealWindow

check("permissions dialog window created", len(captured) == 1, captured)
win = captured[0]
check("dialog title includes the file name", win.get_title() == "Permissions — file.txt",
      win.get_title())

checks = find_all(win, Gtk.CheckButton)
check("9 permission checkboxes (3 x rwx)", len(checks) == 9, len(checks))
expected = [True, True, False, True, False, False, True, False, False]
check("checkboxes reflect 0o644 (rw-r--r--)",
      [c.get_active() for c in checks] == expected, [c.get_active() for c in checks])

entries = find_all(win, Gtk.Entry)
octal_entry = entries[0]
check("octal entry initialized to 644", octal_entry.get_text() == "644", octal_entry.get_text())

buttons = labeled_buttons(win)
labels = [b.get_label() for b in buttons]
check("Cancel and Apply present", set(labels) == {"Cancel", "Apply"}, labels)
check("button visual order is [Apply, Cancel] (matches old pack_end reversal)",
      labels == ["Apply", "Cancel"], labels)

checks[1].set_active(False)  # clear Owner-write -> 0o644 becomes 0o444
check("checkbox toggle updates octal entry", octal_entry.get_text() == "444", octal_entry.get_text())
checks[1].set_active(True)

octal_entry.set_text("755")
check("octal entry edit updates checkboxes",
      [c.get_active() for c in checks] ==
      [True, True, True, True, False, True, True, False, True])
octal_entry.set_text("644")

by_label = {b.get_label(): b for b in buttons}
by_label["Cancel"].emit('clicked')
check("Cancel does not call chmod", chmod_calls == [], chmod_calls)
check("Cancel closes the window", win.get_visible() is False)

captured.clear()
Gtk.Window = _CapturingWindow
try:
    h4._show_permissions_dialog('/remote/file.txt', 'file.txt', 0o644, 'u', 'g')
finally:
    Gtk.Window = _RealWindow
win2 = captured[0]
by_label2 = {b.get_label(): b for b in labeled_buttons(win2)}
# _apply_permissions runs chmod in a background thread and reports status
# via GLib.idle_add — fake both so the synchronous test can observe the
# full chain instead of only the pre-thread side effects.
class SyncThread2:
    def __init__(self, target=None, daemon=None): self._t = target
    def start(self): self._t()
_orig_thread2 = remote.threading.Thread
_orig_idle2 = remote.GLib.idle_add
remote.threading.Thread = SyncThread2
remote.GLib.idle_add = lambda fn, *a: fn(*a)
try:
    by_label2["Apply"].emit('clicked')
finally:
    remote.threading.Thread = _orig_thread2
    remote.GLib.idle_add = _orig_idle2
check("Apply calls ftp_mgr.chmod with the octal value",
      chmod_calls == [('/remote/file.txt', 0o644)], chmod_calls)
check("Apply reports status", h4.status and 'Permissions set' in h4.status[-1], h4.status)

# Invalid octal reports an error and still closes, without raising.
h4.errors.clear()
captured.clear()
Gtk.Window = _CapturingWindow
try:
    h4._show_permissions_dialog('/remote/file.txt', 'file.txt', 0o644, 'u', 'g')
finally:
    Gtk.Window = _RealWindow
win3 = captured[0]
find_all(win3, Gtk.Entry)[0].set_text("not-octal")
by_label3 = {b.get_label(): b for b in labeled_buttons(win3)}
try:
    by_label3["Apply"].emit('clicked')
    ok = True
except Exception:
    ok = False
check("invalid octal reports an error without raising",
      ok and h4.errors and h4.errors[-1][0] == "Invalid Permissions", h4.errors)
check("invalid octal still closes the window", win3.get_visible() is False)

# Escape cancels without applying (was RESPONSE_DELETE_EVENT).
captured.clear()
Gtk.Window = _CapturingWindow
try:
    h4._show_permissions_dialog('/remote/file.txt', 'file.txt', 0o644, 'u', 'g')
finally:
    Gtk.Window = _RealWindow
win4 = captured[0]
key_ctrls = escape_key_controllers(win4)
check("permissions dialog has at least one key controller (ours + GTK's own)",
      len(key_ctrls) >= 1, key_ctrls)
chmod_calls.clear()
h4.status.clear()
for kc in key_ctrls:
    kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)
check("Escape does not call chmod", chmod_calls == [], chmod_calls)
check("Escape reports no status change", h4.status == [], h4.status)
check("Escape closes the window", win4.get_visible() is False)


shutil.rmtree(h.tmp_dir, ignore_errors=True)
shutil.rmtree(h4.tmp_dir, ignore_errors=True)

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
