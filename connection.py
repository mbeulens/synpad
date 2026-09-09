"""SynPad FTP/SFTP connection managers and dialog.

GTK4: `ConnectDialog` has real body content (server picker, a dozen entry
fields) so per the migration plan's dialog gotcha it is a plain Gtk.Window
with explicit buttons, not Adw.AlertDialog — it exposes an async `choose()`
method that mirrors Adw.AlertDialog.choose()'s callback shape so callers
don't need to special-case which kind of dialog they're driving. The
delete-server confirmation *is* a plain heading/body/Yes-No confirm, so it
uses Adw.AlertDialog. The SSH-key file picker used Gtk.FileChooserDialog
(Gtk.Dialog subclass) under GTK3; GTK4 removed `Gtk.Dialog.run()` entirely,
and FileChooserDialog is deprecated as of GTK 4.10, so this uses the native
async replacement `Gtk.FileDialog` instead (built into GTK4, no new
dependency)."""

import ftplib
import hashlib
import os
import stat
import uuid
from pathlib import Path

import secrets_store

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Gio, GLib, Adw

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

from config import find_server_by_guid, save_config

# Timeouts (seconds) for SFTP channel operations. SFTP_OP_TIMEOUT caps every
# blocking read/write so a half-open connection raises socket.timeout instead
# of freezing the UI forever; SFTP_PROBE_TIMEOUT makes is_alive() fail fast.
# paramiko's set_keepalive does NOT abort the transport on missed replies, so
# without these a silently-dropped connection hangs the next operation forever.
SFTP_OP_TIMEOUT = 30
SFTP_PROBE_TIMEOUT = 5


