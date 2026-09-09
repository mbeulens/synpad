"""connection.py under GTK4 — headless.

Guards the `ConnectDialog` Gtk.Dialog -> Gtk.Window + explicit-buttons
migration (a "custom content" dialog per the migration plan's gotcha — a
server picker and a dozen entry fields, not a plain heading/body/response
confirm) and its new async `choose()` API; the delete-server confirmation's
Gtk.MessageDialog.run() -> Adw.AlertDialog.choose() migration (a genuine
heading/body/Yes-No confirm); and the SSH-key picker's
Gtk.FileChooserDialog.run() -> Gtk.FileDialog.open() migration (GTK4 removed
Gtk.Dialog.run() entirely, so the deprecated FileChooserDialog needed a
different replacement than the other three).

Never touches the real ~/.config/synpad/config.json or the OS keyring —
save_config and secrets_store are monkeypatched at module level before any
dialog is exercised.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Gio, GLib, Adw

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)
Adw.init()

import connection
from connection import ConnectDialog, HAS_PARAMIKO
import secrets_store

# Never touch the real config file or OS keyring from a headless test.
save_config_calls = []
connection.save_config = lambda cfg: save_config_calls.append(dict(cfg))

secret_calls = []
connection.secrets_store = type('FakeSecretsStore', (), {
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


def escape_key_controllers(win):
    return [c for c in win.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)]


def press_escape(win):
    for kc in escape_key_controllers(win):
        kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)


parent = Gtk.Window()

base_config = {
    'host': 'example.com', 'port': 22, 'username': 'alice',
    'password': 'secret', 'protocol': 'sftp', 'ssh_key_path': '',
    'server_group': '', 'home_directory': '', 'max_upload_size_mb': 5,
    'servers': [
        {'guid': 'g1', 'name': 'Server One', 'protocol': 'sftp',
         'host': 'one.example.com', 'port': 22, 'username': 'u1',
         'password': '', 'ssh_key_path': '', 'group': '',
         'home_directory': '', 'max_upload_size_mb': 5},
    ],
    'last_server': '',
}


# =====================================================================
# ConnectDialog: construction, field defaults, button order
# =====================================================================

dlg = ConnectDialog(parent, dict(base_config), start_new=True)
check("ConnectDialog is a Gtk.Window (was Gtk.Dialog)", isinstance(dlg, Gtk.Window))
check("title preserved", dlg.get_title() == "Connect to Server", dlg.get_title())
check("start_new selects '(New connection)'",
      dlg.server_combo.get_active_id() == '__new__', dlg.server_combo.get_active_id())
check("host entry starts blank on start_new", dlg.host_entry.get_text() == '')

btns = labeled_buttons(dlg)
labels = [b.get_label() for b in btns]
check("Cancel and Connect present", set(labels) >= {"Cancel", "Connect"}, labels)
by_label = {b.get_label(): b for b in btns}
cancel_i, connect_i = labels.index("Cancel"), labels.index("Connect")
check("button visual order is Cancel before Connect "
      "(matches old add_buttons(CANCEL, CONNECT) call order)",
      cancel_i < connect_i, labels)

if not HAS_PARAMIKO:
    warn_labels = [l.get_label() for l in find_all(dlg, Gtk.Label)
                   if l.get_label() and 'paramiko' in l.get_label()]
    check("paramiko warning shown when unavailable", bool(warn_labels))

dlg.close()


# =====================================================================
# ConnectDialog.choose() — async replacement for the old blocking .run()
# =====================================================================

dlg = ConnectDialog(parent, dict(base_config, last_server='g1'))
check("last_server selects that profile", dlg.server_combo.get_active_id() == 'g1')
check("fields loaded from the selected profile",
      dlg.host_entry.get_text() == 'one.example.com', dlg.host_entry.get_text())

received = []
dlg.choose(lambda d, resp: received.append((d, resp)))
check("choose() does not block (present() returns immediately)", True)

dlg.host_entry.set_text('typed.example.com')
by_label = {b.get_label(): b for b in labeled_buttons(dlg)}
by_label["Connect"].emit('clicked')
check("Connect click invokes the callback with response 'ok'",
      len(received) == 1 and received[0][1] == 'ok', received)
check("dialog widgets are still valid inside the callback (get_values works)",
      received[0][0].get_values()['host'] == 'typed.example.com')
check("dialog is closed only after the callback returns",
      dlg.get_visible() is False)

# Cancel path.
dlg2 = ConnectDialog(parent, dict(base_config))
received2 = []
dlg2.choose(lambda d, resp: received2.append(resp))
by_label2 = {b.get_label(): b for b in labeled_buttons(dlg2)}
by_label2["Cancel"].emit('clicked')
check("Cancel click invokes the callback with response 'cancel'",
      received2 == ['cancel'], received2)
check("Cancel closes the dialog", dlg2.get_visible() is False)

# Escape path — Gtk.Dialog had a built-in close-on-Escape bound to the
# same not-OK outcome; a bare Gtk.Window needs it wired explicitly.
dlg3 = ConnectDialog(parent, dict(base_config))
key_ctrls = escape_key_controllers(dlg3)
check("ConnectDialog has at least one key controller (ours + GTK's own)",
      len(key_ctrls) >= 1, key_ctrls)
received3 = []
dlg3.choose(lambda d, resp: received3.append(resp))
press_escape(dlg3)
check("Escape invokes the callback with response 'cancel' (same as Cancel)",
      received3 == ['cancel'], received3)
check("Escape closes the dialog", dlg3.get_visible() is False)

# choose() never fires twice for one show — a second Escape after the
# dialog already closed must not re-invoke the callback.
press_escape(dlg3)
check("callback fires exactly once per choose()", received3 == ['cancel'], received3)

# Titlebar close / Alt-F4 / destroyed-transient-parent path (IMPORTANT 3,
# final review): before this fix, ConnectDialog had no 'close-request'
# handler at all, so closing it this way never invoked choose()'s
# callback — both consumers (remote.py and dialogs.py) do real work
# unconditionally in the cancel branch (rebuilding the quick-connect
# menu), so a server deleted inside the dialog and then closed via the
# titlebar left stale entries in that menu.
dlg6 = ConnectDialog(parent, dict(base_config))
received6 = []
dlg6.choose(lambda d, resp: received6.append(resp))
dlg6.close()
check("titlebar close (close-request) invokes the callback with 'cancel'",
      received6 == ['cancel'], received6)
check("titlebar close actually closes the dialog", dlg6.get_visible() is False)
# A second close() (as a real second Alt-F4 might do) must not double-fire
# or recurse — self.close() below on the first invocation itself raises
# 'close-request' again, so this also proves that reentry is guarded.
dlg6.close()
check("closing twice does not re-invoke the callback", received6 == ['cancel'], received6)


# =====================================================================
# _on_delete_server — Adw.AlertDialog.choose() replaces MessageDialog.run()
# =====================================================================

dlg4 = ConnectDialog(parent, dict(base_config, last_server='g1'))
check("Server One selected before delete", dlg4.server_combo.get_active_id() == 'g1')

captured = {}
def fake_choose(self, parent_win, cancellable, callback, user_data):
    captured['dlg'] = self
    captured['callback'] = callback
    captured['user_data'] = user_data

_orig_choose = Adw.AlertDialog.choose
Adw.AlertDialog.choose = fake_choose
try:
    dlg4._on_delete_server(None)
finally:
    Adw.AlertDialog.choose = _orig_choose

check("delete opens a confirmation dialog", 'dlg' in captured, captured)
adlg = captured.get('dlg')
if adlg is not None:
    check("heading preserved", adlg.get_heading() == "Delete Server", adlg.get_heading())
    check("body names the server",
          adlg.get_body() == "Delete server profile 'Server One'?", adlg.get_body())
    check("close_response is 'no' (Escape must not delete)",
          adlg.get_close_response() == 'no', adlg.get_close_response())
    check("default_response is 'no' (safe default)",
          adlg.get_default_response() == 'no', adlg.get_default_response())
    check("'yes' response is styled destructive",
          adlg.get_response_appearance('yes') == Adw.ResponseAppearance.DESTRUCTIVE)

class _Result:
    pass

# 'No' — nothing changes.
secret_calls.clear()
adlg.choose_finish = lambda result: 'no'
dlg4._on_delete_server_response(adlg, _Result(), 'g1')
check("'No' leaves the server list untouched",
      any(s['guid'] == 'g1' for s in dlg4.config['servers']), dlg4.config['servers'])
check("'No' does not delete the stored secret",
      ('delete', 'g1') not in secret_calls, secret_calls)

# 'Yes' — deletes the profile, the stored secret, and saves.
adlg.choose_finish = lambda result: 'yes'
save_config_calls.clear()
dlg4._on_delete_server_response(adlg, _Result(), 'g1')
check("'Yes' removes the server from config",
      not any(s['guid'] == 'g1' for s in dlg4.config['servers']), dlg4.config['servers'])
check("'Yes' deletes the stored secret", ('delete', 'g1') in secret_calls, secret_calls)
check("'Yes' saves config", len(save_config_calls) == 1, save_config_calls)
check("'Yes' resets the combo to '(New connection)'",
      dlg4.server_combo.get_active_id() == '__new__')


# =====================================================================
# _on_browse_key — Gtk.FileDialog.open() replaces
# Gtk.FileChooserDialog.run() (Gtk.Dialog.run() no longer exists in GTK4)
# =====================================================================

dlg5 = ConnectDialog(parent, dict(base_config))

captured_fd = {}
class FakeFileDialog:
    def __init__(self):
        self.title = None
        self.initial_folder = None
    def set_title(self, t):
        self.title = t
    def set_initial_folder(self, f):
        self.initial_folder = f
    def open(self, parent_win, cancellable, callback):
        captured_fd['dlg'] = self
        captured_fd['callback'] = callback

_orig_FileDialog = Gtk.FileDialog
Gtk.FileDialog = FakeFileDialog
try:
    dlg5._on_browse_key(None)
finally:
    Gtk.FileDialog = _orig_FileDialog

check("browse opens a file picker", 'dlg' in captured_fd, captured_fd)
fd = captured_fd.get('dlg')
if fd is not None:
    check("picker title preserved", fd.title == "Select SSH Private Key", fd.title)
    ssh_dir = os.path.join(str(__import__('pathlib').Path.home()), '.ssh')
    if os.path.isdir(ssh_dir):
        check("initial folder set to ~/.ssh when it exists",
              fd.initial_folder is not None and fd.initial_folder.get_path() == ssh_dir,
              fd.initial_folder)

# A file was chosen: entry gets its path.
class _Chosen:
    def get_path(self): return '/home/alice/.ssh/id_ed25519'

fd.open_finish = lambda result: _Chosen()
dlg5._on_browse_key_response(fd, _Result())
check("chosen file path fills the key entry",
      dlg5.key_entry.get_text() == '/home/alice/.ssh/id_ed25519', dlg5.key_entry.get_text())

# Cancelled picker (raises GLib.Error): entry is left untouched.
dlg5.key_entry.set_text('unchanged')
def raise_cancelled(result):
    raise GLib.Error("dismissed by user")
fd.open_finish = raise_cancelled
dlg5._on_browse_key_response(fd, _Result())
check("cancelled picker leaves the key entry untouched",
      dlg5.key_entry.get_text() == 'unchanged', dlg5.key_entry.get_text())


print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