class FTPManager:
    """Handles all FTP operations."""

    def __init__(self):
        self.ftp = None
        self.connected = False
        self.home_dir = '/'

    def connect(self, host, port, username, password):
        self.ftp = ftplib.FTP()
        self.ftp.connect(host, port, timeout=10)
        self.ftp.login(username, password)
        self.ftp.set_pasv(True)
        # Apply read timeout to all subsequent commands so a half-open
        # control connection raises instead of hanging forever.
        try:
            self.ftp.sock.settimeout(30)
        except Exception:
            pass
        self.connected = True
        # Auto-detect home directory
        try:
            self.home_dir = self.ftp.pwd()
        except Exception:
            self.home_dir = '/'

    def disconnect(self):
        if self.ftp and self.connected:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
        self.ftp = None
        self.connected = False

    def is_alive(self):
        """Cheap liveness probe via NOOP. Temporarily lowers the socket timeout
        to 5s so a dead connection surfaces quickly instead of blocking the UI
        for the full 30s read timeout. Flips self.connected to False on
        detected death."""
        if not self.connected or self.ftp is None:
            return False
        sock = self.ftp.sock
        old_timeout = None
        try:
            if sock:
                old_timeout = sock.gettimeout()
                sock.settimeout(5)
            self.ftp.voidcmd('NOOP')
            return True
        except Exception:
            self.connected = False
            return False
        finally:
            if sock and old_timeout is not None:
                try:
                    sock.settimeout(old_timeout)
                except Exception:
                    pass

    def list_dir(self, path='/'):
        """Return list of (name, is_dir) tuples for the given remote path."""
        entries = []
        lines = []
        self.ftp.cwd(path)
        self.ftp.retrlines('LIST', lines.append)
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8]
            if name in ('.', '..'):
                continue
            is_dir = line.startswith('d')
            entries.append((name, is_dir))
        entries.sort(key=lambda x: (not x[1], x[0].lower()))
        return entries

    def download(self, remote_path, local_path):
        with open(local_path, 'wb') as f:
            self.ftp.retrbinary(f'RETR {remote_path}', f.write)

    def upload(self, remote_path, local_path, max_size_mb):
        file_size = os.path.getsize(local_path)
        max_bytes = max_size_mb * 1024 * 1024
        if file_size > max_bytes:
            raise ValueError(
                f"File size ({file_size / 1024 / 1024:.2f} MB) exceeds "
                f"limit ({max_size_mb} MB)"
            )
        # CWD into the directory first, then STOR by filename only.
        # This avoids permission issues on servers that restrict
        # full-path writes.
        remote_dir = os.path.dirname(remote_path)
        remote_name = os.path.basename(remote_path)
        if remote_dir:
            self.ftp.cwd(remote_dir)
        with open(local_path, 'rb') as f:
            self.ftp.storbinary(f'STOR {remote_name}', f)

    def get_remote_size(self, remote_path):
        try:
            return self.ftp.size(remote_path)
        except Exception:
            return None

    def get_remote_mtime(self, remote_path):
        """Get the modification time of a remote file (Unix timestamp)."""
        try:
            resp = self.ftp.sendcmd(f'MDTM {remote_path}')
            # Response: 213 YYYYMMDDHHMMSS
            ts_str = resp.split()[1]
            import datetime
            dt = datetime.datetime.strptime(ts_str, '%Y%m%d%H%M%S')
            return dt.timestamp()
        except Exception:
            return None

    def get_remote_hash(self, remote_path):
        """Download file content and return its SHA256 hash."""
        try:
            h = hashlib.sha256()
            self.ftp.retrbinary(f'RETR {remote_path}', h.update)
            return h.hexdigest()
        except Exception:
            return None

    def mkdir(self, remote_path):
        self.ftp.mkd(remote_path)

    def mkfile(self, remote_path):
        """Create an empty file on the server."""
        import io
        remote_dir = os.path.dirname(remote_path)
        remote_name = os.path.basename(remote_path)
        if remote_dir:
            self.ftp.cwd(remote_dir)
        self.ftp.storbinary(f'STOR {remote_name}', io.BytesIO(b''))

    def rmfile(self, remote_path):
        self.ftp.delete(remote_path)

    def rmdir(self, remote_path):
        self.ftp.rmd(remote_path)

    def rename(self, old_path, new_path):
        self.ftp.rename(old_path, new_path)

    def chmod(self, remote_path, mode):
        """Set permissions (octal int, e.g. 0o755). Uses SITE CHMOD."""
        self.ftp.sendcmd(f'SITE CHMOD {oct(mode)[2:]} {remote_path}')

    def get_stat(self, remote_path):
        """Return (mode, uid, gid) — FTP can only get mode from LIST output."""
        lines = []
        self.ftp.retrlines(f'LIST {remote_path}', lines.append)
        if lines:
            # Parse permission string like -rwxr-xr-x
            perms = lines[0][:10]
            mode = self._parse_perm_string(perms)
            # FTP doesn't give numeric uid/gid reliably
            parts = lines[0].split(None, 8)
            owner = parts[2] if len(parts) > 2 else ''
            group = parts[3] if len(parts) > 3 else ''
            return mode, owner, group
        return 0o644, '', ''

    @staticmethod
    def _parse_perm_string(s):
        """Convert -rwxr-xr-x to octal mode int."""
        if len(s) < 10:
            return 0o644
        mode = 0
        for i, ch in enumerate(s[1:]):  # skip type char
            if ch != '-':
                mode |= 1 << (8 - i)
        return mode


# --- SFTP Manager ------------------------------------------------------------

class SFTPManager:
    """Handles all SFTP operations via paramiko."""

    def __init__(self):
        self.transport = None
        self.sftp = None
        self.connected = False
        self.home_dir = '/'

    def connect(self, host, port, username, password, key_path=''):
        if not HAS_PARAMIKO:
            raise ImportError(
                "paramiko is required for SFTP.\n"
                "Install it with: pip install paramiko"
            )
        self.transport = paramiko.Transport((host, port))
        # Keepalive keeps NAT entries alive and surfaces dead connections
        # within ~30s instead of hanging forever on the next operation.
        try:
            self.transport.set_keepalive(30)
        except Exception:
            pass
        # Authenticate with key or password
        if key_path and os.path.isfile(key_path):
            try:
                pkey = paramiko.RSAKey.from_private_key_file(key_path)
            except paramiko.ssh_exception.SSHException:
                try:
                    pkey = paramiko.Ed25519Key.from_private_key_file(key_path)
                except paramiko.ssh_exception.SSHException:
                    pkey = paramiko.ECDSAKey.from_private_key_file(key_path)
            self.transport.connect(username=username, pkey=pkey)
        else:
            self.transport.connect(username=username, password=password)
        self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        # Cap every blocking SFTP read/write so a half-open connection raises
        # socket.timeout instead of hanging the UI forever. Each chunk of a
        # healthy transfer returns well within this, so it only bites on a
        # genuinely dead peer.
        try:
            self.sftp.get_channel().settimeout(SFTP_OP_TIMEOUT)
        except Exception:
            pass
        self.connected = True
        # Auto-detect home directory (resolves '.' to absolute path)
        try:
            self.home_dir = self.sftp.normalize('.')
        except Exception:
            self.home_dir = '/'

    def disconnect(self):
        if self.sftp:
            try:
                self.sftp.close()
            except Exception:
                pass
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass
        self.sftp = None
        self.transport = None
        self.connected = False

    def is_alive(self):
        """Real liveness probe: a lightweight stat() round-trip with a short
        timeout. transport.is_active() alone is insufficient — it stays True on
        a half-open socket (paramiko sends keepalives but never disconnects on
        missed replies), which is exactly what hangs an upload forever. We
        temporarily lower the channel timeout to fail fast, then restore it.
        Flips self.connected to False on detected death."""
        if not self.connected or self.transport is None or self.sftp is None:
            return False
        # Quick structural check first — cheap and short-circuits a closed
        # transport before we attempt a round-trip.
        try:
            if not self.transport.is_active():
                self.connected = False
                return False
        except Exception:
            self.connected = False
            return False
        chan = None
        old_timeout = None
        try:
            chan = self.sftp.get_channel()
            if chan is not None:
                old_timeout = chan.gettimeout()
                chan.settimeout(SFTP_PROBE_TIMEOUT)
            # A stat of the home dir forces an actual request/response, so a
            # half-open connection surfaces as socket.timeout/EOFError here.
            self.sftp.stat(self.home_dir)
            return True
        except Exception:
            self.connected = False
            return False
        finally:
            if chan is not None and old_timeout is not None:
                try:
                    chan.settimeout(old_timeout)
                except Exception:
                    pass

    def list_dir(self, path='/'):
        """Return list of (name, is_dir) tuples for the given remote path."""
        entries = []
        for attr in self.sftp.listdir_attr(path):
            name = attr.filename
            if name in ('.', '..'):
                continue
            is_dir = stat.S_ISDIR(attr.st_mode)
            entries.append((name, is_dir))
        entries.sort(key=lambda x: (not x[1], x[0].lower()))
        return entries

    def download(self, remote_path, local_path):
        self.sftp.get(remote_path, local_path)

    def upload(self, remote_path, local_path, max_size_mb):
        file_size = os.path.getsize(local_path)
        max_bytes = max_size_mb * 1024 * 1024
        if file_size > max_bytes:
            raise ValueError(
                f"File size ({file_size / 1024 / 1024:.2f} MB) exceeds "
                f"limit ({max_size_mb} MB)"
            )
        # Preserve original permissions
        try:
            remote_stat = self.sftp.stat(remote_path)
            original_mode = stat.S_IMODE(remote_stat.st_mode)
        except Exception:
            original_mode = None

        # Write directly into the existing file instead of using put()
        # which creates a temp file + rename and often fails with
        # permission denied (errno 13) on restricted servers.
        try:
            with open(local_path, 'rb') as local_f:
                with self.sftp.open(remote_path, 'wb') as remote_f:
                    remote_f.set_pipelined(True)
                    while True:
                        chunk = local_f.read(32768)
                        if not chunk:
                            break
                        remote_f.write(chunk)
        except PermissionError:
            raise PermissionError(
                f"Permission denied writing to {remote_path}\n\n"
                f"Check that your user has write access to this file "
                f"on the remote server."
            )

        # Restore original permissions
        if original_mode is not None:
            try:
                self.sftp.chmod(remote_path, original_mode)
            except Exception:
                pass  # non-critical

    def get_remote_size(self, remote_path):
        try:
            return self.sftp.stat(remote_path).st_size
        except Exception:
            return None

    def get_remote_mtime(self, remote_path):
        """Get the modification time of a remote file (Unix timestamp)."""
        try:
            return self.sftp.stat(remote_path).st_mtime
        except Exception:
            return None

    def get_remote_hash(self, remote_path):
        """Download file content and return its SHA256 hash."""
        try:
            h = hashlib.sha256()
            with self.sftp.open(remote_path, 'rb') as f:
                while True:
                    chunk = f.read(32768)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def mkdir(self, remote_path):
        self.sftp.mkdir(remote_path)

    def mkfile(self, remote_path):
        """Create an empty file on the server."""
        with self.sftp.open(remote_path, 'w') as f:
            pass

    def rmfile(self, remote_path):
        self.sftp.remove(remote_path)

    def rmdir(self, remote_path):
        self.sftp.rmdir(remote_path)

    def rename(self, old_path, new_path):
        self.sftp.rename(old_path, new_path)

    def chmod(self, remote_path, mode):
        """Set permissions (octal int, e.g. 0o755)."""
        self.sftp.chmod(remote_path, mode)

    def get_stat(self, remote_path):
        """Return (mode, owner, group) for the remote path."""
        attr = self.sftp.stat(remote_path)
        mode = stat.S_IMODE(attr.st_mode)
        return mode, str(attr.st_uid), str(attr.st_gid)


# --- Connection Dialog -------------------------------------------------------

class ConnectDialog(Gtk.Window):
    """Dialog for entering FTP/SFTP connection details.

    GTK4: this used to be a Gtk.Dialog driven with the blocking `.run()` /
    `.destroy()` pattern; both callers (dialogs.py and remote.py) branched
    on the return value. It is now a plain Gtk.Window with explicit
    Cancel/Connect buttons — see `choose()` below for the async replacement
    API. The decision logic each caller ran after `.run()` returned is
    unchanged; only the control flow moves into a callback."""

    def __init__(self, parent, config, start_new=False):
        super().__init__(
            title="Connect to Server",
            transient_for=parent,
            modal=True,
        )
        self.set_default_size(450, -1)
        self.config = config
        self._loading_server = False  # prevent save-trigger during load
        self._response_callback = None
        self._response_user_data = ()
        self._responded = False  # guards close-request re-entry from _respond()'s own self.close()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.set_margin_start(12)
        outer.set_margin_end(12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        self.set_child(outer)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        grid.set_vexpand(True)
        outer.append(grid)

        row = 0

        # --- Saved Servers selector ---
        grid.attach(Gtk.Label(label="Server:", halign=Gtk.Align.END), 0, row, 1, 1)
        server_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.server_combo = Gtk.ComboBoxText()
        self.server_combo.append('__new__', '(New connection)')
        for srv in config.get('servers', []):
            self.server_combo.append(srv['guid'], srv['name'])
        self.server_combo.set_hexpand(True)
        server_box.append(self.server_combo)

        self.btn_save_server = Gtk.Button(label="Save")
        self.btn_save_server.set_tooltip_text("Save current settings as a server profile")
        self.btn_save_server.connect('clicked', self._on_save_server)
        server_box.append(self.btn_save_server)

        self.btn_delete_server = Gtk.Button(label="Delete")
        self.btn_delete_server.set_tooltip_text("Delete selected server profile")
        self.btn_delete_server.connect('clicked', self._on_delete_server)
        server_box.append(self.btn_delete_server)

        grid.attach(server_box, 1, row, 1, 1)
        row += 1

        # Separator
        grid.attach(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), 0, row, 2, 1)
        row += 1

        # Profile name
        grid.attach(Gtk.Label(label="Profile name:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.name_entry = Gtk.Entry(
            hexpand=True,
            placeholder_text="e.g. My Production Server",
        )
        self.name_entry.set_activates_default(True)
        grid.attach(self.name_entry, 1, row, 1, 1)
        row += 1

        # Protocol selector
        grid.attach(Gtk.Label(label="Protocol:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.proto_combo = Gtk.ComboBoxText()
        self.proto_combo.append('sftp', 'SFTP (SSH)')
        self.proto_combo.append('ftp', 'FTP')
        active_proto = config.get('protocol', 'sftp')
        self.proto_combo.set_active_id(active_proto)
        self.proto_combo.connect('changed', self._on_protocol_changed)
        grid.attach(self.proto_combo, 1, row, 1, 1)
        row += 1

        # Host
        grid.attach(Gtk.Label(label="Host:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.host_entry = Gtk.Entry(hexpand=True, text=config.get('host', ''))
        self.host_entry.set_activates_default(True)
        grid.attach(self.host_entry, 1, row, 1, 1)
        row += 1

        # Port
        grid.attach(Gtk.Label(label="Port:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.port_entry = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_entry.set_value(config.get('port', 22))
        grid.attach(self.port_entry, 1, row, 1, 1)
        row += 1

        # Username
        grid.attach(Gtk.Label(label="Username:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.user_entry = Gtk.Entry(hexpand=True, text=config.get('username', ''))
        self.user_entry.set_activates_default(True)
        grid.attach(self.user_entry, 1, row, 1, 1)
        row += 1

        # Password
        grid.attach(Gtk.Label(label="Password:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.pass_entry = Gtk.Entry(
            hexpand=True,
            visibility=False,
            input_purpose=Gtk.InputPurpose.PASSWORD,
            text=config.get('password', ''),
        )
        self.pass_entry.set_activates_default(True)
        grid.attach(self.pass_entry, 1, row, 1, 1)
        row += 1

        # SSH Key path (SFTP only)
        self.key_label = Gtk.Label(label="SSH Key:", halign=Gtk.Align.END)
        grid.attach(self.key_label, 0, row, 1, 1)
        key_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.key_entry = Gtk.Entry(
            hexpand=True,
            text=config.get('ssh_key_path', ''),
            placeholder_text="(optional) path to private key",
        )
        self.key_entry.set_hexpand(True)
        key_box.append(self.key_entry)
        self.key_browse_btn = Gtk.Button(label="Browse...")
        self.key_browse_btn.connect('clicked', self._on_browse_key)
        key_box.append(self.key_browse_btn)
        grid.attach(key_box, 1, row, 1, 1)
        self.key_box_widget = key_box
        row += 1

        # Group
        grid.attach(Gtk.Label(label="Group:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.group_entry = Gtk.Entry(
            hexpand=True,
            text=config.get('server_group', ''),
            placeholder_text="(optional, e.g. Production)",
        )
        self.group_entry.set_activates_default(True)
        grid.attach(self.group_entry, 1, row, 1, 1)
        row += 1

        # Home directory
        grid.attach(Gtk.Label(label="Home dir:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.home_entry = Gtk.Entry(
            hexpand=True,
            text=config.get('home_directory', ''),
            placeholder_text="(auto-detect, or e.g. /home/user)",
        )
        self.home_entry.set_activates_default(True)
        grid.attach(self.home_entry, 1, row, 1, 1)
        row += 1

        # Max upload size
        grid.attach(Gtk.Label(label="Max upload (MB):", halign=Gtk.Align.END), 0, row, 1, 1)
        self.size_entry = Gtk.SpinButton.new_with_range(1, 1000, 1)
        self.size_entry.set_value(config.get('max_upload_size_mb', 5))
        grid.attach(self.size_entry, 1, row, 1, 1)
        row += 1

        # Remember
        self.remember_check = Gtk.CheckButton(label="Remember credentials")
        self.remember_check.set_active(bool(config.get('host')))
        grid.attach(self.remember_check, 1, row, 1, 1)

        # SFTP availability warning
        if not HAS_PARAMIKO:
            row += 1
            warn = Gtk.Label()
            warn.set_markup(
                '<span foreground="orange">paramiko not installed. '
                'SFTP unavailable.\nInstall: pip install paramiko</span>'
            )
            grid.attach(warn, 0, row, 2, 1)

        # Connect the server combo after all fields exist
        self.server_combo.connect('changed', self._on_server_changed)

        # Select last used server or (New connection)
        if start_new:
            self.server_combo.set_active_id('__new__')
        else:
            last = config.get('last_server', '')
            if last and self.server_combo.set_active_id(last) is None:
                self.server_combo.set_active_id('__new__')
            else:
                self.server_combo.set_active_id(last or '__new__')

        self._update_sftp_fields()
        self._update_delete_btn()

        # --- Cancel / Connect buttons (was Gtk.Dialog.add_buttons) ---
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(4)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _b: self._respond('cancel'))
        btn_connect = Gtk.Button(label="Connect")
        btn_connect.add_css_class('suggested-action')
        btn_connect.connect('clicked', lambda _b: self._respond('ok'))
        # add_buttons(CANCEL, CONNECT) preserves call order visually
        # (verified empirically against GTK3: Cancel, then Connect,
        # left-to-right) — append in that same order.
        btn_row.append(btn_cancel)
        btn_row.append(btn_connect)
        outer.append(btn_row)
        self.set_default_widget(btn_connect)

        # Gtk.Dialog closed on Escape (dlg.run() returned
        # RESPONSE_DELETE_EVENT, treated as not-OK by every caller); a bare
        # Gtk.Window has no such built-in behavior, so wire it explicitly
        # to the same cancel path the Cancel button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                self._respond('cancel')
                return True
            return False

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        self.add_controller(key_ctrl)

        # Closing via the titlebar's own close control, Alt-F4, or the
        # transient parent being destroyed all raise 'close-request'
        # without going through Connect/Cancel/Escape — a bare Gtk.Window
        # has no other hook for this, so choose()'s callback was
        # previously stranded on this path entirely (never invoked at
        # all — not even with 'cancel'). Route it to the same cancel path
        # Cancel takes, and let the default handling actually tear the
        # window down (return False) rather than calling self.close()
        # ourselves from inside its own close-request handling.
        def on_close_request(_win):
            self._respond('cancel')
            return False

        self.connect('close-request', on_close_request)

    def choose(self, callback, *user_data):
        """Async replacement for the old `resp = dlg.run()` pattern.

        Shows the dialog and returns immediately. When the user clicks
        Connect, clicks Cancel, or presses Escape, `callback(dialog,
        response, *user_data)` is invoked with `response` being the string
        'ok' (Connect clicked — call `dialog.get_values()` for the entered
        fields) or 'cancel' (Cancel clicked or Escape pressed — no values
        should be read). The dialog is still open (and its widgets valid)
        while the callback runs, and is closed automatically right after
        the callback returns — callers should not call close()/destroy()
        themselves. Mirrors Adw.AlertDialog.choose()'s callback shape so
        call sites don't need to special-case which kind of dialog this
        is."""
        self._response_callback = callback
        self._response_user_data = user_data
        self.present()

    def _respond(self, response):
        # Guards against being invoked twice for one dialog — e.g. the
        # self.close() below raises 'close-request', whose handler also
        # calls _respond('cancel'); without this guard self.close() would
        # recurse into 'close-request' indefinitely instead of bottoming
        # out.
        if self._responded:
            return
        self._responded = True
        cb = self._response_callback
        user_data = self._response_user_data
        self._response_callback = None
        self._response_user_data = ()
        if cb is not None:
            cb(self, response, *user_data)
        self.close()

    def _on_server_changed(self, combo):
        """Load fields from selected server profile."""
        server_id = combo.get_active_id()
        self._update_delete_btn()
        if server_id == '__new__' or server_id is None:
            self._loading_server = True
            self.name_entry.set_text('')
            self.proto_combo.set_active_id('sftp')
            self.host_entry.set_text('')
            self.port_entry.set_value(22)
            self.user_entry.set_text('')
            self.pass_entry.set_text('')
            self.key_entry.set_text('')
            self.group_entry.set_text('')
            self.home_entry.set_text('')
            self.size_entry.set_value(5)
            self.remember_check.set_active(False)
            self._update_sftp_fields()
            self._loading_server = False
            return
        srv = find_server_by_guid(self.config, server_id)
        if srv:
            self._loading_server = True
            self.name_entry.set_text(srv.get('name', ''))
            self.proto_combo.set_active_id(srv.get('protocol', 'sftp'))
            self.host_entry.set_text(srv.get('host', ''))
            self.port_entry.set_value(srv.get('port', 22))
            self.user_entry.set_text(srv.get('username', ''))
            stored_pwd = secrets_store.get_password(srv.get('guid', ''))
            self.pass_entry.set_text(stored_pwd if stored_pwd is not None else srv.get('password', ''))
            self.key_entry.set_text(srv.get('ssh_key_path', ''))
            self.group_entry.set_text(srv.get('group', ''))
            self.home_entry.set_text(srv.get('home_directory', ''))
            self.size_entry.set_value(srv.get('max_upload_size_mb', 5))
            self.remember_check.set_active(True)
            self._update_sftp_fields()
            self._loading_server = False

    def _on_save_server(self, _btn):
        """Save current fields as a server profile using the name field."""
        name = self.name_entry.get_text().strip()
        if not name:
            # Auto-fill from host if empty
            name = self.host_entry.get_text().strip()
            if not name:
                return
            self.name_entry.set_text(name)

        current_id = self.server_combo.get_active_id()
        existing_guid = None
        if current_id and current_id != '__new__':
            existing_guid = current_id

        guid = existing_guid or str(uuid.uuid4())
        pwd = self.pass_entry.get_text()
        # Store the password in the OS secret store. Keep plaintext only
        # as a fallback when the keyring is unavailable.
        stored = secrets_store.set_password(guid, pwd) if pwd else False
        profile = {
            'guid': guid,
            'name': name,
            'group': self.group_entry.get_text().strip(),
            'protocol': self.proto_combo.get_active_id(),
            'host': self.host_entry.get_text().strip(),
            'port': int(self.port_entry.get_value()),
            'username': self.user_entry.get_text().strip(),
            'password': '' if stored else pwd,
            'ssh_key_path': self.key_entry.get_text().strip(),
            'home_directory': self.home_entry.get_text().strip(),
            'max_upload_size_mb': int(self.size_entry.get_value()),
        }

        servers = self.config.get('servers', [])
        if existing_guid:
            for i, srv in enumerate(servers):
                if srv.get('guid') == existing_guid:
                    servers[i] = profile
                    break
            self._rebuild_server_combo()
        else:
            servers.append(profile)
            self.server_combo.append(profile['guid'], name)

        self.config['servers'] = servers
        save_config(self.config)
        self.server_combo.set_active_id(profile['guid'])

    def _on_delete_server(self, _btn):
        """Delete the currently selected server profile.

        GTK4: a plain heading/body/Yes-No confirm, so this is the one
        dialog in this module that fits Adw.AlertDialog. `.run()`'s
        blocking `resp != Gtk.ResponseType.YES` check becomes the async
        `_on_delete_server_response` callback below; the decision logic
        (only delete on Yes) is unchanged."""
        server_id = self.server_combo.get_active_id()
        if server_id == '__new__' or server_id is None:
            return

        srv = find_server_by_guid(self.config, server_id)
        display_name = srv['name'] if srv else server_id

        dlg = Adw.AlertDialog(
            heading="Delete Server",
            body=f"Delete server profile '{display_name}'?",
        )
        dlg.add_response('no', "No")
        dlg.add_response('yes', "Yes")
        dlg.set_response_appearance('yes', Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response('no')
        dlg.set_close_response('no')
        dlg.choose(self, None, self._on_delete_server_response, server_id)

    def _on_delete_server_response(self, dlg, result, server_id):
        response = dlg.choose_finish(result)
        if response != 'yes':
            return

        servers = self.config.get('servers', [])
        self.config['servers'] = [s for s in servers if s.get('guid') != server_id]
        secrets_store.delete_password(server_id)
        save_config(self.config)
        self._rebuild_server_combo()
        self.server_combo.set_active_id('__new__')

    def _rebuild_server_combo(self):
        """Rebuild the server dropdown from config."""
        self.server_combo.remove_all()
        self.server_combo.append('__new__', '(New connection)')
        for srv in self.config.get('servers', []):
            self.server_combo.append(srv['guid'], srv['name'])

    def _update_delete_btn(self):
        server_id = self.server_combo.get_active_id()
        is_saved = server_id is not None and server_id != '__new__'
        self.btn_delete_server.set_sensitive(is_saved)

    def _on_protocol_changed(self, combo):
        proto = combo.get_active_id()
        # Auto-switch port when protocol changes
        current_port = int(self.port_entry.get_value())
        if proto == 'sftp' and current_port == 21:
            self.port_entry.set_value(22)
        elif proto == 'ftp' and current_port == 22:
            self.port_entry.set_value(21)
        self._update_sftp_fields()

    def _update_sftp_fields(self):
        is_sftp = self.proto_combo.get_active_id() == 'sftp'
        self.key_label.set_visible(is_sftp)
        self.key_box_widget.set_visible(is_sftp)

    def _on_browse_key(self, _btn):
        """GTK4: Gtk.FileChooserDialog is a Gtk.Dialog subclass, and
        Gtk.Dialog.run() no longer exists at all in GTK4 (it's also
        deprecated since GTK 4.10 in favor of Gtk.FileDialog). Use the
        native async Gtk.FileDialog instead — built into GTK4, no new
        dependency. The decision logic (only set the entry when a file was
        actually chosen) is unchanged from the old
        `resp == Gtk.ResponseType.OK` check."""
        dlg = Gtk.FileDialog()
        dlg.set_title("Select SSH Private Key")
        # Start in ~/.ssh
        ssh_dir = os.path.join(str(Path.home()), '.ssh')
        if os.path.isdir(ssh_dir):
            dlg.set_initial_folder(Gio.File.new_for_path(ssh_dir))
        dlg.open(self, None, self._on_browse_key_response)

    def _on_browse_key_response(self, dlg, result):
        try:
            file = dlg.open_finish(result)
        except GLib.Error:
            return  # cancelled, or the picker failed — leave entry as-is
        if file is not None:
            path = file.get_path()
            if path:
                self.key_entry.set_text(path)

    def get_values(self):
        server_id = self.server_combo.get_active_id()
        server_guid = server_id if server_id != '__new__' else ''
        srv = find_server_by_guid(self.config, server_guid) if server_guid else None
        return {
            'server_name': self.name_entry.get_text().strip() or (srv['name'] if srv else ''),
            'server_guid': server_guid,
            'protocol': self.proto_combo.get_active_id(),
            'host': self.host_entry.get_text().strip(),
            'port': int(self.port_entry.get_value()),
            'username': self.user_entry.get_text().strip(),
            'password': self.pass_entry.get_text(),
            'ssh_key_path': self.key_entry.get_text().strip(),
            'group': self.group_entry.get_text().strip(),
            'home_directory': self.home_entry.get_text().strip(),
            'max_upload_size_mb': int(self.size_entry.get_value()),
            'remember': self.remember_check.get_active(),
        }
