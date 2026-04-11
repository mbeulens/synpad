#!/usr/bin/env python3
"""SynPad - A lightweight PHP IDE with FTP/SFTP integration for Linux."""

APP_VERSION = "1.8.11"

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GtkSource, Gdk, GLib, Pango

import ftplib
import json
import os
import re
import stat
import tempfile
import uuid
import threading
from pathlib import Path

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


# --- Configuration -----------------------------------------------------------

CONFIG_DIR = os.path.join(str(Path.home()), '.config', 'synpad')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
SESSION_FILE = os.path.join(CONFIG_DIR, 'session.json')

DEFAULT_CONFIG = {
    'host': '',
    'port': 22,
    'username': '',
    'password': '',
    'max_upload_size_mb': 5,
    'last_directory': '/',
    'protocol': 'sftp',       # 'ftp' or 'sftp'
    'ssh_key_path': '',        # optional path to private key
    'home_directory': '',      # starting directory on connect (blank = /)
    'servers': [],             # saved server profiles
    'last_server': '',         # GUID of last used server
    'pane_order': ['symbols', 'editor', 'files'],  # left to right
    'dark_theme': True,        # editor color scheme
    'color_scheme': 'oblivion', # GtkSourceView style scheme id
    'custom_colors': {},        # active custom overrides: style_id -> {fg, bg, bold, italic}
    'saved_color_schemes': {},  # name -> {base: scheme_id, colors: {style_id -> props}}
    'active_custom_scheme': '', # name of currently active saved custom scheme
}


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        # Migrate: add GUIDs to any server profiles that don't have one
        migrated = False
        for srv in cfg.get('servers', []):
            if 'guid' not in srv:
                srv['guid'] = str(uuid.uuid4())
                migrated = True
        if migrated:
            save_config(cfg)
        return cfg
    return dict(DEFAULT_CONFIG)


def find_server_by_guid(cfg, guid):
    """Find a server profile by its GUID. Returns the dict or None."""
    for srv in cfg.get('servers', []):
        if srv.get('guid') == guid:
            return srv
    return None


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)


# --- FTP Manager -------------------------------------------------------------

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

class ConnectDialog(Gtk.Dialog):
    """Dialog for entering FTP/SFTP connection details."""

    def __init__(self, parent, config):
        super().__init__(
            title="Connect to Server",
            transient_for=parent,
            modal=True,
        )
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_CONNECT, Gtk.ResponseType.OK,
        )
        self.set_default_size(450, -1)
        self.set_default_response(Gtk.ResponseType.OK)
        self.config = config
        self._loading_server = False  # prevent save-trigger during load

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        box.pack_start(grid, True, True, 0)

        row = 0

        # --- Saved Servers selector ---
        grid.attach(Gtk.Label(label="Server:", halign=Gtk.Align.END), 0, row, 1, 1)
        server_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.server_combo = Gtk.ComboBoxText()
        self.server_combo.append('__new__', '(New connection)')
        for srv in config.get('servers', []):
            self.server_combo.append(srv['guid'], srv['name'])
        server_box.pack_start(self.server_combo, True, True, 0)

        self.btn_save_server = Gtk.Button(label="Save")
        self.btn_save_server.set_tooltip_text("Save current settings as a server profile")
        self.btn_save_server.connect('clicked', self._on_save_server)
        server_box.pack_start(self.btn_save_server, False, False, 0)

        self.btn_rename_server = Gtk.Button(label="Rename")
        self.btn_rename_server.set_tooltip_text("Rename selected server profile")
        self.btn_rename_server.connect('clicked', self._on_rename_server)
        server_box.pack_start(self.btn_rename_server, False, False, 0)

        self.btn_delete_server = Gtk.Button(label="Delete")
        self.btn_delete_server.set_tooltip_text("Delete selected server profile")
        self.btn_delete_server.connect('clicked', self._on_delete_server)
        server_box.pack_start(self.btn_delete_server, False, False, 0)

        grid.attach(server_box, 1, row, 1, 1)
        row += 1

        # Separator
        grid.attach(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), 0, row, 2, 1)
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
        key_box.pack_start(self.key_entry, True, True, 0)
        self.key_browse_btn = Gtk.Button(label="Browse...")
        self.key_browse_btn.connect('clicked', self._on_browse_key)
        key_box.pack_start(self.key_browse_btn, False, False, 0)
        grid.attach(key_box, 1, row, 1, 1)
        self.key_box_widget = key_box
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
        last = config.get('last_server', '')
        if last and self.server_combo.set_active_id(last) is None:
            self.server_combo.set_active_id('__new__')
        else:
            self.server_combo.set_active_id(last or '__new__')

        self._update_sftp_fields()
        self._update_delete_btn()
        self.show_all()

    def _on_server_changed(self, combo):
        """Load fields from selected server profile."""
        server_id = combo.get_active_id()
        self._update_delete_btn()
        if server_id == '__new__' or server_id is None:
            return
        srv = find_server_by_guid(self.config, server_id)
        if srv:
            self._loading_server = True
            self.proto_combo.set_active_id(srv.get('protocol', 'sftp'))
            self.host_entry.set_text(srv.get('host', ''))
            self.port_entry.set_value(srv.get('port', 22))
            self.user_entry.set_text(srv.get('username', ''))
            self.pass_entry.set_text(srv.get('password', ''))
            self.key_entry.set_text(srv.get('ssh_key_path', ''))
            self.home_entry.set_text(srv.get('home_directory', ''))
            self.size_entry.set_value(srv.get('max_upload_size_mb', 5))
            self.remember_check.set_active(True)
            self._update_sftp_fields()
            self._loading_server = False

    def _on_save_server(self, _btn):
        """Save current fields as a named server profile."""
        current_id = self.server_combo.get_active_id()
        # Suggest the current server name or host as default
        default_name = ''
        if current_id and current_id != '__new__':
            srv = find_server_by_guid(self.config, current_id)
            if srv:
                default_name = srv['name']
        elif self.host_entry.get_text().strip():
            default_name = self.host_entry.get_text().strip()

        dlg = Gtk.Dialog(
            title="Save Server",
            transient_for=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        dlg.set_default_response(Gtk.ResponseType.OK)
        content = dlg.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.add(Gtk.Label(label="Server name:", halign=Gtk.Align.START))
        name_entry = Gtk.Entry(text=default_name)
        name_entry.set_activates_default(True)
        content.add(name_entry)
        dlg.show_all()

        resp = dlg.run()
        name = name_entry.get_text().strip()
        dlg.destroy()

        if resp != Gtk.ResponseType.OK or not name:
            return

        # If editing an existing server, update it; otherwise create new
        existing_guid = None
        if current_id and current_id != '__new__':
            existing_guid = current_id

        profile = {
            'guid': existing_guid or str(uuid.uuid4()),
            'name': name,
            'protocol': self.proto_combo.get_active_id(),
            'host': self.host_entry.get_text().strip(),
            'port': int(self.port_entry.get_value()),
            'username': self.user_entry.get_text().strip(),
            'password': self.pass_entry.get_text(),
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
            # Rebuild combo to update the display name
            self._rebuild_server_combo()
        else:
            servers.append(profile)
            self.server_combo.append(profile['guid'], name)

        self.config['servers'] = servers
        save_config(self.config)
        self.server_combo.set_active_id(profile['guid'])

    def _on_delete_server(self, _btn):
        """Delete the currently selected server profile."""
        server_id = self.server_combo.get_active_id()
        if server_id == '__new__' or server_id is None:
            return

        srv = find_server_by_guid(self.config, server_id)
        display_name = srv['name'] if srv else server_id

        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Delete Server",
        )
        dlg.format_secondary_text(f"Delete server profile '{display_name}'?")
        resp = dlg.run()
        dlg.destroy()

        if resp != Gtk.ResponseType.YES:
            return

        servers = self.config.get('servers', [])
        self.config['servers'] = [s for s in servers if s.get('guid') != server_id]
        save_config(self.config)
        self._rebuild_server_combo()
        self.server_combo.set_active_id('__new__')

    def _on_rename_server(self, _btn):
        """Rename the currently selected server profile."""
        server_id = self.server_combo.get_active_id()
        if server_id == '__new__' or server_id is None:
            return

        srv = find_server_by_guid(self.config, server_id)
        if not srv:
            return

        dlg = Gtk.Dialog(
            title="Rename Server",
            transient_for=self,
            modal=True,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK,
        )
        dlg.set_default_response(Gtk.ResponseType.OK)
        content = dlg.get_content_area()
        content.set_spacing(8)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.add(Gtk.Label(label="New name:", halign=Gtk.Align.START))
        name_entry = Gtk.Entry(text=srv['name'])
        name_entry.set_activates_default(True)
        content.add(name_entry)
        dlg.show_all()

        resp = dlg.run()
        new_name = name_entry.get_text().strip()
        dlg.destroy()

        if resp != Gtk.ResponseType.OK or not new_name:
            return

        srv['name'] = new_name
        save_config(self.config)
        self._rebuild_server_combo()
        self.server_combo.set_active_id(server_id)

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
        self.btn_rename_server.set_sensitive(is_saved
        )

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
        dlg = Gtk.FileChooserDialog(
            title="Select SSH Private Key",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        # Start in ~/.ssh
        ssh_dir = os.path.join(str(Path.home()), '.ssh')
        if os.path.isdir(ssh_dir):
            dlg.set_current_folder(ssh_dir)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            self.key_entry.set_text(dlg.get_filename())
        dlg.destroy()

    def get_values(self):
        server_id = self.server_combo.get_active_id()
        server_guid = server_id if server_id != '__new__' else ''
        srv = find_server_by_guid(self.config, server_guid) if server_guid else None
        return {
            'server_name': srv['name'] if srv else '',
            'server_guid': server_guid,
            'protocol': self.proto_combo.get_active_id(),
            'host': self.host_entry.get_text().strip(),
            'port': int(self.port_entry.get_value()),
            'username': self.user_entry.get_text().strip(),
            'password': self.pass_entry.get_text(),
            'ssh_key_path': self.key_entry.get_text().strip(),
            'home_directory': self.home_entry.get_text().strip(),
            'max_upload_size_mb': int(self.size_entry.get_value()),
            'remember': self.remember_check.get_active(),
        }


# --- Symbol Parser -----------------------------------------------------------

# Patterns: (icon, regex) — regex must have named group 'name' and match at line start
SYMBOL_PATTERNS = {
    'php': [
        ('method',  re.compile(
            r'^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*'
            r'function\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*(?:abstract\s+)?class\s+(?P<name>\w+)', re.MULTILINE)),
        ('iface',   re.compile(
            r'^\s*interface\s+(?P<name>\w+)', re.MULTILINE)),
    ],
    'js': [
        ('func',    re.compile(
            r'^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('method',  re.compile(
            r'^\s*(?:(?:static|async|get|set)\s+)*(?P<name>(?!if|else|for|while|switch|catch|return|throw|typeof|instanceof|new|delete|void|do)\w+)\s*\([^)]*\)\s*\{', re.MULTILINE)),
        ('arrow',   re.compile(
            r'^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?'
            r'(?:\([^)]*\)|[\w]+)\s*=>', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*(?:export\s+)?class\s+(?P<name>\w+)', re.MULTILINE)),
    ],
    'ts': None,  # will copy from js
    'py': [
        ('func',    re.compile(
            r'^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*class\s+(?P<name>\w+)', re.MULTILINE)),
    ],
}
SYMBOL_PATTERNS['ts'] = SYMBOL_PATTERNS['js']

SYMBOL_ICONS = {
    'func': 'media-playback-start-symbolic',
    'method': 'media-playback-start-symbolic',
    'arrow': 'go-next-symbolic',
    'class': 'dialog-information-symbolic',
    'iface': 'dialog-question-symbolic',
}

# Extensions that support symbol parsing
SYMBOL_EXTENSIONS = {'php', 'js', 'jsx', 'ts', 'tsx', 'py'}


def parse_symbols(content, ext):
    """Return list of (kind, name, line_number, char_offset) from source content."""
    lang = ext
    if ext in ('jsx',):
        lang = 'js'
    elif ext in ('tsx',):
        lang = 'ts'
    patterns = SYMBOL_PATTERNS.get(lang)
    if not patterns:
        return []

    symbols = []
    seen = set()  # (name, offset) to deduplicate across patterns
    for kind, pattern in patterns:
        for m in pattern.finditer(content):
            name = m.group('name')
            offset = m.start('name')
            line = content[:offset].count('\n') + 1
            key = (name, offset)
            if key not in seen:
                seen.add(key)
                symbols.append((kind, name, line, offset))
    symbols.sort(key=lambda s: s[3])
    return symbols


# --- Code Completion ---------------------------------------------------------

from gi.repository import GObject

# Completions: {name: signature_or_None}
# None = keyword (no hint), string = function signature hint
PHP_COMPLETIONS = {
    # Keywords (no hints)
    'abstract': None, 'and': None, 'array': None, 'as': None, 'break': None,
    'callable': None, 'case': None, 'catch': None, 'class': None, 'clone': None,
    'const': None, 'continue': None, 'declare': None, 'default': None,
    'do': None, 'echo': None, 'else': None, 'elseif': None, 'empty': None,
    'extends': None, 'final': None, 'finally': None, 'fn': None, 'for': None,
    'foreach': None, 'function': None, 'global': None, 'if': None,
    'implements': None, 'include': None, 'include_once': None,
    'instanceof': None, 'interface': None, 'isset': None, 'list': None,
    'match': None, 'namespace': None, 'new': None, 'null': None, 'or': None,
    'print': None, 'private': None, 'protected': None, 'public': None,
    'readonly': None, 'require': None, 'require_once': None, 'return': None,
    'static': None, 'switch': None, 'throw': None, 'trait': None, 'try': None,
    'unset': None, 'use': None, 'var': None, 'while': None, 'yield': None,
    'true': None, 'false': None, 'self': None, 'parent': None,
    # Functions with signatures
    'array_key_exists': '(mixed $key, array $array): bool',
    'array_keys': '(array $array, mixed $filter_value?, bool $strict?): array',
    'array_map': '(callable $callback, array $array, array ...$arrays): array',
    'array_merge': '(array ...$arrays): array',
    'array_filter': '(array $array, callable $callback?, int $mode?): array',
    'array_pop': '(array &$array): mixed',
    'array_push': '(array &$array, mixed ...$values): int',
    'array_shift': '(array &$array): mixed',
    'array_slice': '(array $array, int $offset, int $length?, bool $preserve_keys?): array',
    'array_splice': '(array &$array, int $offset, int $length?, mixed $replacement?): array',
    'array_unique': '(array $array, int $flags?): array',
    'array_values': '(array $array): array',
    'array_combine': '(array $keys, array $values): array',
    'array_chunk': '(array $array, int $length, bool $preserve_keys?): array',
    'array_column': '(array $array, int|string $column_key, int|string $index_key?): array',
    'array_count_values': '(array $array): array',
    'array_diff': '(array $array, array ...$arrays): array',
    'array_intersect': '(array $array, array ...$arrays): array',
    'array_flip': '(array $array): array',
    'array_reverse': '(array $array, bool $preserve_keys?): array',
    'array_search': '(mixed $needle, array $haystack, bool $strict?): int|string|false',
    'array_sum': '(array $array): int|float',
    'array_rand': '(array $array, int $num?): int|string|array',
    'count': '(array|Countable $value, int $mode?): int',
    'in_array': '(mixed $needle, array $haystack, bool $strict?): bool',
    'sort': '(array &$array, int $flags?): bool',
    'usort': '(array &$array, callable $callback): bool',
    'ksort': '(array &$array, int $flags?): bool',
    'implode': '(string $separator, array $array): string',
    'explode': '(string $separator, string $string, int $limit?): array',
    'compact': '(string|array ...$var_names): array',
    'extract': '(array &$array, int $flags?, string $prefix?): int',
    'range': '(mixed $start, mixed $end, int|float $step?): array',
    'str_contains': '(string $haystack, string $needle): bool',
    'str_replace': '(mixed $search, mixed $replace, mixed $subject, int &$count?): mixed',
    'str_starts_with': '(string $haystack, string $needle): bool',
    'str_ends_with': '(string $haystack, string $needle): bool',
    'strlen': '(string $string): int',
    'strpos': '(string $haystack, string $needle, int $offset?): int|false',
    'strtolower': '(string $string): string',
    'strtoupper': '(string $string): string',
    'substr': '(string $string, int $offset, int $length?): string',
    'trim': '(string $string, string $characters?): string',
    'ltrim': '(string $string, string $characters?): string',
    'rtrim': '(string $string, string $characters?): string',
    'sprintf': '(string $format, mixed ...$values): string',
    'printf': '(string $format, mixed ...$values): int',
    'number_format': '(float $num, int $decimals?, string $dec_point?, string $thousands_sep?): string',
    'preg_match': '(string $pattern, string $subject, array &$matches?, int $flags?, int $offset?): int|false',
    'preg_replace': '(mixed $pattern, mixed $replacement, mixed $subject, int $limit?, int &$count?): mixed',
    'preg_match_all': '(string $pattern, string $subject, array &$matches?, int $flags?, int $offset?): int|false',
    'preg_split': '(string $pattern, string $subject, int $limit?, int $flags?): array|false',
    'file_get_contents': '(string $filename, bool $use_include_path?, resource $context?, int $offset?, int $length?): string|false',
    'file_put_contents': '(string $filename, mixed $data, int $flags?, resource $context?): int|false',
    'file_exists': '(string $filename): bool',
    'fopen': '(string $filename, string $mode, bool $use_include_path?, resource $context?): resource|false',
    'fclose': '(resource $stream): bool',
    'fread': '(resource $stream, int $length): string|false',
    'fwrite': '(resource $stream, string $data, int $length?): int|false',
    'fgets': '(resource $stream, int $length?): string|false',
    'is_file': '(string $filename): bool',
    'is_dir': '(string $filename): bool',
    'mkdir': '(string $directory, int $permissions?, bool $recursive?, resource $context?): bool',
    'rmdir': '(string $directory, resource $context?): bool',
    'unlink': '(string $filename, resource $context?): bool',
    'rename': '(string $from, string $to, resource $context?): bool',
    'copy': '(string $from, string $to, resource $context?): bool',
    'realpath': '(string $path): string|false',
    'dirname': '(string $path, int $levels?): string',
    'basename': '(string $path, string $suffix?): string',
    'json_encode': '(mixed $value, int $flags?, int $depth?): string|false',
    'json_decode': '(string $json, bool $assoc?, int $depth?, int $flags?): mixed',
    'var_dump': '(mixed ...$values): void',
    'print_r': '(mixed $value, bool $return?): string|bool',
    'var_export': '(mixed $value, bool $return?): string|null',
    'intval': '(mixed $value, int $base?): int',
    'floatval': '(mixed $value): float',
    'strval': '(mixed $value): string',
    'is_array': '(mixed $value): bool',
    'is_string': '(mixed $value): bool',
    'is_int': '(mixed $value): bool',
    'is_null': '(mixed $value): bool',
    'is_numeric': '(mixed $value): bool',
    'is_bool': '(mixed $value): bool',
    'is_object': '(mixed $value): bool',
    'date': '(string $format, int $timestamp?): string',
    'time': '(): int',
    'strtotime': '(string $datetime, int $baseTimestamp?): int|false',
    'mktime': '(int $hour, int $minute?, int $second?, int $month?, int $day?, int $year?): int|false',
    'header': '(string $header, bool $replace?, int $response_code?): void',
    'setcookie': '(string $name, string $value?, int $expires?, string $path?, string $domain?, bool $secure?, bool $httponly?): bool',
    'session_start': '(array $options?): bool',
    'session_destroy': '(): bool',
    'htmlspecialchars': '(string $string, int $flags?, string $encoding?, bool $double_encode?): string',
    'htmlentities': '(string $string, int $flags?, string $encoding?, bool $double_encode?): string',
    'urlencode': '(string $string): string',
    'urldecode': '(string $string): string',
    'base64_encode': '(string $string): string',
    'base64_decode': '(string $string, bool $strict?): string|false',
    'md5': '(string $string, bool $binary?): string',
    'sha1': '(string $string, bool $binary?): string',
    'password_hash': '(string $password, string|int $algo, array $options?): string',
    'password_verify': '(string $password, string $hash): bool',
    'rand': '(int $min?, int $max?): int',
    'mt_rand': '(int $min?, int $max?): int',
    'min': '(mixed ...$values): mixed',
    'max': '(mixed ...$values): mixed',
    'abs': '(int|float $num): int|float',
    'ceil': '(int|float $num): float',
    'floor': '(int|float $num): float',
    'round': '(int|float $num, int $precision?, int $mode?): float',
    'pow': '(mixed $base, mixed $exp): int|float',
    'sqrt': '(float $num): float',
    'class_exists': '(string $class, bool $autoload?): bool',
    'method_exists': '(object|string $object_or_class, string $method): bool',
    'property_exists': '(object|string $object_or_class, string $property): bool',
    'mysqli_connect': '(string $host?, string $username?, string $password?, string $database?, int $port?, string $socket?): mysqli|false',
    'mysqli_query': '(mysqli $mysql, string $query, int $result_mode?): mysqli_result|bool',
    'PDO': None, 'PDOStatement': None, 'Exception': None,
    'TypeError': None, 'RuntimeException': None,
    'stdClass': None, 'ArrayObject': None, 'DateTime': None,
    'DateTimeImmutable': None,
}

JS_COMPLETIONS = {
    # Keywords (no hints)
    'abstract': None, 'arguments': None, 'async': None, 'await': None,
    'break': None, 'case': None, 'class': None, 'const': None,
    'continue': None, 'debugger': None, 'default': None, 'delete': None,
    'do': None, 'else': None, 'enum': None, 'export': None, 'extends': None,
    'false': None, 'finally': None, 'for': None, 'from': None,
    'function': None, 'get': None, 'if': None, 'implements': None,
    'import': None, 'in': None, 'instanceof': None, 'interface': None,
    'let': None, 'new': None, 'null': None, 'of': None, 'package': None,
    'private': None, 'protected': None, 'public': None, 'return': None,
    'set': None, 'static': None, 'super': None, 'switch': None,
    'this': None, 'throw': None, 'true': None, 'try': None, 'typeof': None,
    'undefined': None, 'var': None, 'void': None, 'while': None,
    'with': None, 'yield': None,
    # TypeScript extras
    'type': None, 'namespace': None, 'declare': None, 'module': None,
    'readonly': None, 'keyof': None, 'infer': None, 'never': None,
    'unknown': None, 'any': None, 'string': None, 'number': None,
    'boolean': None, 'symbol': None, 'object': None,
    'Record': None, 'Partial': None, 'Required': None, 'Pick': None, 'Omit': None,
    # Functions/methods with signatures
    'console.log': '(...data: any[]): void',
    'console.error': '(...data: any[]): void',
    'console.warn': '(...data: any[]): void',
    'console.info': '(...data: any[]): void',
    'console.table': '(data: any, columns?: string[]): void',
    'console.dir': '(obj: any, options?: object): void',
    'document.getElementById': '(id: string): HTMLElement | null',
    'document.querySelector': '(selectors: string): Element | null',
    'document.querySelectorAll': '(selectors: string): NodeList',
    'document.createElement': '(tagName: string): HTMLElement',
    'document.addEventListener': '(type: string, listener: Function, options?: object): void',
    'window.addEventListener': '(type: string, listener: Function, options?: object): void',
    'JSON.parse': '(text: string, reviver?: Function): any',
    'JSON.stringify': '(value: any, replacer?: Function, space?: number): string',
    'Math.abs': '(x: number): number',
    'Math.ceil': '(x: number): number',
    'Math.floor': '(x: number): number',
    'Math.round': '(x: number): number',
    'Math.max': '(...values: number[]): number',
    'Math.min': '(...values: number[]): number',
    'Math.random': '(): number',
    'Math.pow': '(base: number, exponent: number): number',
    'Math.sqrt': '(x: number): number',
    'Object.keys': '(obj: object): string[]',
    'Object.values': '(obj: object): any[]',
    'Object.entries': '(obj: object): [string, any][]',
    'Object.assign': '(target: object, ...sources: object[]): object',
    'Object.freeze': '(obj: T): Readonly<T>',
    'Object.defineProperty': '(obj: object, prop: string, descriptor: object): object',
    'Array.isArray': '(value: any): boolean',
    'Array.from': '(arrayLike: Iterable, mapFn?: Function): any[]',
    'Array.of': '(...items: any[]): any[]',
    'Promise': None, 'Promise.all': '(values: Promise[]): Promise<any[]>',
    'Promise.resolve': '(value?: any): Promise',
    'Promise.reject': '(reason?: any): Promise',
    'setTimeout': '(callback: Function, delay?: number, ...args: any[]): number',
    'setInterval': '(callback: Function, delay?: number, ...args: any[]): number',
    'clearTimeout': '(id: number): void',
    'clearInterval': '(id: number): void',
    'parseInt': '(string: string, radix?: number): number',
    'parseFloat': '(string: string): number',
    'isNaN': '(value: any): boolean',
    'isFinite': '(value: any): boolean',
    'encodeURIComponent': '(uri: string): string',
    'decodeURIComponent': '(uri: string): string',
    'fetch': '(input: string | Request, init?: RequestInit): Promise<Response>',
    'Map': None, 'Set': None, 'WeakMap': None, 'WeakSet': None,
    'Symbol': None, 'Proxy': None, 'Reflect': None,
    'RegExp': None, 'Date': None, 'Error': None, 'TypeError': None, 'RangeError': None,
    'Request': None, 'Response': None, 'Headers': None,
    'URL': None, 'URLSearchParams': None,
    # Array/String methods
    'addEventListener': '(type: string, listener: Function, options?: object): void',
    'appendChild': '(node: Node): Node',
    'charAt': '(index: number): string',
    'concat': '(...items: any[]): any[] | string',
    'endsWith': '(searchString: string, length?: number): boolean',
    'every': '(callback: (value, index, array) => boolean): boolean',
    'fill': '(value: any, start?: number, end?: number): any[]',
    'filter': '(callback: (value, index, array) => boolean): any[]',
    'find': '(callback: (value, index, array) => boolean): any | undefined',
    'findIndex': '(callback: (value, index, array) => boolean): number',
    'flat': '(depth?: number): any[]',
    'flatMap': '(callback: (value, index, array) => any): any[]',
    'forEach': '(callback: (value, index, array) => void): void',
    'includes': '(searchElement: any, fromIndex?: number): boolean',
    'indexOf': '(searchElement: any, fromIndex?: number): number',
    'join': '(separator?: string): string',
    'lastIndexOf': '(searchElement: any, fromIndex?: number): number',
    'map': '(callback: (value, index, array) => any): any[]',
    'match': '(regexp: RegExp | string): string[] | null',
    'matchAll': '(regexp: RegExp): IterableIterator<RegExpMatchArray>',
    'padEnd': '(targetLength: number, padString?: string): string',
    'padStart': '(targetLength: number, padString?: string): string',
    'pop': '(): any | undefined',
    'push': '(...items: any[]): number',
    'reduce': '(callback: (acc, value, index, array) => any, initialValue?: any): any',
    'repeat': '(count: number): string',
    'replace': '(search: string | RegExp, replacement: string | Function): string',
    'replaceAll': '(search: string | RegExp, replacement: string): string',
    'reverse': '(): any[]',
    'shift': '(): any | undefined',
    'slice': '(start?: number, end?: number): any[] | string',
    'some': '(callback: (value, index, array) => boolean): boolean',
    'sort': '(compareFn?: (a, b) => number): any[]',
    'splice': '(start: number, deleteCount?: number, ...items: any[]): any[]',
    'split': '(separator: string | RegExp, limit?: number): string[]',
    'startsWith': '(searchString: string, position?: number): boolean',
    'substring': '(start: number, end?: number): string',
    'then': '(onFulfilled?: Function, onRejected?: Function): Promise',
    'catch': '(onRejected?: Function): Promise',
    'toFixed': '(digits?: number): string',
    'toLowerCase': '(): string',
    'toUpperCase': '(): string',
    'trim': '(): string',
    'trimEnd': '(): string',
    'trimStart': '(): string',
    'unshift': '(...items: any[]): number',
    'length': None,
    # React
    'useState': '<T>(initialState: T | () => T): [T, (value: T) => void]',
    'useEffect': '(effect: () => void | (() => void), deps?: any[]): void',
    'useCallback': '<T>(callback: T, deps: any[]): T',
    'useMemo': '<T>(factory: () => T, deps: any[]): T',
    'useRef': '<T>(initialValue: T): { current: T }',
    'useContext': '<T>(context: Context<T>): T',
    'useReducer': '(reducer: Function, initialState: any): [state, dispatch]',
    'createElement': '(type: string, props?: object, ...children: any[]): ReactElement',
    'Component': None, 'Fragment': None, 'StrictMode': None,
    'module.exports': None, 'require': '(id: string): any', 'exports': None,
}

# Map file extensions to completion dicts
COMPLETION_LANGS = {
    'php': PHP_COMPLETIONS,
    'js': JS_COMPLETIONS,
    'jsx': JS_COMPLETIONS,
    'ts': JS_COMPLETIONS,
    'tsx': JS_COMPLETIONS,
}


class SynPadCompletionProvider(GObject.Object, GtkSource.CompletionProvider):
    """Provides keyword/function completion for PHP and JS/TS with signatures."""

    def __init__(self, completions_dict):
        super().__init__()
        self._items = []  # list of (word, proposal)
        for name in sorted(completions_dict.keys()):
            sig = completions_dict[name]
            if sig:
                label = f"{name}  {sig}"
                info = f"{name}{sig}"
            else:
                label = name
                info = name
            # Escape markup characters in label and info
            label_safe = label.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            info_safe = info.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            proposal = GtkSource.CompletionItem.new(label_safe, name, None, info_safe)
            self._items.append((name, proposal))

    def do_get_name(self):
        return "SynPad"

    def do_get_priority(self):
        return 1

    def do_match(self, context):
        return True

    def do_populate(self, context):
        # Get the word being typed
        end_iter = context.get_iter()
        if isinstance(end_iter, tuple):
            _, end_iter = end_iter
        start_iter = end_iter.copy()

        # Walk back to find the start of the current word (including dots for JS)
        while start_iter.backward_char():
            ch = start_iter.get_char()
            if not (ch.isalnum() or ch == '_' or ch == '.' or ch == '$'):
                start_iter.forward_char()
                break

        prefix = start_iter.get_buffer().get_text(start_iter, end_iter, False).lower()

        if len(prefix) < 2:
            context.add_proposals(self, [], True)
            return

        matches = [prop for word, prop in self._items
                   if word.lower().startswith(prefix)]
        context.add_proposals(self, matches[:30], True)

    def do_get_activation(self):
        return GtkSource.CompletionActivation.USER_REQUESTED | \
               GtkSource.CompletionActivation.INTERACTIVE

    def do_get_interactive_delay(self):
        return 50

    def do_activate_proposal(self, proposal, text_iter):
        # Find the start of the current word
        start = text_iter.copy()
        while start.backward_char():
            ch = start.get_char()
            if not (ch.isalnum() or ch == '_' or ch == '.' or ch == '$'):
                start.forward_char()
                break

        buf = text_iter.get_buffer()
        buf.begin_user_action()
        buf.delete(start, text_iter)
        buf.insert(start, proposal.get_text())
        buf.end_user_action()
        return True


class DocumentWordProvider(GObject.Object, GtkSource.CompletionProvider):
    """Provides completion from words already in the document."""

    def __init__(self):
        super().__init__()

    def do_get_name(self):
        return "Document"

    def do_get_priority(self):
        return 0

    def do_match(self, context):
        return True

    def do_populate(self, context):
        end_iter = context.get_iter()
        if isinstance(end_iter, tuple):
            _, end_iter = end_iter
        start_iter = end_iter.copy()

        while start_iter.backward_char():
            ch = start_iter.get_char()
            if not (ch.isalnum() or ch == '_' or ch == '$'):
                start_iter.forward_char()
                break

        buf = start_iter.get_buffer()
        prefix = buf.get_text(start_iter, end_iter, False)

        if len(prefix) < 3:
            context.add_proposals(self, [], True)
            return

        # Gather all words from the buffer
        full_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        words = set(re.findall(r'[A-Za-z_$]\w{2,}', full_text))
        words.discard(prefix)

        prefix_lower = prefix.lower()
        matches = sorted(w for w in words if w.lower().startswith(prefix_lower))

        proposals = [GtkSource.CompletionItem.new(w, w, None, None)
                     for w in matches[:20]]
        context.add_proposals(self, proposals, True)

    def do_get_activation(self):
        return GtkSource.CompletionActivation.USER_REQUESTED | \
               GtkSource.CompletionActivation.INTERACTIVE

    def do_get_interactive_delay(self):
        return 80

    def do_activate_proposal(self, proposal, text_iter):
        start = text_iter.copy()
        while start.backward_char():
            ch = start.get_char()
            if not (ch.isalnum() or ch == '_' or ch == '$'):
                start.forward_char()
                break

        buf = text_iter.get_buffer()
        buf.begin_user_action()
        buf.delete(start, text_iter)
        buf.insert(start, proposal.get_text())
        buf.end_user_action()
        return True


# --- Open Tab Tracker --------------------------------------------------------

class OpenTab:
    """Represents one open file tab."""

    def __init__(self, remote_path, local_path, source_view, buffer,
                 is_local=False, server_guid=''):
        self.remote_path = remote_path
        self.local_path = local_path
        self.source_view = source_view
        self.buffer = buffer
        self.modified = False
        self.is_local = is_local  # True for local files, False for remote
        self.server_guid = server_guid  # GUID of the server this file belongs to


# --- Main Window -------------------------------------------------------------

class SynPadWindow(Gtk.Window):
    """Main application window."""

    def __init__(self):
        super().__init__(title="SynPad - PHP IDE")
        self.set_default_size(1200, 750)
        self.config = load_config()
        self.ftp_mgr = None  # set on connect (FTPManager or SFTPManager)
        self.current_server_guid = ''  # GUID of currently connected server
        self._pending_upload = None   # (tab, page_num, max_mb) for auto-switch
        self._pending_tree_reload = None  # vals dict for tree reload after upload
        self.tabs = {}  # page_num -> OpenTab
        self.current_remote_dir = '/'
        self.tmp_dir = tempfile.mkdtemp(prefix='synpad_')

        self._build_ui()
        self._connect_signals()
        self._apply_css()
        self._apply_gtk_theme()
        self._restore_session()

    # -- UI Construction ------------------------------------------------------

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        # Toolbar
        toolbar = Gtk.HeaderBar()
        toolbar.set_show_close_button(True)
        toolbar.set_title(f"SynPad v{APP_VERSION}")
        toolbar.set_subtitle("Disconnected")
        self.set_titlebar(toolbar)
        self.header = toolbar

        # Hamburger menu button
        menu_btn = Gtk.MenuButton()
        menu_btn.set_image(Gtk.Image.new_from_icon_name(
            'open-menu-symbolic', Gtk.IconSize.BUTTON))
        menu_btn.set_relief(Gtk.ReliefStyle.NONE)

        menu = Gtk.Menu()

        item_new_local = Gtk.MenuItem(label="New Local File  Ctrl+N")
        item_new_local.connect('activate', lambda _: self._on_new_local_file())
        menu.append(item_new_local)

        item_open_local = Gtk.MenuItem(label="Open Local File  Ctrl+O")
        item_open_local.connect('activate', lambda _: self._on_open_local_file())
        menu.append(item_open_local)

        self.item_save = Gtk.MenuItem(label="Save  Ctrl+S")
        self.item_save.set_sensitive(False)
        self.item_save.connect('activate', lambda _: self._on_save(None))
        menu.append(self.item_save)

        menu.append(Gtk.SeparatorMenuItem())

        item_find = Gtk.MenuItem(label="Find  Ctrl+F")
        item_find.connect('activate', lambda _: self._show_search(show_replace=False))
        menu.append(item_find)

        item_replace = Gtk.MenuItem(label="Find & Replace  Ctrl+R")
        item_replace.connect('activate', lambda _: self._show_search(show_replace=True))
        menu.append(item_replace)

        item_goto = Gtk.MenuItem(label="Go to Line  Ctrl+G")
        item_goto.connect('activate', lambda _: self._on_goto_line())
        menu.append(item_goto)

        menu.append(Gtk.SeparatorMenuItem())

        item_json = Gtk.MenuItem(label="Pretty Print JSON")
        item_json.connect('activate', lambda _: self._on_pretty_print_json())
        menu.append(item_json)

        item_xml = Gtk.MenuItem(label="Pretty Print XML")
        item_xml.connect('activate', lambda _: self._on_pretty_print_xml())
        menu.append(item_xml)

        menu.append(Gtk.SeparatorMenuItem())

        item_scheme = Gtk.MenuItem(label="Color Scheme")
        item_scheme.connect('activate', self._on_pick_scheme)
        menu.append(item_scheme)

        item_custom_colors = Gtk.MenuItem(label="Custom Colors")
        item_custom_colors.connect('activate', self._on_custom_colors)
        menu.append(item_custom_colors)

        menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="Settings")
        item_settings.connect('activate', self._on_open_settings)
        menu.append(item_settings)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit  Ctrl+Q")
        item_quit.connect('activate', lambda _: self._on_quit(None))
        menu.append(item_quit)

        menu.show_all()
        menu_btn.set_popup(menu)
        toolbar.pack_start(menu_btn)


        self.btn_theme = Gtk.Button()
        self.btn_theme.set_relief(Gtk.ReliefStyle.NONE)
        self._update_theme_icon()
        self.btn_theme.set_tooltip_text("Toggle light/dark theme")
        toolbar.pack_end(self.btn_theme)

        self.btn_console = Gtk.Button()
        self.btn_console.set_image(Gtk.Image.new_from_icon_name(
            'utilities-terminal-symbolic', Gtk.IconSize.BUTTON))
        self.btn_console.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_console.set_tooltip_text("Toggle console")
        toolbar.pack_end(self.btn_console)

        # --- Build the three panes as independent widgets ---

        # 1) Symbol / function list pane
        self.symbol_pane = self._make_pane_wrapper('symbols', 'Functions')
        self.btn_refresh_symbols = Gtk.Button()
        self.btn_refresh_symbols.set_image(
            Gtk.Image.new_from_icon_name('view-refresh-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        self.btn_refresh_symbols.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_refresh_symbols.set_tooltip_text("Refresh symbol list")
        self.btn_refresh_symbols.connect('clicked', self._on_refresh_symbols)
        # Insert refresh button into the symbol header (before the right-arrow)
        sym_header = self.symbol_pane.get_children()[0]  # the header Box
        sym_header.pack_end(self.btn_refresh_symbols, False, False, 0)

        self.symbol_scroll = Gtk.ScrolledWindow()
        self.symbol_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.symbol_scroll.set_size_request(180, -1)

        # ListStore: icon_name, display_text, line_number, char_offset
        self.symbol_store = Gtk.ListStore(str, str, int, int)
        self.symbol_view = Gtk.TreeView(model=self.symbol_store)
        self.symbol_view.set_headers_visible(False)
        self.symbol_view.set_activate_on_single_click(True)

        sym_col = Gtk.TreeViewColumn("Symbol")
        sym_icon = Gtk.CellRendererPixbuf()
        sym_text = Gtk.CellRendererText()
        sym_col.pack_start(sym_icon, False)
        sym_col.pack_start(sym_text, True)
        sym_col.add_attribute(sym_icon, 'icon-name', 0)
        sym_col.add_attribute(sym_text, 'text', 1)
        self.symbol_view.append_column(sym_col)

        self.symbol_view.get_style_context().add_class('symbol-pane')
        self.symbol_view.connect('row-activated', self._on_symbol_activated)
        self.symbol_scroll.add(self.symbol_view)
        self.symbol_pane.pack_start(self.symbol_scroll, True, True, 0)

        # 2) Editor (notebook with tabs)
        self.editor_pane = self._make_pane_wrapper('editor', 'Editor')
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_size_request(300, -1)
        welcome = Gtk.Label(label="Connect to an FTP/SFTP server and open a file to start editing.")
        welcome.set_margin_top(40)
        self.notebook.append_page(welcome, Gtk.Label(label="Welcome"))
        self.editor_pane.pack_start(self.notebook, True, True, 0)

        # 3) File tree — with connection controls below header
        self.files_pane = self._make_pane_wrapper('files', 'Files')

        # Row 1: Connect, Disconnect, Refresh icons
        conn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        conn_row.set_margin_start(4)
        conn_row.set_margin_end(4)

        self.btn_connect = Gtk.Button()
        self.btn_connect.set_image(Gtk.Image.new_from_icon_name(
            'network-server-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        self.btn_connect.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_connect.set_tooltip_text("Connect to server")
        conn_row.pack_start(self.btn_connect, False, False, 0)

        self.btn_disconnect = Gtk.Button()
        self.btn_disconnect.set_image(Gtk.Image.new_from_icon_name(
            'network-offline-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        self.btn_disconnect.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_disconnect.set_tooltip_text("Disconnect")
        self.btn_disconnect.set_sensitive(False)
        conn_row.pack_start(self.btn_disconnect, False, False, 0)

        self.btn_refresh = Gtk.Button()
        self.btn_refresh.set_image(Gtk.Image.new_from_icon_name(
            'view-refresh-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        self.btn_refresh.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_refresh.set_tooltip_text("Refresh file tree")
        self.btn_refresh.set_sensitive(False)
        conn_row.pack_start(self.btn_refresh, False, False, 0)

        self.scroll_tree = Gtk.ScrolledWindow()
        self.scroll_tree.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroll_tree.set_size_request(150, -1)

        self.tree_store = Gtk.TreeStore(str, str, str, bool, bool)
        self.tree_view = Gtk.TreeView(model=self.tree_store)
        self.tree_view.set_headers_visible(False)

        col = Gtk.TreeViewColumn("Files")
        icon_renderer = Gtk.CellRendererPixbuf()
        text_renderer = Gtk.CellRendererText()
        col.pack_start(icon_renderer, False)
        col.pack_start(text_renderer, True)
        col.add_attribute(icon_renderer, 'icon-name', 1)
        col.add_attribute(text_renderer, 'text', 0)
        self.tree_view.append_column(col)

        self.scroll_tree.add(self.tree_view)

        # Quick connect dropdown (top, right after header)
        server_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        server_row.set_margin_start(4)
        server_row.set_margin_end(4)
        server_row.set_margin_bottom(2)

        self.quick_combo = Gtk.ComboBoxText()
        self.quick_combo.set_tooltip_text("Quick connect to saved server")
        self._rebuild_quick_combo()
        server_row.pack_start(self.quick_combo, True, True, 0)

        self.files_pane.pack_start(server_row, False, False, 0)
        self.files_pane.pack_start(self.scroll_tree, True, True, 0)

        # Connect, Disconnect, Refresh icons (bottom)
        conn_row.set_margin_bottom(4)
        self.files_pane.pack_end(conn_row, False, False, 0)

        # Map pane IDs to widgets
        self._pane_widgets = {
            'symbols': self.symbol_pane,
            'editor':  self.editor_pane,
            'files':   self.files_pane,
        }

        # Two persistent Paneds: outer(child1, inner(child2, child3))
        self._inner_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned.pack2(self._inner_paned, resize=True, shrink=True)

        # Vertical paned: top = editor panes, bottom = console
        self._main_vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._main_vpaned.pack1(self._outer_paned, resize=True, shrink=True)

        # Console pane
        console_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        console_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        console_header.set_margin_start(6)
        console_header.set_margin_end(6)
        console_header.set_margin_top(2)
        console_header.set_margin_bottom(2)

        lbl = Gtk.Label()
        lbl.set_markup("<b>Console</b>")
        console_header.pack_start(lbl, False, False, 0)

        btn_clear_console = Gtk.Button()
        btn_clear_console.set_image(Gtk.Image.new_from_icon_name(
            'edit-clear-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_clear_console.set_relief(Gtk.ReliefStyle.NONE)
        btn_clear_console.set_tooltip_text("Clear console")
        btn_clear_console.connect('clicked', self._on_clear_console)
        console_header.pack_end(btn_clear_console, False, False, 0)

        console_box.pack_start(console_header, False, False, 0)
        console_box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self._console_scroll = Gtk.ScrolledWindow()
        self._console_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._console_buffer = Gtk.TextBuffer()
        self._console_view = Gtk.TextView(buffer=self._console_buffer)
        self._console_view.set_editable(False)
        self._console_view.set_cursor_visible(False)
        self._console_view.set_monospace(True)
        self._console_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._console_view.get_style_context().add_class('console-view')

        # Tag for timestamps
        self._console_buffer.create_tag('timestamp', foreground='#888888')
        self._console_buffer.create_tag('error', foreground='#ef2929')
        self._console_buffer.create_tag('success', foreground='#8ae234')

        self._console_scroll.add(self._console_view)
        console_box.pack_start(self._console_scroll, True, True, 0)

        self._console_pane = console_box
        self._main_vpaned.pack2(self._console_pane, resize=False, shrink=True)

        vbox.pack_start(self._main_vpaned, True, True, 0)

        # Console starts hidden
        self._console_visible = False
        self._console_pane.set_no_show_all(True)

        # Apply saved order
        self._apply_pane_layout()

        # Search window (created on demand)
        self._search_window = None

        # Status bar
        self.statusbar = Gtk.Statusbar()
        vbox.pack_end(self.statusbar, False, False, 0)
        self._set_status("Ready")

    def _apply_css(self):
        css = b"""
        .editor-view {
            font-family: "Source Code Pro", "DejaVu Sans Mono", "Consolas", monospace;
            font-size: 13px;
        }
        .symbol-pane {
            font-family: "Source Code Pro", "DejaVu Sans Mono", "Consolas", monospace;
            font-size: 12px;
        }
        .pane-header {
            background-color: @theme_bg_color;
            padding: 2px 0px;
        }
        .pane-header:hover {
            background-color: shade(@theme_bg_color, 1.1);
        }
        .console-view {
            font-family: "Source Code Pro", "DejaVu Sans Mono", "Consolas", monospace;
            font-size: 11px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _make_pane_wrapper(self, pane_id, title):
        """Create a VBox with a header containing arrow buttons to reorder."""
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper._pane_id = pane_id

        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        header_box.set_margin_start(4)
        header_box.set_margin_end(4)
        header_box.set_margin_top(2)
        header_box.set_margin_bottom(2)
        header_box.get_style_context().add_class('pane-header')

        # Move left button
        btn_left = Gtk.Button()
        btn_left.set_image(Gtk.Image.new_from_icon_name(
            'go-previous-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_left.set_relief(Gtk.ReliefStyle.NONE)
        btn_left.set_tooltip_text("Move pane left")
        btn_left.connect('clicked', self._on_move_pane, pane_id, -1)
        header_box.pack_start(btn_left, False, False, 0)

        lbl = Gtk.Label()
        lbl.set_markup(f"<b>{title}</b>")
        header_box.pack_start(lbl, True, False, 0)

        # Move right button
        btn_right = Gtk.Button()
        btn_right.set_image(Gtk.Image.new_from_icon_name(
            'go-next-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_right.set_relief(Gtk.ReliefStyle.NONE)
        btn_right.set_tooltip_text("Move pane right")
        btn_right.connect('clicked', self._on_move_pane, pane_id, +1)
        header_box.pack_end(btn_right, False, False, 0)

        wrapper.pack_start(header_box, False, False, 0)
        wrapper.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                           False, False, 0)
        return wrapper

    def _on_move_pane(self, _btn, pane_id, direction):
        """Move a pane left (-1) or right (+1)."""
        order = self.config.get('pane_order', ['symbols', 'editor', 'files'])
        idx = order.index(pane_id)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(order):
            return
        order[idx], order[new_idx] = order[new_idx], order[idx]
        self.config['pane_order'] = order
        save_config(self.config)
        # Defer reparenting to next idle cycle so GTK finishes the click
        GLib.idle_add(self._apply_pane_layout)

    def _apply_pane_layout(self):
        """Place panes into the two persistent Paneds based on config order."""
        order = self.config.get('pane_order', ['symbols', 'editor', 'files'])

        # Detach all panes from their current parents
        for pane_id in self._pane_widgets:
            w = self._pane_widgets[pane_id]
            parent = w.get_parent()
            if parent:
                parent.remove(w)

        left_w = self._pane_widgets[order[0]]
        mid_w = self._pane_widgets[order[1]]
        right_w = self._pane_widgets[order[2]]

        # outer_paned: pack1 = left, pack2 = inner_paned
        # inner_paned: pack1 = middle, pack2 = right
        self._outer_paned.pack1(left_w, resize=False, shrink=True)
        self._inner_paned.pack1(mid_w, resize=True, shrink=True)
        self._inner_paned.pack2(right_w, resize=False, shrink=True)

        # Set divider positions — give the editor the most space
        editor_pos = order.index('editor')
        if editor_pos == 0:
            self._outer_paned.set_position(700)
            self._inner_paned.set_position(600)
        elif editor_pos == 1:
            self._outer_paned.set_position(200)
            self._inner_paned.set_position(700)
        else:
            self._outer_paned.set_position(200)
            self._inner_paned.set_position(200)

        self._outer_paned.show_all()

    def _get_scheme(self):
        """Return the GtkSource scheme, applying custom color overrides."""
        custom = self.config.get('custom_colors', {})
        if custom:
            # Build a custom scheme XML and load it
            return self._build_custom_scheme()
        mgr = GtkSource.StyleSchemeManager.get_default()
        scheme_id = self.config.get('color_scheme', 'oblivion')
        return mgr.get_scheme(scheme_id) or mgr.get_scheme('classic')

    def _build_custom_scheme(self):
        """Generate a custom GtkSourceView scheme XML from config overrides."""
        base_id = self.config.get('color_scheme', 'oblivion')
        custom = self.config.get('custom_colors', {})

        # Start from the base scheme to get its background/text colors
        mgr = GtkSource.StyleSchemeManager.get_default()
        base = mgr.get_scheme(base_id)

        lines = ['<?xml version="1.0" encoding="UTF-8"?>']
        lines.append(f'<style-scheme id="synpad-custom" name="SynPad Custom" version="1.0">')
        lines.append(f'  <author>SynPad</author>')
        lines.append(f'  <description>Custom colors based on {base_id}</description>')

        # Copy base text/background if available
        if base:
            style = base.get_style('text')
            if style:
                fg = style.props.foreground if style.props.foreground_set else None
                bg = style.props.background if style.props.background_set else None
                parts = []
                if fg and self._is_valid_color(fg):
                    parts.append(f'foreground="{fg}"')
                if bg and self._is_valid_color(bg):
                    parts.append(f'background="{bg}"')
                if parts:
                    lines.append(f'  <style name="text" {" ".join(parts)}/>')

            for default_style in ['selection', 'cursor', 'current-line',
                                  'line-numbers', 'bracket-match',
                                  'search-match']:
                style = base.get_style(default_style)
                if style:
                    parts = self._style_to_attrs(style)
                    if parts:
                        lines.append(f'  <style name="{default_style}" {" ".join(parts)}/>')

        # Apply user overrides
        for style_id, props in custom.items():
            parts = []
            if props.get('fg'):
                parts.append(f'foreground="{props["fg"]}"')
            if props.get('bg'):
                parts.append(f'background="{props["bg"]}"')
            if props.get('bold'):
                parts.append('bold="true"')
            if props.get('italic'):
                parts.append('italic="true"')
            if parts:
                lines.append(f'  <style name="{style_id}" {" ".join(parts)}/>')

        # Ensure search-match is always defined
        has_search_match = any('search-match' in l for l in lines)
        if not has_search_match:
            lines.append('  <style name="search-match" foreground="#000000" background="#ffff00"/>')

        lines.append('</style-scheme>')

        # Write to config dir and load
        xml_path = os.path.join(CONFIG_DIR, 'synpad-custom.xml')
        with open(xml_path, 'w') as f:
            f.write('\n'.join(lines))

        # Ensure our config dir is in the search path
        search_paths = list(mgr.get_search_path())
        if CONFIG_DIR not in search_paths:
            search_paths.insert(0, CONFIG_DIR)
            mgr.set_search_path(search_paths)
        mgr.force_rescan()

        return mgr.get_scheme('synpad-custom')

    @staticmethod
    def _is_valid_color(val):
        """Check if a color string is a valid hex color for GtkSourceView XML."""
        if not val:
            return False
        rgba = Gdk.RGBA()
        return rgba.parse(val)

    def _style_to_attrs(self, style):
        """Convert a GtkSourceStyle to XML attribute strings."""
        parts = []
        if style.props.foreground_set and self._is_valid_color(style.props.foreground):
            parts.append(f'foreground="{style.props.foreground}"')
        if style.props.background_set and self._is_valid_color(style.props.background):
            parts.append(f'background="{style.props.background}"')
        if style.props.bold_set and style.props.bold:
            parts.append('bold="true"')
        if style.props.italic_set and style.props.italic:
            parts.append('italic="true"')
        return parts

    def _update_theme_icon(self):
        if self.config.get('dark_theme', True):
            self.btn_theme.set_image(Gtk.Image.new_from_icon_name(
                'weather-clear-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        else:
            self.btn_theme.set_image(Gtk.Image.new_from_icon_name(
                'weather-clear-night-symbolic', Gtk.IconSize.SMALL_TOOLBAR))

    def _on_open_settings(self, _item):
        """Open the settings dialog (same as connect dialog for now)."""
        dlg = ConnectDialog(self, self.config)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            vals = dlg.get_values()
            if vals.get('remember'):
                self.config['host'] = vals['host']
                self.config['port'] = vals['port']
                self.config['username'] = vals['username']
                self.config['password'] = vals['password']
                self.config['max_upload_size_mb'] = vals['max_upload_size_mb']
                self.config['protocol'] = vals.get('protocol', 'sftp')
                self.config['ssh_key_path'] = vals.get('ssh_key_path', '')
                self.config['home_directory'] = vals.get('home_directory', '')
                self.config['last_server'] = vals.get('server_guid', '')
                save_config(self.config)
        # Always rebuild quick connect — renames/saves/deletes may have happened
        self._rebuild_quick_combo()
        dlg.destroy()

    def _on_toggle_theme(self, _btn):
        dark = not self.config.get('dark_theme', True)
        self.config['dark_theme'] = dark
        # Switch to a matching base scheme
        if dark:
            self.config['color_scheme'] = 'oblivion'
        else:
            self.config['color_scheme'] = 'classic'
        save_config(self.config)
        self._update_theme_icon()
        self._apply_gtk_theme()
        self._apply_scheme_to_all()

    def _apply_scheme_to_all(self):
        """Apply the current color scheme to all open editor tabs."""
        scheme = self._get_scheme()
        if scheme:
            for tab in self.tabs.values():
                tab.buffer.set_style_scheme(scheme)

    def _apply_gtk_theme(self):
        """Set the GTK application-wide dark/light preference."""
        settings = Gtk.Settings.get_default()
        settings.set_property(
            'gtk-application-prefer-dark-theme',
            self.config.get('dark_theme', True),
        )

    # -- Session Save / Restore ------------------------------------------------

    def _save_session(self):
        """Save all open tabs to session file so they can be restored."""
        session_tabs = []
        for page_num in range(self.notebook.get_n_pages()):
            tab = self.tabs.get(page_num)
            if not tab:
                continue
            # Get current content from buffer
            start = tab.buffer.get_start_iter()
            end = tab.buffer.get_end_iter()
            content = tab.buffer.get_text(start, end, True)

            session_tabs.append({
                'remote_path': tab.remote_path,
                'local_path': tab.local_path,
                'is_local': tab.is_local,
                'modified': tab.modified,
                'content': content,
                'server_guid': tab.server_guid,
            })

        session = {
            'tabs': session_tabs,
            'active_tab': self.notebook.get_current_page(),
        }

        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(SESSION_FILE, 'w') as f:
                json.dump(session, f, indent=2)
        except Exception:
            pass

    def _restore_session(self):
        """Restore tabs from the previous session."""
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, 'r') as f:
                session = json.load(f)
        except Exception:
            return

        tabs = session.get('tabs', [])
        if not tabs:
            return

        for tab_data in tabs:
            remote_path = tab_data.get('remote_path', '')
            local_path = tab_data.get('local_path', '')
            is_local = tab_data.get('is_local', False)
            content = tab_data.get('content', '')
            was_modified = tab_data.get('modified', False)
            server_guid = tab_data.get('server_guid', '')

            if not remote_path:
                continue

            # For local files, re-read from disk if not modified
            if is_local and not was_modified and os.path.isfile(local_path):
                try:
                    with open(local_path, 'r', errors='replace') as f:
                        content = f.read()
                except Exception:
                    pass

            # For remote files, save content to temp file
            if not is_local:
                filename = os.path.basename(remote_path)
                local_path = os.path.join(
                    self.tmp_dir,
                    filename.replace('/', '_') + f'_{id(remote_path)}'
                )
                try:
                    with open(local_path, 'w') as f:
                        f.write(content)
                except Exception:
                    continue

            self._create_editor_tab(remote_path, local_path, content,
                                    is_local=is_local, server_guid=server_guid)

            # Mark as modified if it was unsaved
            if was_modified:
                page_num = self.notebook.get_n_pages() - 1
                tab = self.tabs.get(page_num)
                if tab:
                    tab.buffer.set_modified(True)

        # Restore active tab
        active = session.get('active_tab', 0)
        if active < self.notebook.get_n_pages():
            self.notebook.set_current_page(active)

        self.item_save.set_sensitive(bool(self.tabs))

    # -- Console ---------------------------------------------------------------

    def _on_toggle_console(self, *_args):
        """Toggle the console pane visibility."""
        self._console_visible = not self._console_visible
        if self._console_visible:
            self._console_pane.set_no_show_all(False)
            self._console_pane.show_all()
            self._console_pane.set_no_show_all(True)
            # Set a reasonable split — console gets ~200px
            alloc = self._main_vpaned.get_allocation()
            self._main_vpaned.set_position(alloc.height - 200)
        else:
            self._console_pane.set_visible(False)

    def _on_clear_console(self, *_args):
        """Clear the console output."""
        self._console_buffer.set_text('')

    def _console_log(self, message, tag=None):
        """Insert a timestamped message at the top of the console. Thread-safe via idle_add."""
        def _do_log():
            import datetime
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            start = self._console_buffer.get_start_iter()
            if tag:
                self._console_buffer.insert_with_tags_by_name(start, f"{message}\n", tag)
            else:
                self._console_buffer.insert(start, f"{message}\n")
            # Insert timestamp before the message we just added
            start = self._console_buffer.get_start_iter()
            self._console_buffer.insert_with_tags_by_name(start, f"[{ts}] ", 'timestamp')
            # Trim to 500 lines
            if self._console_buffer.get_line_count() > 500:
                trim_start = self._console_buffer.get_iter_at_line(500)
                trim_end = self._console_buffer.get_end_iter()
                self._console_buffer.delete(trim_start, trim_end)
            return False

        GLib.idle_add(_do_log)

    def _on_pick_scheme(self, _item):
        """Dialog to pick a GtkSourceView color scheme."""
        dlg = Gtk.Dialog(
            title="Color Scheme",
            transient_for=self,
            modal=True,
        )
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dlg.set_default_size(350, 400)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)

        box.add(Gtk.Label(label="Select a color scheme:", halign=Gtk.Align.START))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # ListStore: scheme_id, display_name, description
        store = Gtk.ListStore(str, str, str)
        mgr = GtkSource.StyleSchemeManager.get_default()
        current = self.config.get('color_scheme', 'oblivion')

        select_iter = None
        for sid in sorted(mgr.get_scheme_ids()):
            s = mgr.get_scheme(sid)
            it = store.append([sid, s.get_name(), s.get_description() or ''])
            if sid == current:
                select_iter = it

        tv = Gtk.TreeView(model=store)
        tv.set_headers_visible(False)
        col = Gtk.TreeViewColumn("Scheme")
        cell = Gtk.CellRendererText()
        col.pack_start(cell, True)
        col.add_attribute(cell, 'text', 1)
        tv.append_column(col)

        if select_iter:
            tv.get_selection().select_iter(select_iter)

        # Live preview on selection change
        def on_sel_changed(sel):
            model, it = sel.get_selected()
            if it:
                sid = model[it][0]
                scheme = mgr.get_scheme(sid)
                if scheme:
                    for tab in self.tabs.values():
                        tab.buffer.set_style_scheme(scheme)

        tv.get_selection().connect('changed', on_sel_changed)

        scroll.add(tv)
        box.pack_start(scroll, True, True, 0)
        dlg.show_all()

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            model, it = tv.get_selection().get_selected()
            if it:
                self.config['color_scheme'] = model[it][0]
                self.config['custom_colors'] = {}  # reset custom when changing base
                save_config(self.config)
                self._apply_scheme_to_all()
        else:
            # Revert preview
            self._apply_scheme_to_all()
        dlg.destroy()

    # -- Custom Colors constants --
    STYLE_ITEMS = [
        ('def:comment',         'Comments'),
        ('def:string',          'Strings'),
        ('def:keyword',         'Keywords'),
        ('def:type',            'Types'),
        ('def:identifier',      'Identifiers / Functions'),
        ('def:statement',       'Statements'),
        ('def:preprocessor',    'Preprocessor'),
        ('def:constant',        'Constants'),
        ('def:special-char',    'Special Characters'),
        ('def:floating-point',  'Numbers'),
        ('def:error',           'Errors'),
        ('def:warning',         'Warnings'),
        ('text',                'Editor Text'),
        ('current-line',        'Current Line'),
        ('line-numbers',        'Line Numbers'),
    ]

    def _on_custom_colors(self, _item):
        """Dialog to customize individual syntax element colors."""
        custom = dict(self.config.get('custom_colors', {}))

        dlg = Gtk.Dialog(
            title="Custom Colors",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(520, 560)

        box = dlg.get_content_area()
        box.set_spacing(4)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)

        # --- Load saved scheme row (top) ---
        load_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        load_row.pack_start(Gtk.Label(label="Load scheme:", halign=Gtk.Align.START), False, False, 0)

        scheme_combo = Gtk.ComboBoxText()
        scheme_combo.append('__none__', '(none)')
        for name in sorted(self.config.get('saved_color_schemes', {}).keys()):
            scheme_combo.append(name, name)
        active = self.config.get('active_custom_scheme', '')
        scheme_combo.set_active_id(active if active else '__none__')
        load_row.pack_start(scheme_combo, True, True, 0)

        btn_delete_scheme = Gtk.Button(label="Delete")
        btn_delete_scheme.set_tooltip_text("Delete selected scheme")
        load_row.pack_start(btn_delete_scheme, False, False, 0)

        box.pack_start(load_row, False, False, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)

        box.pack_start(Gtk.Label(
            label="Check a box to override that color. Uncheck to use scheme default.",
            halign=Gtk.Align.START, wrap=True), False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        grid.set_margin_top(8)
        grid.attach(Gtk.Label(label="<b>Element</b>", use_markup=True,
                              halign=Gtk.Align.START), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Foreground</b>", use_markup=True), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Background</b>", use_markup=True), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>B</b>", use_markup=True), 3, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>I</b>", use_markup=True), 4, 0, 1, 1)

        color_buttons = {}

        for row_i, (style_id, label) in enumerate(self.STYLE_ITEMS, start=1):
            props = custom.get(style_id, {})

            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, row_i, 1, 1)

            # Foreground: checkbox + color button
            fg_chk = Gtk.CheckButton()
            fg_btn = Gtk.ColorButton()
            fg_btn.props.use_alpha = False
            fg_btn.set_title(f"{label} Foreground")
            if props.get('fg'):
                rgba = Gdk.RGBA()
                rgba.parse(props['fg'])
                fg_btn.set_rgba(rgba)
                fg_chk.set_active(True)
            fg_btn.set_sensitive(fg_chk.get_active())
            fg_chk.connect('toggled', lambda c, b: b.set_sensitive(c.get_active()), fg_btn)
            fg_btn.connect('color-set', lambda b, c: c.set_active(True), fg_chk)
            fg_box = Gtk.Box(spacing=2)
            fg_box.pack_start(fg_chk, False, False, 0)
            fg_box.pack_start(fg_btn, False, False, 0)
            grid.attach(fg_box, 1, row_i, 1, 1)

            # Background: checkbox + color button
            bg_chk = Gtk.CheckButton()
            bg_btn = Gtk.ColorButton()
            bg_btn.props.use_alpha = False
            bg_btn.set_title(f"{label} Background")
            if props.get('bg'):
                rgba = Gdk.RGBA()
                rgba.parse(props['bg'])
                bg_btn.set_rgba(rgba)
                bg_chk.set_active(True)
            bg_btn.set_sensitive(bg_chk.get_active())
            bg_chk.connect('toggled', lambda c, b: b.set_sensitive(c.get_active()), bg_btn)
            bg_btn.connect('color-set', lambda b, c: c.set_active(True), bg_chk)
            bg_box = Gtk.Box(spacing=2)
            bg_box.pack_start(bg_chk, False, False, 0)
            bg_box.pack_start(bg_btn, False, False, 0)
            grid.attach(bg_box, 2, row_i, 1, 1)

            # Bold checkbox
            bold_chk = Gtk.CheckButton()
            bold_chk.set_active(props.get('bold', False))
            grid.attach(bold_chk, 3, row_i, 1, 1)

            # Italic checkbox
            italic_chk = Gtk.CheckButton()
            italic_chk.set_active(props.get('italic', False))
            grid.attach(italic_chk, 4, row_i, 1, 1)

            color_buttons[style_id] = {
                'fg_btn': fg_btn, 'fg_chk': fg_chk,
                'bg_btn': bg_btn, 'bg_chk': bg_chk,
                'bold_chk': bold_chk, 'italic_chk': italic_chk,
            }

        scroll.add(grid)
        box.pack_start(scroll, True, True, 0)

        # --- Helper: read current colors from the dialog widgets ---
        def _read_colors():
            result = {}
            for sid, w in color_buttons.items():
                p = {}
                if w['fg_chk'].get_active():
                    p['fg'] = self._rgba_to_hex(w['fg_btn'].get_rgba())
                if w['bg_chk'].get_active():
                    p['bg'] = self._rgba_to_hex(w['bg_btn'].get_rgba())
                if w['bold_chk'].get_active():
                    p['bold'] = True
                if w['italic_chk'].get_active():
                    p['italic'] = True
                if p:
                    result[sid] = p
            return result

        # --- Helper: load colors into the dialog widgets ---
        def _load_colors(colors):
            for sid, w in color_buttons.items():
                p = colors.get(sid, {})
                if p.get('fg'):
                    rgba = Gdk.RGBA()
                    rgba.parse(p['fg'])
                    w['fg_btn'].set_rgba(rgba)
                    w['fg_chk'].set_active(True)
                else:
                    w['fg_chk'].set_active(False)
                w['fg_btn'].set_sensitive(w['fg_chk'].get_active())

                if p.get('bg'):
                    rgba = Gdk.RGBA()
                    rgba.parse(p['bg'])
                    w['bg_btn'].set_rgba(rgba)
                    w['bg_chk'].set_active(True)
                else:
                    w['bg_chk'].set_active(False)
                w['bg_btn'].set_sensitive(w['bg_chk'].get_active())

                w['bold_chk'].set_active(p.get('bold', False))
                w['italic_chk'].set_active(p.get('italic', False))

        # --- Save button ---
        def on_save_scheme(_btn):
            name = scheme_name_entry.get_text().strip()
            if not name:
                self._show_error("Save Failed", "Please enter a scheme name.")
                return

            saved = self.config.get('saved_color_schemes', {})
            saved[name] = {
                'base': self.config.get('color_scheme', 'oblivion'),
                'colors': _read_colors(),
            }
            self.config['saved_color_schemes'] = saved
            self.config['active_custom_scheme'] = name
            save_config(self.config)

            # Update load combo
            if scheme_combo.set_active_id(name) is None:
                scheme_combo.append(name, name)
                scheme_combo.set_active_id(name)

            self._set_status(f"Saved color scheme '{name}'")

        # btn_save_scheme connected after widget is created (below)

        # --- Load on combo change ---
        def on_scheme_changed(combo):
            sid = combo.get_active_id()
            if sid == '__none__':
                _load_colors({})
                return
            saved = self.config.get('saved_color_schemes', {})
            if sid in saved:
                scheme_data = saved[sid]
                # Also switch base scheme
                base = scheme_data.get('base', 'oblivion')
                self.config['color_scheme'] = base
                _load_colors(scheme_data.get('colors', {}))

        scheme_combo.connect('changed', on_scheme_changed)

        # --- Delete button ---
        def on_delete_scheme(_btn):
            sid = scheme_combo.get_active_id()
            if sid == '__none__':
                return
            saved = self.config.get('saved_color_schemes', {})
            if sid in saved:
                del saved[sid]
                self.config['saved_color_schemes'] = saved
                if self.config.get('active_custom_scheme') == sid:
                    self.config['active_custom_scheme'] = ''
                save_config(self.config)
                # Rebuild combo
                scheme_combo.remove_all()
                scheme_combo.append('__none__', '(unsaved)')
                for n in sorted(saved.keys()):
                    scheme_combo.append(n, n)
                scheme_combo.set_active_id('__none__')

        btn_delete_scheme.connect('clicked', on_delete_scheme)

        # --- Save scheme row ---
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        save_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        save_row.pack_start(Gtk.Label(label="Save as:", halign=Gtk.Align.START), False, False, 0)

        scheme_name_entry = Gtk.Entry()
        scheme_name_entry.set_placeholder_text("Enter scheme name")
        current_name = self.config.get('active_custom_scheme', '')
        if current_name:
            scheme_name_entry.set_text(current_name)
        save_row.pack_start(scheme_name_entry, True, True, 0)

        btn_save_scheme = Gtk.Button(label="Save")
        btn_save_scheme.get_style_context().add_class('suggested-action')
        btn_save_scheme.connect('clicked', on_save_scheme)
        save_row.pack_start(btn_save_scheme, False, False, 0)

        box.pack_start(save_row, False, False, 0)

        # --- Bottom button row ---
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_bottom(8)

        btn_reset = Gtk.Button(label="Reset All")
        btn_reset.set_tooltip_text("Clear all custom colors")
        btn_row.pack_start(btn_reset, False, False, 0)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_row.pack_end(btn_cancel, False, False, 0)

        btn_apply = Gtk.Button(label="Apply")
        btn_apply.get_style_context().add_class('suggested-action')
        btn_row.pack_end(btn_apply, False, False, 0)

        box.pack_start(btn_row, False, False, 0)

        btn_apply.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_reset.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.REJECT))

        dlg.show_all()

        resp = dlg.run()

        if resp == Gtk.ResponseType.OK:
            self.config['custom_colors'] = _read_colors()
            active = scheme_combo.get_active_id()
            self.config['active_custom_scheme'] = active if active != '__none__' else ''
            save_config(self.config)
            self._apply_scheme_to_all()

        elif resp == Gtk.ResponseType.REJECT:
            self.config['custom_colors'] = {}
            self.config['active_custom_scheme'] = ''
            save_config(self.config)
            self._apply_scheme_to_all()

        dlg.destroy()

    def _rgba_to_hex(self, rgba):
        """Convert a Gdk.RGBA to #rrggbb hex string."""
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        return f'#{r:02x}{g:02x}{b:02x}'

    def _rebuild_quick_combo(self):
        """Refresh the quick-connect dropdown from config."""
        self.quick_combo.remove_all()
        servers = self.config.get('servers', [])
        if not servers:
            self.quick_combo.set_visible(False)
            return
        self.quick_combo.set_visible(True)
        self.quick_combo.append('__none__', 'Quick Connect...')
        for srv in servers:
            label = f"{srv['name']} ({srv.get('protocol','sftp').upper()})"
            self.quick_combo.append(srv['guid'], label)
        self.quick_combo.set_active_id('__none__')

    def _on_quick_connect(self, combo):
        """Instantly connect to a saved server from the toolbar dropdown."""
        server_id = combo.get_active_id()
        if server_id == '__none__' or server_id is None:
            return
        # Disconnect first if connected
        if self.ftp_mgr and self.ftp_mgr.connected:
            self._on_disconnect(None)
        srv = find_server_by_guid(self.config, server_id)
        if srv:
            vals = dict(srv)
            vals['remember'] = True
            vals['server_guid'] = srv['guid']
            vals['server_name'] = srv['name']
            self._do_connect(vals)
        # Reset combo to placeholder
        combo.set_active_id('__none__')

    def _connect_signals(self):
        self.connect('destroy', self._on_quit)
        self.connect('key-press-event', self._on_key_press)
        self.btn_connect.connect('clicked', self._on_connect)
        self.btn_disconnect.connect('clicked', self._on_disconnect)
        # btn_save removed — menu item handles save
        self.btn_refresh.connect('clicked', self._on_refresh)
        self.quick_combo.connect('changed', self._on_quick_connect)
        self.btn_theme.connect('clicked', self._on_toggle_theme)
        self.btn_console.connect('clicked', self._on_toggle_console)
        self.tree_view.connect('row-activated', self._on_tree_row_activated)
        self.tree_view.connect('row-expanded', self._on_tree_row_expanded)
        self.tree_view.connect('button-press-event', self._on_tree_right_click)
        self.notebook.connect('switch-page', self._on_tab_switched)

    # -- Status ---------------------------------------------------------------

    def _set_status(self, msg):
        ctx = self.statusbar.get_context_id('main')
        self.statusbar.pop(ctx)
        self.statusbar.push(ctx, msg)

    def _show_error(self, title, msg):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()

    def _show_info(self, title, msg):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dlg.format_secondary_text(msg)
        dlg.run()
        dlg.destroy()

    # -- Symbol Pane ----------------------------------------------------------

    def _get_file_ext(self, filepath):
        return filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''

    def _update_symbols(self, tab):
        """Parse and populate the symbol list for the given tab."""
        self.symbol_store.clear()
        if not tab:
            return
        ext = self._get_file_ext(tab.remote_path)
        if ext not in SYMBOL_EXTENSIONS:
            self.symbol_store.append(
                ['dialog-information-symbolic', '(no symbols for this file type)', 0, 0])
            return
        start = tab.buffer.get_start_iter()
        end = tab.buffer.get_end_iter()
        content = tab.buffer.get_text(start, end, True)
        symbols = parse_symbols(content, ext)
        if not symbols:
            self.symbol_store.append(
                ['dialog-information-symbolic', '(no functions found)', 0, 0])
            return
        for kind, name, line, offset in symbols:
            icon = SYMBOL_ICONS.get(kind, 'media-playback-start-symbolic')
            display = f"{name}  :{line}"
            self.symbol_store.append([icon, display, line, offset])

    def _on_tab_switched(self, _notebook, _page, page_num):
        tab = self.tabs.get(page_num)
        self._update_symbols(tab)

    def _on_refresh_symbols(self, _btn):
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        self._update_symbols(tab)

    def _on_symbol_activated(self, _view, path, _col):
        tree_iter = self.symbol_store.get_iter(path)
        offset = self.symbol_store[tree_iter][3]
        if offset <= 0 and self.symbol_store[tree_iter][2] <= 0:
            return
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return
        buf = tab.buffer
        # Go to the start of the line containing the symbol
        target_iter = buf.get_iter_at_offset(offset)
        target_iter.set_line_offset(0)
        buf.place_cursor(target_iter)
        tab.source_view.grab_focus()

        def _do_scroll():
            insert_mark = buf.get_insert()
            tab.source_view.scroll_to_mark(insert_mark, 0.0, True, 0.0, 0.5)
            return False

        GLib.idle_add(_do_scroll)

    # -- Connection -----------------------------------------------------------

    def _on_connect(self, _btn):
        dlg = ConnectDialog(self, self.config)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            vals = dlg.get_values()
            dlg.destroy()
            self._rebuild_quick_combo()
            self._do_connect(vals)
        else:
            dlg.destroy()
            self._rebuild_quick_combo()

    def _do_connect(self, vals):
        protocol = vals.get('protocol', 'sftp')
        self._set_status(f"Connecting via {protocol.upper()} to {vals['host']}...")
        self._console_log(f"{protocol.upper()} CONNECT {vals['username']}@{vals['host']}:{vals['port']}")
        self.btn_connect.set_sensitive(False)

        def work():
            try:
                if protocol == 'sftp':
                    self.ftp_mgr = SFTPManager()
                    self.ftp_mgr.connect(
                        vals['host'], vals['port'],
                        vals['username'], vals['password'],
                        vals.get('ssh_key_path', ''),
                    )
                else:
                    self.ftp_mgr = FTPManager()
                    self.ftp_mgr.connect(
                        vals['host'], vals['port'],
                        vals['username'], vals['password'],
                    )
                GLib.idle_add(self._on_connected, vals)
            except Exception as e:
                GLib.idle_add(self._on_connect_failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_connected(self, vals):
        # Auto-save server profile if connecting without one
        server_guid = vals.get('server_guid', '')
        if not server_guid:
            server_guid = str(uuid.uuid4())
            profile = {
                'guid': server_guid,
                'name': vals.get('host', 'Unknown'),
                'protocol': vals.get('protocol', 'sftp'),
                'host': vals['host'],
                'port': vals['port'],
                'username': vals['username'],
                'password': vals['password'],
                'ssh_key_path': vals.get('ssh_key_path', ''),
                'home_directory': vals.get('home_directory', ''),
                'max_upload_size_mb': vals['max_upload_size_mb'],
            }
            self.config.setdefault('servers', []).append(profile)
            vals['server_guid'] = server_guid
            vals['server_name'] = profile['name']

        self.current_server_guid = server_guid
        self.config['host'] = vals['host']
        self.config['port'] = vals['port']
        self.config['username'] = vals['username']
        self.config['password'] = vals['password']
        self.config['max_upload_size_mb'] = vals['max_upload_size_mb']
        self.config['protocol'] = vals.get('protocol', 'sftp')
        self.config['ssh_key_path'] = vals.get('ssh_key_path', '')
        self.config['home_directory'] = vals.get('home_directory', '')
        self.config['last_server'] = server_guid
        save_config(self.config)

        proto_label = vals.get('protocol', 'sftp').upper()
        server_name = vals.get('server_name', '')
        if server_name:
            self.header.set_subtitle(f"[{server_name}] {proto_label}: {vals['username']}@{vals['host']}")
        else:
            self.header.set_subtitle(f"{proto_label}: {vals['username']}@{vals['host']}")
        self._rebuild_quick_combo()
        self.btn_connect.set_sensitive(False)
        self.btn_disconnect.set_sensitive(True)
        self.item_save.set_sensitive(True)
        self.btn_refresh.set_sensitive(True)
        self._set_status("Connected")
        self._console_log(f"Connected to {vals['host']} — home: {self.ftp_mgr.home_dir}", 'success')
        # Use manual home dir if set, otherwise auto-detected from server
        start_dir = vals.get('home_directory', '').strip()
        if not start_dir:
            start_dir = self.ftp_mgr.home_dir
        self._load_tree(start_dir)

    def _on_connect_failed(self, err):
        self.btn_connect.set_sensitive(True)
        self._set_status("Connection failed")
        self._console_log(f"Connection failed: {err}", 'error')
        self._show_error("Connection Failed", err)

    def _on_disconnect(self, _btn):
        if self.ftp_mgr:
            self._console_log("DISCONNECT")
            self.ftp_mgr.disconnect()
            self.ftp_mgr = None
        self.current_server_guid = ''
        self.tree_store.clear()
        self.header.set_subtitle("Disconnected")
        self.btn_connect.set_sensitive(True)
        self.btn_disconnect.set_sensitive(False)
        self.item_save.set_sensitive(False)
        self.btn_refresh.set_sensitive(False)
        self._set_status("Disconnected")

    # -- File Tree ------------------------------------------------------------

    def _load_tree(self, path, parent_iter=None):
        self._set_status(f"Loading {path}...")
        self._console_log(f"LIST {path}")

        def work():
            try:
                entries = self.ftp_mgr.list_dir(path)
                self._console_log(f"LIST {path} — {len(entries)} items")
                GLib.idle_add(self._populate_tree, path, parent_iter, entries)
            except Exception as e:
                self._console_log(f"LIST FAILED {path}: {e}", 'error')
                GLib.idle_add(self._set_status, f"Error listing {path}: {e}")

        threading.Thread(target=work, daemon=True).start()

    def _populate_tree(self, path, parent_iter, entries):
        if parent_iter:
            # Collect old placeholder children to remove AFTER adding new ones.
            # If we remove first, GTK sees 0 children and auto-collapses the row.
            old_children = []
            child = self.tree_store.iter_children(parent_iter)
            while child:
                old_children.append(self.tree_store.get_path(child))
                child = self.tree_store.iter_next(child)
            self.tree_store[parent_iter][4] = True  # mark loaded
        else:
            old_children = None
            self.tree_store.clear()

        # Add new entries
        norm = path.rstrip('/') or ''
        for name, is_dir in entries:
            full = f"{norm}/{name}"
            icon = 'folder' if is_dir else self._icon_for_file(name)
            it = self.tree_store.append(parent_iter, [name, icon, full, is_dir, False])
            if is_dir:
                # Add placeholder child so the expander arrow shows
                self.tree_store.append(it, ['Loading...', 'content-loading-symbolic', '', False, False])

        # Now remove old placeholders (row is still expanded because it has new children)
        if old_children:
            for tree_path in reversed(old_children):
                try:
                    old_iter = self.tree_store.get_iter(tree_path)
                    self.tree_store.remove(old_iter)
                except ValueError:
                    pass

        self._set_status(f"Loaded {path} ({len(entries)} items)")

    def _icon_for_file(self, name):
        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        mapping = {
            'php': 'text-x-script',
            'js': 'text-x-script',
            'ts': 'text-x-script',
            'py': 'text-x-script',
            'html': 'text-html',
            'htm': 'text-html',
            'css': 'text-css',
            'json': 'text-x-generic',
            'xml': 'text-xml',
            'sql': 'text-x-sql',
            'md': 'text-x-generic',
            'txt': 'text-x-generic',
            'sh': 'text-x-script',
            'yml': 'text-x-generic',
            'yaml': 'text-x-generic',
            'ini': 'text-x-generic',
            'conf': 'text-x-generic',
            'env': 'text-x-generic',
        }
        return mapping.get(ext, 'text-x-generic')

    def _on_tree_row_expanded(self, _view, tree_iter, _path):
        is_dir = self.tree_store[tree_iter][3]
        loaded = self.tree_store[tree_iter][4]
        if is_dir and not loaded:
            remote_path = self.tree_store[tree_iter][2]
            self._load_tree(remote_path, tree_iter)

    def _on_tree_row_activated(self, _view, path, _col):
        tree_iter = self.tree_store.get_iter(path)
        is_dir = self.tree_store[tree_iter][3]
        if is_dir:
            if self.tree_view.row_expanded(path):
                self.tree_view.collapse_row(path)
            else:
                self.tree_view.expand_row(path, False)
        else:
            remote_path = self.tree_store[tree_iter][2]
            self._open_file(remote_path)

    # -- File Tree Context Menu ------------------------------------------------

    def _on_tree_right_click(self, _view, event):
        """Show context menu on right-click in file tree."""
        if event.button != 3:
            return False
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            return False

        # Get the clicked row and select it
        path_info = self.tree_view.get_path_at_pos(int(event.x), int(event.y))

        if path_info:
            tree_path = path_info[0]
            self.tree_view.get_selection().select_path(tree_path)
            self.tree_view.set_cursor(tree_path, None, False)

        menu = Gtk.Menu()

        if path_info:
            tree_path, _col, _cx, _cy = path_info
            tree_iter = self.tree_store.get_iter(tree_path)
            is_dir = self.tree_store[tree_iter][3]
            remote_path = self.tree_store[tree_iter][2]
            name = self.tree_store[tree_iter][0]

            if is_dir:
                # Right-clicked on a directory
                item = Gtk.MenuItem(label="New File...")
                item.connect('activate', lambda _: self._on_tree_new_file(remote_path, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label="New Directory...")
                item.connect('activate', lambda _: self._on_tree_new_dir(remote_path, tree_iter))
                menu.append(item)

                menu.append(Gtk.SeparatorMenuItem())

                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_tree_rename(remote_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_tree_permissions(remote_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete Directory '{name}'")
                item.connect('activate', lambda _: self._on_tree_delete_dir(remote_path, tree_iter))
                menu.append(item)
            else:
                # Right-clicked on a file
                item = Gtk.MenuItem(label=f"Rename '{name}'...")
                item.connect('activate', lambda _: self._on_tree_rename(remote_path, name, tree_iter))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Permissions '{name}'...")
                item.connect('activate', lambda _: self._on_tree_permissions(remote_path, name))
                menu.append(item)

                item = Gtk.MenuItem(label=f"Delete '{name}'")
                item.connect('activate', lambda _: self._on_tree_delete_file(remote_path, tree_iter))
                menu.append(item)
        else:
            # Right-clicked on empty space — use the root/home dir
            start_dir = self.config.get('home_directory', '').strip()
            if not start_dir:
                start_dir = self.ftp_mgr.home_dir

            item = Gtk.MenuItem(label="New File...")
            item.connect('activate', lambda _: self._on_tree_new_file(start_dir, None))
            menu.append(item)

            item = Gtk.MenuItem(label="New Directory...")
            item.connect('activate', lambda _: self._on_tree_new_dir(start_dir, None))
            menu.append(item)

        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _ask_name(self, title, prompt):
        """Show a simple dialog asking for a name. Returns name or None."""
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True,
                         use_header_bar=False)
        dlg.set_default_size(300, -1)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        box.pack_start(Gtk.Label(label=prompt, halign=Gtk.Align.START), False, False, 0)

        entry = Gtk.Entry()
        entry.set_activates_default(False)
        entry.connect('activate', lambda _: dlg.response(Gtk.ResponseType.OK))
        box.pack_start(entry, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_row.pack_end(btn_cancel, False, False, 0)
        btn_ok = Gtk.Button(label="Create")
        btn_ok.get_style_context().add_class('suggested-action')
        btn_ok.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_end(btn_ok, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        dlg.show_all()
        resp = dlg.run()
        name = entry.get_text().strip()
        dlg.destroy()

        if resp == Gtk.ResponseType.OK and name:
            return name
        return None

    def _confirm_delete(self, what):
        """Ask for confirmation before deleting. Returns True if confirmed."""
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Confirm Delete",
        )
        dlg.format_secondary_text(f"Are you sure you want to delete:\n\n{what}\n\nThis cannot be undone.")
        resp = dlg.run()
        dlg.destroy()
        return resp == Gtk.ResponseType.YES

    def _on_tree_new_file(self, parent_dir, parent_iter):
        """Create a new empty file in the given directory."""
        name = self._ask_name("New File", "File name:")
        if not name:
            return
        remote_path = f"{parent_dir.rstrip('/')}/{name}"
        self._set_status(f"Creating {remote_path}...")

        def work():
            try:
                self.ftp_mgr.mkfile(remote_path)
                GLib.idle_add(self._on_tree_file_created, parent_dir, parent_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Create Failed", str(e))
                GLib.idle_add(self._set_status, "Create failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_file_created(self, parent_dir, parent_iter, remote_path):
        self._set_status(f"Created {remote_path}")
        self._console_log(f"MKDIR/MKFILE {remote_path}", 'success')
        # Refresh the parent directory
        if parent_iter:
            self.tree_store[parent_iter][4] = False  # mark as not loaded
            self._load_tree(parent_dir, parent_iter)
        else:
            self._on_refresh(None)

    def _on_tree_new_dir(self, parent_dir, parent_iter):
        """Create a new directory in the given directory."""
        name = self._ask_name("New Directory", "Directory name:")
        if not name:
            return
        remote_path = f"{parent_dir.rstrip('/')}/{name}"
        self._set_status(f"Creating directory {remote_path}...")

        def work():
            try:
                self.ftp_mgr.mkdir(remote_path)
                GLib.idle_add(self._on_tree_file_created, parent_dir, parent_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Create Failed", str(e))
                GLib.idle_add(self._set_status, "Create failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_permissions(self, remote_path, name):
        """Show chmod/chown dialog for a file or directory."""
        self._set_status(f"Reading permissions for {name}...")

        def work():
            try:
                mode, owner, group = self.ftp_mgr.get_stat(remote_path)
                GLib.idle_add(self._show_permissions_dialog,
                              remote_path, name, mode, owner, group)
            except Exception as e:
                GLib.idle_add(self._show_error, "Permission Error", str(e))
                GLib.idle_add(self._set_status, "Failed to read permissions")

        threading.Thread(target=work, daemon=True).start()

    def _show_permissions_dialog(self, remote_path, name, mode, owner, group):
        """Display the permissions editing dialog."""
        self._set_status(f"Permissions: {name}")

        dlg = Gtk.Dialog(
            title=f"Permissions — {name}",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(350, -1)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # --- Permission checkboxes ---
        box.pack_start(Gtk.Label(label=f"<b>{remote_path}</b>",
                                 use_markup=True, halign=Gtk.Align.START),
                       False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        grid.set_margin_top(8)

        # Headers
        grid.attach(Gtk.Label(label=""), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Read</b>", use_markup=True), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Write</b>", use_markup=True), 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="<b>Execute</b>", use_markup=True), 3, 0, 1, 1)

        # Build checkboxes for owner/group/others
        checks = {}
        labels = [('Owner', 6), ('Group', 3), ('Others', 0)]
        for row_i, (label, shift) in enumerate(labels, start=1):
            grid.attach(Gtk.Label(label=label, halign=Gtk.Align.START), 0, row_i, 1, 1)
            for col_i, (perm, bit) in enumerate(
                    [('r', 2), ('w', 1), ('x', 0)], start=1):
                chk = Gtk.CheckButton()
                chk.set_active(bool(mode & (1 << (shift + bit))))
                grid.attach(chk, col_i, row_i, 1, 1)
                checks[(label, perm)] = chk

        box.pack_start(grid, False, False, 0)

        # --- Octal display ---
        octal_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        octal_row.pack_start(Gtk.Label(label="Octal:"), False, False, 0)
        octal_entry = Gtk.Entry(text=f"{mode:03o}", width_chars=6)
        octal_row.pack_start(octal_entry, False, False, 0)
        box.pack_start(octal_row, False, False, 0)

        # Sync checkboxes → octal entry
        def update_octal(*_args):
            val = 0
            for (lbl, perm), chk in checks.items():
                shift = {'Owner': 6, 'Group': 3, 'Others': 0}[lbl]
                bit = {'r': 2, 'w': 1, 'x': 0}[perm]
                if chk.get_active():
                    val |= 1 << (shift + bit)
            octal_entry.set_text(f"{val:03o}")

        for chk in checks.values():
            chk.connect('toggled', update_octal)

        # Sync octal entry → checkboxes
        def update_checks(*_args):
            txt = octal_entry.get_text().strip()
            try:
                val = int(txt, 8)
            except ValueError:
                return
            for (lbl, perm), chk in checks.items():
                shift = {'Owner': 6, 'Group': 3, 'Others': 0}[lbl]
                bit = {'r': 2, 'w': 1, 'x': 0}[perm]
                # Block signal to avoid loop
                chk.handler_block_by_func(update_octal)
                chk.set_active(bool(val & (1 << (shift + bit))))
                chk.handler_unblock_by_func(update_octal)

        octal_entry.connect('changed', update_checks)

        # --- Buttons ---
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(8)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.CANCEL))
        btn_row.pack_end(btn_cancel, False, False, 0)

        btn_apply = Gtk.Button(label="Apply")
        btn_apply.get_style_context().add_class('suggested-action')
        btn_apply.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        btn_row.pack_end(btn_apply, False, False, 0)

        box.pack_start(btn_row, False, False, 0)
        dlg.show_all()

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            try:
                new_mode = int(octal_entry.get_text().strip(), 8)
            except ValueError:
                self._show_error("Invalid Permissions",
                                 "Octal value is not valid.")
                dlg.destroy()
                return

            self._apply_permissions(remote_path, name, new_mode)
        dlg.destroy()

    def _apply_permissions(self, remote_path, name, new_mode):
        """Apply chmod in a background thread."""
        self._set_status(f"Applying permissions to {name}...")

        def work():
            try:
                self.ftp_mgr.chmod(remote_path, new_mode)
                GLib.idle_add(self._set_status,
                              f"Permissions set: {name} → {oct(new_mode)}")
                self._console_log(f"CHMOD {oct(new_mode)} {remote_path}", 'success')
            except Exception as e:
                GLib.idle_add(self._show_error, "Permission Error", str(e))
                GLib.idle_add(self._set_status, "Permission update failed")
                self._console_log(f"CHMOD FAILED {remote_path}: {e}", 'error')

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_rename(self, remote_path, old_name, tree_iter):
        """Rename a file or directory on the server."""
        new_name = self._ask_name("Rename", f"New name for '{old_name}':")
        if not new_name or new_name == old_name:
            return
        parent_dir = os.path.dirname(remote_path)
        new_path = f"{parent_dir.rstrip('/')}/{new_name}"
        self._set_status(f"Renaming {old_name} to {new_name}...")

        def work():
            try:
                self.ftp_mgr.rename(remote_path, new_path)
                GLib.idle_add(self._on_tree_renamed, tree_iter,
                              remote_path, new_path, new_name)
            except Exception as e:
                GLib.idle_add(self._show_error, "Rename Failed", str(e))
                GLib.idle_add(self._set_status, "Rename failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_renamed(self, tree_iter, old_path, new_path, new_name):
        """Update the tree and any open tabs after a rename."""
        # Update tree store
        is_dir = self.tree_store[tree_iter][3]
        self.tree_store[tree_iter][0] = new_name
        self.tree_store[tree_iter][2] = new_path
        if not is_dir:
            self.tree_store[tree_iter][1] = self._icon_for_file(new_name)

        # Update any open tab pointing to the old path
        for tab in self.tabs.values():
            if tab.remote_path == old_path:
                tab.remote_path = new_path
                # Update the tab label
                page_widget = tab.source_view.get_parent()
                tab_widget = self.notebook.get_tab_label(page_widget)
                if tab_widget:
                    # tab_widget is the EventBox > Box > Label
                    ebox = tab_widget
                    tab_box = ebox.get_child()
                    for child in tab_box.get_children():
                        if isinstance(child, Gtk.Label):
                            if tab.modified:
                                child.set_markup(f"<b>* {new_name}</b>")
                            else:
                                child.set_text(new_name)
                            break
                break

        self._set_status(f"Renamed to {new_path}")
        self._console_log(f"RENAME {old_path} → {new_path}", 'success')

    def _on_tree_delete_file(self, remote_path, tree_iter):
        """Delete a file from the server."""
        if not self._confirm_delete(remote_path):
            return
        self._set_status(f"Deleting {remote_path}...")

        def work():
            try:
                self.ftp_mgr.rmfile(remote_path)
                GLib.idle_add(self._on_tree_item_deleted, tree_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Delete Failed", str(e))
                GLib.idle_add(self._set_status, "Delete failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_delete_dir(self, remote_path, tree_iter):
        """Delete a directory from the server."""
        if not self._confirm_delete(remote_path):
            return
        self._set_status(f"Deleting directory {remote_path}...")

        def work():
            try:
                self.ftp_mgr.rmdir(remote_path)
                GLib.idle_add(self._on_tree_item_deleted, tree_iter, remote_path)
            except Exception as e:
                GLib.idle_add(self._show_error, "Delete Failed",
                              f"{str(e)}\n\nNote: directory must be empty to delete.")
                GLib.idle_add(self._set_status, "Delete failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_tree_item_deleted(self, tree_iter, remote_path):
        """Remove the deleted item from the tree."""
        self.tree_store.remove(tree_iter)
        self._set_status(f"Deleted {remote_path}")
        self._console_log(f"DELETE {remote_path}", 'success')

        # Close any open tab for this file
        for page_num, tab in list(self.tabs.items()):
            if tab.remote_path == remote_path:
                self._close_tab(page_num)
                break

    # -- File Operations ------------------------------------------------------

    def _open_file(self, remote_path):
        # Check if already open
        for page_num, tab in self.tabs.items():
            if tab.remote_path == remote_path:
                self.notebook.set_current_page(page_num)
                return

        self._set_status(f"Downloading {remote_path}...")
        self._console_log(f"GET {remote_path}")
        filename = os.path.basename(remote_path)
        local_path = os.path.join(self.tmp_dir, filename.replace('/', '_') + f'_{id(remote_path)}')
        srv_guid = self.current_server_guid

        def work():
            try:
                self.ftp_mgr.download(remote_path, local_path)
                with open(local_path, 'r', errors='replace') as f:
                    content = f.read()
                GLib.idle_add(self._create_editor_tab, remote_path, local_path,
                              content, False, srv_guid)
            except Exception as e:
                GLib.idle_add(self._show_error, "Download Failed", str(e))
                GLib.idle_add(self._set_status, "Download failed")

        threading.Thread(target=work, daemon=True).start()

    def _create_editor_tab(self, remote_path, local_path, content,
                           is_local=False, server_guid=''):
        # Create source buffer with language
        lang_mgr = GtkSource.LanguageManager.get_default()
        lang = self._detect_language(lang_mgr, remote_path)

        buf = GtkSource.Buffer()
        if lang:
            buf.set_language(lang)
        buf.set_highlight_syntax(True)

        # Set color scheme from config
        scheme = self._get_scheme()
        if scheme:
            buf.set_style_scheme(scheme)

        buf.set_text(content)
        buf.set_modified(False)

        # Create source view
        view = GtkSource.View.new_with_buffer(buf)
        view.set_show_line_numbers(True)
        view.set_highlight_current_line(True)
        view.set_auto_indent(True)
        view.set_indent_on_tab(True)
        view.set_tab_width(4)
        view.set_insert_spaces_instead_of_tabs(True)
        view.set_show_line_marks(True)
        view.set_monospace(True)
        view.get_style_context().add_class('editor-view')

        # Code completion — deferred until widget is realized
        def _setup_completion(*_args):
            ext = self._get_file_ext(remote_path)
            completion = view.get_completion()
            completion.set_property('show-headers', False)
            completion.set_property('select-on-show', True)

            if ext in COMPLETION_LANGS:
                provider = SynPadCompletionProvider(COMPLETION_LANGS[ext])
                completion.add_provider(provider)
                view._lang_provider = provider

            doc_provider = DocumentWordProvider()
            completion.add_provider(doc_provider)
            view._doc_provider = doc_provider

        view.connect('realize', _setup_completion)

        # Intercept Ctrl+F/R before GtkSourceView's built-in handlers
        view.connect('key-press-event', self._on_editor_key_press)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)

        # Tab label with close button, wrapped in EventBox for right-click menu
        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tab_label = Gtk.Label(label=os.path.basename(remote_path))
        tab_box.pack_start(tab_label, True, True, 0)
        close_btn = Gtk.Button()
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.set_image(Gtk.Image.new_from_icon_name('window-close-symbolic', Gtk.IconSize.MENU))
        tab_box.pack_end(close_btn, False, False, 0)

        tab_ebox = Gtk.EventBox()
        tab_ebox.add(tab_box)
        tab_ebox.connect('button-press-event', self._on_tab_right_click)
        tab_ebox.show_all()

        # Remove welcome tab if present
        if self.notebook.get_n_pages() == 1 and not self.tabs:
            self.notebook.remove_page(0)

        page_num = self.notebook.append_page(scroll, tab_ebox)
        self.notebook.set_tab_reorderable(scroll, True)
        scroll.show_all()
        self.notebook.set_current_page(page_num)

        tab = OpenTab(remote_path, local_path, view, buf,
                      is_local=is_local, server_guid=server_guid)
        self.tabs[page_num] = tab

        # Track modification
        def on_modified_changed(_buf):
            if buf.get_modified():
                tab_label.set_markup(f"<b>* {os.path.basename(remote_path)}</b>")
                tab.modified = True
            else:
                tab_label.set_text(os.path.basename(remote_path))
                tab.modified = False

        buf.connect('modified-changed', on_modified_changed)

        # Close button
        def on_close(_btn):
            self._close_tab(page_num)

        close_btn.connect('clicked', on_close)

        self._set_status(f"Opened {remote_path}")
        self._update_symbols(tab)

    def _detect_language(self, lang_mgr, filepath):
        ext = filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
        mapping = {
            'php': 'php', 'js': 'js', 'ts': 'typescript',
            'py': 'python3', 'html': 'html', 'htm': 'html',
            'css': 'css', 'json': 'json', 'xml': 'xml',
            'sql': 'sql', 'sh': 'sh', 'bash': 'sh',
            'yml': 'yaml', 'yaml': 'yaml', 'md': 'markdown',
            'ini': 'ini', 'conf': 'ini',
        }
        lang_id = mapping.get(ext)
        if lang_id:
            return lang_mgr.get_language(lang_id)
        return None

    def _close_tab(self, page_num):
        tab = self.tabs.get(page_num)
        if not tab:
            return
        if tab.modified:
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Unsaved Changes",
            )
            dlg.format_secondary_text(
                f"'{os.path.basename(tab.remote_path)}' has unsaved changes. Close anyway?"
            )
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return

        self.notebook.remove_page(page_num)
        del self.tabs[page_num]
        # Re-index tabs after removal
        self._reindex_tabs()

        if self.notebook.get_n_pages() == 0:
            welcome = Gtk.Label(label="Connect to an FTP/SFTP server and open a file to start editing.")
            welcome.set_margin_top(40)
            welcome.show()
            self.notebook.append_page(welcome, Gtk.Label(label="Welcome"))

    def _reindex_tabs(self):
        new_tabs = {}
        for i in range(self.notebook.get_n_pages()):
            widget = self.notebook.get_nth_page(i)
            for old_num, tab in self.tabs.items():
                scroll = tab.source_view.get_parent()
                if scroll is widget:
                    new_tabs[i] = tab
                    break
        self.tabs = new_tabs

    # -- Tab Context Menu -----------------------------------------------------

    def _on_tab_right_click(self, widget, event):
        """Show context menu on right-click on a tab label."""
        if event.button != 3:
            return False

        # Find which page this tab belongs to
        clicked_page = None
        for i in range(self.notebook.get_n_pages()):
            page_widget = self.notebook.get_nth_page(i)
            tab_widget = self.notebook.get_tab_label(page_widget)
            if tab_widget is widget:
                clicked_page = i
                break
        if clicked_page is None or clicked_page not in self.tabs:
            return False

        menu = Gtk.Menu()

        item_close = Gtk.MenuItem(label="Close")
        item_close.connect('activate', lambda _: self._close_tab(clicked_page))
        menu.append(item_close)

        item_close_all = Gtk.MenuItem(label="Close All")
        item_close_all.connect('activate', lambda _: self._close_all_tabs())
        menu.append(item_close_all)

        item_close_others = Gtk.MenuItem(label="Close All But This")
        item_close_others.connect('activate',
                                  lambda _: self._close_all_tabs_except(clicked_page))
        menu.append(item_close_others)

        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _close_all_tabs(self):
        """Close all open tabs."""
        # Work on a copy since _close_tab modifies self.tabs
        for page_num in sorted(self.tabs.keys(), reverse=True):
            self._close_tab(page_num)

    def _close_all_tabs_except(self, keep_page):
        """Close all tabs except the given page number."""
        # Find the remote_path of the tab to keep (page nums shift as we close)
        keep_tab = self.tabs.get(keep_page)
        if not keep_tab:
            return
        keep_path = keep_tab.remote_path
        for page_num in sorted(self.tabs.keys(), reverse=True):
            tab = self.tabs.get(page_num)
            if tab and tab.remote_path != keep_path:
                self._close_tab(page_num)

    # -- Open Local File ------------------------------------------------------

    def _on_new_local_file(self):
        """Create a new local file via Save dialog, then open it."""
        dlg = Gtk.FileChooserDialog(
            title="New Local File",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name("untitled.php")

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            filepath = dlg.get_filename()
            dlg.destroy()
            # Create the empty file
            try:
                with open(filepath, 'w') as f:
                    pass
            except Exception as e:
                self._show_error("Create Failed", str(e))
                return
            self._open_local_file(filepath)
        else:
            dlg.destroy()

    def _on_open_local_file(self):
        """Open a file from the local filesystem."""
        dlg = Gtk.FileChooserDialog(
            title="Open Local File",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        # Add filters
        filt_all = Gtk.FileFilter()
        filt_all.set_name("All files")
        filt_all.add_pattern("*")
        dlg.add_filter(filt_all)

        filt_code = Gtk.FileFilter()
        filt_code.set_name("Code files")
        for ext in ['php', 'js', 'ts', 'jsx', 'tsx', 'py', 'html', 'htm',
                     'css', 'json', 'xml', 'sql', 'sh', 'yml', 'yaml',
                     'md', 'txt', 'ini', 'conf', 'env']:
            filt_code.add_pattern(f"*.{ext}")
        dlg.add_filter(filt_code)

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            filepath = dlg.get_filename()
            dlg.destroy()
            self._open_local_file(filepath)
        else:
            dlg.destroy()

    def _open_local_file(self, filepath):
        """Open a local file in an editor tab."""
        # Check if already open
        for page_num, tab in self.tabs.items():
            if tab.is_local and tab.local_path == filepath:
                self.notebook.set_current_page(page_num)
                return

        try:
            with open(filepath, 'r', errors='replace') as f:
                content = f.read()
        except Exception as e:
            self._show_error("Open Failed", str(e))
            return

        self._create_editor_tab(filepath, filepath, content, is_local=True)
        self.item_save.set_sensitive(True)

    # -- Save (local or remote) -----------------------------------------------

    def _on_save(self, _btn):
        """Save the current file — locally or via upload depending on type."""
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            self._set_status("No file open to save")
            return
        if tab.is_local:
            self._on_save_local(tab)
        else:
            self._on_save_upload(None)

    def _on_save_local(self, tab):
        """Save a local file to disk."""
        start = tab.buffer.get_start_iter()
        end = tab.buffer.get_end_iter()
        content = tab.buffer.get_text(start, end, True)

        try:
            with open(tab.local_path, 'w') as f:
                f.write(content)
            tab.buffer.set_modified(False)
            size_kb = os.path.getsize(tab.local_path) / 1024
            self._set_status(f"Saved {tab.local_path} ({size_kb:.1f} KB)")
        except Exception as e:
            self._show_error("Save Failed", str(e))

    # -- Save & Upload --------------------------------------------------------

    def _on_save_upload(self, _btn):
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            self._set_status("No file open to save")
            return

        # Save content to local temp file first (always on main thread)
        start = tab.buffer.get_start_iter()
        end = tab.buffer.get_end_iter()
        content = tab.buffer.get_text(start, end, True)
        with open(tab.local_path, 'w') as f:
            f.write(content)

        # Check size
        file_size = os.path.getsize(tab.local_path)
        max_mb = self.config.get('max_upload_size_mb', 5)
        max_bytes = max_mb * 1024 * 1024
        if file_size > max_bytes:
            self._show_error(
                "File Too Large",
                f"File size: {file_size / 1024 / 1024:.2f} MB\n"
                f"Max allowed: {max_mb} MB\n\n"
                f"Adjust the limit in the connection settings."
            )
            return

        # Check if we need to switch server
        tab_guid = tab.server_guid
        if tab_guid and tab_guid != self.current_server_guid:
            srv = find_server_by_guid(self.config, tab_guid)
            if not srv:
                self._show_error("Server Not Found",
                    "The server profile for this file no longer exists.\n"
                    "Connect to the correct server manually.")
                return
            self._console_log(
                f"Auto-switching to '{srv['name']}' for upload...", 'success')
            # Disconnect current
            if self.ftp_mgr and self.ftp_mgr.connected:
                self.ftp_mgr.disconnect()
                self.ftp_mgr = None
                self.current_server_guid = ''
            # Store pending upload info, then connect
            self._pending_upload = (tab, page_num, max_mb)
            vals = dict(srv)
            vals['server_guid'] = srv['guid']
            vals['server_name'] = srv['name']
            vals['remember'] = True
            self._set_status(f"Switching to {srv['name']}...")
            self.item_save.set_sensitive(False)

            def switch_connect():
                try:
                    protocol = vals.get('protocol', 'sftp')
                    if protocol == 'sftp':
                        self.ftp_mgr = SFTPManager()
                        self.ftp_mgr.connect(
                            vals['host'], vals['port'],
                            vals['username'], vals['password'],
                            vals.get('ssh_key_path', ''),
                        )
                    else:
                        self.ftp_mgr = FTPManager()
                        self.ftp_mgr.connect(
                            vals['host'], vals['port'],
                            vals['username'], vals['password'],
                        )
                    # UI update + trigger pending upload on main thread
                    GLib.idle_add(self._on_switch_connected_and_upload, vals)
                except Exception as e:
                    GLib.idle_add(self._on_upload_failed,
                                  f"Server switch failed: {e}")

            threading.Thread(target=switch_connect, daemon=True).start()
            return

        # No server switch needed — check connection
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            if tab_guid:
                srv = find_server_by_guid(self.config, tab_guid)
                if srv:
                    self._pending_upload = (tab, page_num, max_mb)
                    vals = dict(srv)
                    vals['server_guid'] = srv['guid']
                    vals['server_name'] = srv['name']
                    vals['remember'] = True
                    self._set_status(f"Reconnecting to {srv['name']}...")
                    self.item_save.set_sensitive(False)

                    def reconnect():
                        try:
                            protocol = vals.get('protocol', 'sftp')
                            if protocol == 'sftp':
                                self.ftp_mgr = SFTPManager()
                                self.ftp_mgr.connect(
                                    vals['host'], vals['port'],
                                    vals['username'], vals['password'],
                                    vals.get('ssh_key_path', ''),
                                )
                            else:
                                self.ftp_mgr = FTPManager()
                                self.ftp_mgr.connect(
                                    vals['host'], vals['port'],
                                    vals['username'], vals['password'],
                                )
                            GLib.idle_add(self._on_switch_connected_and_upload, vals)
                        except Exception as e:
                            GLib.idle_add(self._on_upload_failed,
                                          f"Reconnect failed: {e}")

                    threading.Thread(target=reconnect, daemon=True).start()
                    return
            self._show_error("Not Connected", "Connect to a server first.")
            return

        # Connected to the right server — upload directly
        self._do_upload(tab, page_num, max_mb)

    def _do_upload(self, tab, page_num, max_mb):
        """Upload the file (local temp already written). Must be called on main thread."""
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            self._show_error("Not Connected", "Connection lost. Try saving again.")
            self.item_save.set_sensitive(True)
            return
        self._set_status(f"Uploading {tab.remote_path}...")
        file_size = os.path.getsize(tab.local_path)
        self._console_log(f"PUT {tab.remote_path} ({file_size / 1024:.1f} KB)")
        self.item_save.set_sensitive(False)

        # Capture reference to manager — don't use self.ftp_mgr in thread
        # in case it changes during upload
        mgr = self.ftp_mgr

        def work():
            try:
                mgr.upload(tab.remote_path, tab.local_path, max_mb)
                GLib.idle_add(self._on_upload_done, tab, page_num)
            except Exception as e:
                GLib.idle_add(self._on_upload_failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_switch_connected_and_upload(self, vals):
        """Update UI after server switch, then perform the pending upload."""
        self.current_server_guid = vals.get('server_guid', '')
        self.config['last_server'] = vals.get('server_guid', '')
        save_config(self.config)

        proto_label = vals.get('protocol', 'sftp').upper()
        server_name = vals.get('server_name', '')
        if server_name:
            self.header.set_subtitle(f"[{server_name}] {proto_label}: {vals['username']}@{vals['host']}")
        else:
            self.header.set_subtitle(f"{proto_label}: {vals['username']}@{vals['host']}")
        self.btn_connect.set_sensitive(False)
        self.btn_disconnect.set_sensitive(True)
        self.btn_refresh.set_sensitive(True)
        self._console_log(
            f"Switched to {vals['username']}@{vals['host']}", 'success')

        # Perform the pending upload FIRST, then reload tree
        # (both use the SFTP connection which is not thread-safe)
        if self._pending_upload:
            tab, page_num, max_mb = self._pending_upload
            self._pending_upload = None
            # Upload, and reload tree after upload completes
            self._pending_tree_reload = vals
            self._do_upload(tab, page_num, max_mb)
        else:
            # No pending upload — just reload tree
            start_dir = vals.get('home_directory', '').strip()
            if not start_dir and self.ftp_mgr:
                start_dir = self.ftp_mgr.home_dir
            if start_dir:
                self._load_tree(start_dir)

    def _on_upload_done(self, tab, page_num):
        tab.buffer.set_modified(False)
        self.item_save.set_sensitive(True)
        size_kb = os.path.getsize(tab.local_path) / 1024
        self._set_status(f"Uploaded {tab.remote_path} ({size_kb:.1f} KB)")
        self._console_log(f"PUT OK {tab.remote_path} ({size_kb:.1f} KB)", 'success')

        # If there's a pending tree reload (from server switch), do it now
        if hasattr(self, '_pending_tree_reload') and self._pending_tree_reload:
            vals = self._pending_tree_reload
            self._pending_tree_reload = None
            start_dir = vals.get('home_directory', '').strip()
            if not start_dir and self.ftp_mgr:
                start_dir = self.ftp_mgr.home_dir
            if start_dir:
                self._load_tree(start_dir)

    def _on_upload_failed(self, err):
        self.item_save.set_sensitive(True)
        self._set_status("Upload failed")
        self._console_log(f"PUT FAILED: {err}", 'error')
        self._show_error("Upload Failed", err)

    # -- Refresh --------------------------------------------------------------

    def _on_refresh(self, _btn):
        if self.ftp_mgr and self.ftp_mgr.connected:
            start_dir = self.config.get('home_directory', '').strip()
            if not start_dir:
                start_dir = self.ftp_mgr.home_dir
            self._load_tree(start_dir)

    # -- Keyboard Shortcuts ---------------------------------------------------

    # -- Search / Replace -------------------------------------------------------

    def _build_search_window(self, show_replace=False):
        """Create the search/replace window."""
        if self._search_window:
            self._search_window.destroy()

        win = Gtk.Window(
            title="Find & Replace" if show_replace else "Find",
            transient_for=self,
            destroy_with_parent=True,
            type_hint=Gdk.WindowTypeHint.DIALOG,
        )
        win.set_default_size(420, -1)
        win.set_resizable(False)
        win.set_keep_above(True)
        win.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        win.connect('delete-event', lambda *a: self._on_search_close() or True)
        win.connect('key-press-event', self._on_search_window_key)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # --- Find row ---
        find_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        find_row.pack_start(Gtk.Label(label="Find:", width_chars=8, halign=Gtk.Align.END),
                            False, False, 0)

        self._search_entry = Gtk.Entry(hexpand=True)
        self._search_entry.connect('activate', self._on_search_next)
        self._search_entry.connect('changed', self._on_search_changed)
        find_row.pack_start(self._search_entry, True, True, 0)

        btn_prev = Gtk.Button()
        btn_prev.set_image(Gtk.Image.new_from_icon_name(
            'go-up-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_prev.set_relief(Gtk.ReliefStyle.NONE)
        btn_prev.set_tooltip_text("Previous (Shift+Enter)")
        btn_prev.connect('clicked', self._on_search_prev)
        find_row.pack_start(btn_prev, False, False, 0)

        btn_next = Gtk.Button()
        btn_next.set_image(Gtk.Image.new_from_icon_name(
            'go-down-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_next.set_relief(Gtk.ReliefStyle.NONE)
        btn_next.set_tooltip_text("Next (Enter)")
        btn_next.connect('clicked', self._on_search_next)
        find_row.pack_start(btn_next, False, False, 0)

        box.pack_start(find_row, False, False, 0)

        # --- Replace row ---
        if show_replace:
            replace_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            replace_row.pack_start(
                Gtk.Label(label="Replace:", width_chars=8, halign=Gtk.Align.END),
                False, False, 0)

            self._replace_entry = Gtk.Entry(hexpand=True)
            replace_row.pack_start(self._replace_entry, True, True, 0)

            btn_replace = Gtk.Button(label="Replace")
            btn_replace.connect('clicked', self._on_replace_one)
            replace_row.pack_start(btn_replace, False, False, 0)

            btn_replace_all = Gtk.Button(label="All")
            btn_replace_all.connect('clicked', self._on_replace_all)
            replace_row.pack_start(btn_replace_all, False, False, 0)

            box.pack_start(replace_row, False, False, 0)

        # --- Options row ---
        opt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        opt_row.set_margin_start(70)

        self._chk_match_case = Gtk.CheckButton(label="Match case")
        self._chk_match_case.connect('toggled', self._on_search_option_changed)
        opt_row.pack_start(self._chk_match_case, False, False, 0)

        self._chk_regex = Gtk.CheckButton(label="Regex")
        self._chk_regex.connect('toggled', self._on_search_option_changed)
        opt_row.pack_start(self._chk_regex, False, False, 0)

        self._search_match_label = Gtk.Label(label="")
        opt_row.pack_end(self._search_match_label, False, False, 0)

        box.pack_start(opt_row, False, False, 0)

        win.add(box)
        self._search_window = win
        self._search_show_replace = show_replace

        # Search context
        self._search_settings = GtkSource.SearchSettings()
        self._search_settings.set_wrap_around(True)
        self._search_context = None

    def _on_search_window_key(self, _win, event):
        """Handle keys in the search window."""
        if event.keyval == Gdk.KEY_Escape:
            self._on_search_close()
            return True
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        if event.keyval == Gdk.KEY_Return and shift:
            self._on_search_prev()
            return True
        return False

    def _show_search(self, show_replace=False):
        """Show the search window, optionally with replace."""
        # Reuse if already open with the right mode
        if self._search_window and self._search_show_replace == show_replace:
            self._search_window.present()
            self._search_entry.grab_focus()
        else:
            self._build_search_window(show_replace)
            self._search_window.show_all()

        # Pre-fill with selected text
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if tab:
            buf = tab.buffer
            if buf.get_has_selection():
                start, end = buf.get_selection_bounds()
                selected = buf.get_text(start, end, False)
                if '\n' not in selected:
                    self._search_entry.set_text(selected)
            self._search_entry.select_region(0, -1)
            self._setup_search_context(tab)

    def _setup_search_context(self, tab):
        """Create or update the search context for the current tab."""
        self._search_context = GtkSource.SearchContext.new(
            tab.buffer, self._search_settings)
        self._search_context.set_highlight(True)
        self._update_match_count()

    def _apply_search_settings(self):
        """Apply checkbox state to search settings."""
        text = self._search_entry.get_text()
        self._search_settings.set_search_text(text if text else None)
        self._search_settings.set_case_sensitive(self._chk_match_case.get_active())
        self._search_settings.set_regex_enabled(self._chk_regex.get_active())

    def _update_match_count(self):
        """Update the match count label."""
        if not self._search_context:
            self._search_match_label.set_text("")
            return
        count = self._search_context.get_occurrences_count()
        if count == -1:
            self._search_match_label.set_text("...")
        elif count == 0:
            self._search_match_label.set_markup(
                '<span foreground="red">No matches</span>')
        else:
            # Find which match the cursor is on
            page_num = self.notebook.get_current_page()
            tab = self.tabs.get(page_num)
            if tab:
                cursor = tab.buffer.get_iter_at_mark(tab.buffer.get_insert())
                pos = self._search_context.get_occurrence_position(
                    cursor, cursor)
                if pos > 0:
                    self._search_match_label.set_text(f"{pos} of {count}")
                else:
                    self._search_match_label.set_text(f"{count} matches")
            else:
                self._search_match_label.set_text(f"{count} matches")

    def _on_search_changed(self, _entry):
        """Called when search text changes — update highlights live."""
        if not self._search_window:
            return
        self._apply_search_settings()
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if tab and not self._search_context:
            self._setup_search_context(tab)
        if self._search_context:
            GLib.idle_add(self._update_match_count)

    def _on_search_option_changed(self, _chk):
        """Called when a checkbox is toggled."""
        self._apply_search_settings()
        if self._search_context:
            GLib.idle_add(self._update_match_count)

    def _on_search_next(self, *_args):
        """Find next match."""
        self._apply_search_settings()
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab or not self._search_context:
            return
        # Search from END of current selection so we advance to the next match
        if tab.buffer.get_has_selection():
            _, search_from = tab.buffer.get_selection_bounds()
        else:
            search_from = tab.buffer.get_iter_at_mark(tab.buffer.get_insert())
        result = self._search_context.forward(search_from)
        found, start, end = result[0], result[1], result[2]
        if found:
            tab.buffer.select_range(start, end)
            tab.source_view.scroll_to_iter(start, 0.1, True, 0.0, 0.5)
        self._update_match_count()

    def _on_search_prev(self, *_args):
        """Find previous match."""
        self._apply_search_settings()
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab or not self._search_context:
            return
        # Search from START of current selection so we go to the previous match
        if tab.buffer.get_has_selection():
            search_from, _ = tab.buffer.get_selection_bounds()
        else:
            search_from = tab.buffer.get_iter_at_mark(tab.buffer.get_insert())
        result = self._search_context.backward(search_from)
        found, start, end = result[0], result[1], result[2]
        if found:
            tab.buffer.select_range(start, end)
            tab.source_view.scroll_to_iter(start, 0.1, True, 0.0, 0.5)
        self._update_match_count()

    def _on_replace_one(self, *_args):
        """Replace the current match and move to next."""
        self._apply_search_settings()
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab or not self._search_context:
            return
        buf = tab.buffer
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            replacement = self._replace_entry.get_text()
            try:
                self._search_context.replace(start, end, replacement, -1)
            except Exception:
                pass
        self._on_search_next()

    def _on_replace_all(self, *_args):
        """Replace all matches."""
        self._apply_search_settings()
        if not self._search_context:
            return
        replacement = self._replace_entry.get_text()
        try:
            count = self._search_context.replace_all(replacement, -1)
            self._set_status(f"Replaced {count} occurrence(s)")
        except Exception as e:
            self._set_status(f"Replace error: {e}")
        self._update_match_count()

    def _on_search_close(self, *_args):
        """Close the search window and clear highlights."""
        if self._search_context:
            self._search_context.set_highlight(False)
            self._search_settings.set_search_text(None)
            self._search_context = None
        if self._search_window:
            self._search_window.destroy()
            self._search_window = None
        # Return focus to editor
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if tab:
            tab.source_view.grab_focus()

    # -- Keyboard Shortcuts ---------------------------------------------------

    def _on_pretty_print_json(self):
        """Pretty print the current buffer as JSON."""
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return
        buf = tab.buffer
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        try:
            parsed = json.loads(text)
            pretty = json.dumps(parsed, indent=4, ensure_ascii=False)
            buf.begin_user_action()
            buf.set_text(pretty)
            buf.end_user_action()
            self._set_status("JSON formatted")
        except json.JSONDecodeError as e:
            self._show_error("JSON Error", f"Invalid JSON:\n\n{e}")

    def _on_pretty_print_xml(self):
        """Pretty print the current buffer as XML."""
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return
        buf = tab.buffer
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        try:
            import xml.dom.minidom
            dom = xml.dom.minidom.parseString(text)
            pretty = dom.toprettyxml(indent="    ")
            # Remove extra XML declaration if the original didn't have one
            if not text.lstrip().startswith('<?xml'):
                # Strip the declaration added by toprettyxml
                lines = pretty.split('\n')
                if lines and lines[0].startswith('<?xml'):
                    pretty = '\n'.join(lines[1:])
            pretty = pretty.rstrip() + '\n'
            buf.begin_user_action()
            buf.set_text(pretty)
            buf.end_user_action()
            self._set_status("XML formatted")
        except Exception as e:
            self._show_error("XML Error", f"Invalid XML:\n\n{e}")

    def _on_goto_line(self):
        """Show a small dialog to jump to a line number."""
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return

        dlg = Gtk.Dialog(
            title="Go to Line",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(250, -1)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        total = tab.buffer.get_line_count()
        current = tab.buffer.get_iter_at_mark(
            tab.buffer.get_insert()).get_line() + 1

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.pack_start(Gtk.Label(label="Line:"), False, False, 0)

        spin = Gtk.SpinButton.new_with_range(1, total, 1)
        spin.set_value(current)
        spin.connect('activate', lambda _: dlg.response(Gtk.ResponseType.OK))
        row.pack_start(spin, True, True, 0)

        row.pack_start(Gtk.Label(label=f"/ {total}"), False, False, 0)

        btn_go = Gtk.Button(label="Go")
        btn_go.get_style_context().add_class('suggested-action')
        btn_go.connect('clicked', lambda _: dlg.response(Gtk.ResponseType.OK))
        row.pack_start(btn_go, False, False, 0)

        box.pack_start(row, False, False, 0)
        dlg.show_all()

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            line = int(spin.get_value()) - 1
            target = tab.buffer.get_iter_at_line(line)
            tab.buffer.place_cursor(target)
            tab.source_view.scroll_to_iter(target, 0.1, True, 0.0, 0.5)
            tab.source_view.grab_focus()
        dlg.destroy()

    # -- Docblock Generation ---------------------------------------------------

    def _try_expand_docblock(self, view):
        """If cursor is on a line containing only '/**', expand to a docblock.
        Returns True if expanded, False otherwise."""
        buf = view.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        line_num = cursor.get_line()

        # Get the current line text
        line_start = buf.get_iter_at_line(line_num)
        line_end = line_start.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()
        line_text = buf.get_text(line_start, line_end, False)

        # Check if line is just whitespace + /**
        stripped = line_text.strip()
        if stripped != '/**':
            return False

        # Get the indentation
        indent = line_text[:len(line_text) - len(line_text.lstrip())]

        # Get the file extension to determine language
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return False
        ext = self._get_file_ext(tab.remote_path)
        if ext not in ('php', 'js', 'jsx', 'ts', 'tsx'):
            return False

        # Read the next non-empty line to find the function signature
        total_lines = buf.get_line_count()
        func_line = None
        for i in range(line_num + 1, min(line_num + 5, total_lines)):
            next_start = buf.get_iter_at_line(i)
            next_end = next_start.copy()
            if not next_end.ends_line():
                next_end.forward_to_line_end()
            next_text = buf.get_text(next_start, next_end, False).strip()
            if next_text:
                func_line = next_text
                break

        if not func_line:
            return False

        # Parse the function signature
        if ext == 'php':
            docblock = self._generate_php_docblock(func_line, indent)
        else:
            docblock = self._generate_js_docblock(func_line, indent)

        if not docblock:
            return False

        # Replace the /** line with the full docblock
        buf.begin_user_action()
        buf.delete(line_start, line_end)
        buf.insert(line_start, docblock)
        buf.end_user_action()
        return True

    def _generate_php_docblock(self, func_line, indent):
        """Generate a PHP docblock from a function signature."""
        # Match: function name(params): returntype
        # or: public static function name(params): returntype
        m = re.match(
            r'(?:(?:public|private|protected|static|abstract|final)\s+)*'
            r'function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*(\S+))?',
            func_line.strip()
        )
        if not m:
            return None

        func_name = m.group(1)
        params_str = m.group(2).strip()
        return_type = m.group(3) or 'void'

        lines = [f'{indent}/**']
        lines.append(f'{indent} * {func_name}')
        lines.append(f'{indent} *')

        # Parse parameters
        if params_str:
            for param in params_str.split(','):
                param = param.strip()
                if not param:
                    continue
                # PHP param formats: Type $name, $name, Type $name = default
                parts = param.split('=')[0].strip().split()
                if len(parts) >= 2:
                    ptype = parts[-2].lstrip('?').lstrip('&')
                    pname = parts[-1]
                else:
                    ptype = 'mixed'
                    pname = parts[0]
                # Clean up $name
                pname = pname.lstrip('&').lstrip('.')
                if not pname.startswith('$'):
                    pname = '$' + pname
                lines.append(f'{indent} * @param {ptype} {pname}')

        lines.append(f'{indent} * @return {return_type}')
        lines.append(f'{indent} */')

        return '\n'.join(lines)

    def _generate_js_docblock(self, func_line, indent):
        """Generate a JSDoc block from a JS/TS function signature."""
        # Match various function forms:
        # function name(params) {
        # async function name(params) {
        # const name = (params) => {
        # name(params) {  (class method)
        # export function name(params): returntype {

        # Try function declaration
        m = re.match(
            r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)(?:\s*:\s*(\S+))?',
            func_line.strip()
        )
        if not m:
            # Try arrow function: const name = (params) =>
            m = re.match(
                r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?'
                r'\(([^)]*)\)(?:\s*:\s*(\S+))?\s*=>',
                func_line.strip()
            )
        if not m:
            # Try class method: name(params) {
            m = re.match(
                r'(?:(?:static|async|get|set|public|private|protected)\s+)*'
                r'(\w+)\s*\(([^)]*)\)(?:\s*:\s*(\S+))?\s*\{',
                func_line.strip()
            )
        if not m:
            return None

        func_name = m.group(1)
        params_str = m.group(2).strip()
        return_type = m.group(3)

        lines = [f'{indent}/**']
        lines.append(f'{indent} * {func_name}')
        lines.append(f'{indent} *')

        # Parse parameters
        if params_str:
            for param in params_str.split(','):
                param = param.strip()
                if not param:
                    continue
                # JS/TS param formats: name, name: type, name: type = default, ...name
                param = param.split('=')[0].strip()
                if ':' in param:
                    pname, ptype = param.split(':', 1)
                    pname = pname.strip().lstrip('.')
                    ptype = ptype.strip()
                else:
                    pname = param.lstrip('.')
                    ptype = '*'
                lines.append(f'{indent} * @param {{{ptype}}} {pname}')

        if return_type:
            lines.append(f'{indent} * @returns {{{return_type}}}')
        else:
            lines.append(f'{indent} * @returns {{*}}')
        lines.append(f'{indent} */')

        return '\n'.join(lines)

    def _on_editor_key_press(self, _view, event):
        """Intercept keys on the source view before GtkSourceView handles them."""
        # Tab on /** line → expand docblock
        if event.keyval == Gdk.KEY_Tab:
            if self._try_expand_docblock(_view):
                return True
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and event.keyval == Gdk.KEY_f:
            self._show_search(show_replace=False)
            return True
        if ctrl and event.keyval == Gdk.KEY_r:
            self._show_search(show_replace=True)
            return True
        if ctrl and event.keyval == Gdk.KEY_g:
            self._on_goto_line()
            return True
        if ctrl and event.keyval == Gdk.KEY_n:
            self._on_new_local_file()
            return True
        if ctrl and event.keyval == Gdk.KEY_o:
            self._on_open_local_file()
            return True
        if ctrl and event.keyval == Gdk.KEY_s:
            self._on_save(None)
            return True
        return False

    def _on_key_press(self, _widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK

        if ctrl and event.keyval == Gdk.KEY_s:
            self._on_save(None)
            return True
        elif ctrl and event.keyval == Gdk.KEY_n:
            self._on_new_local_file()
            return True
        elif ctrl and event.keyval == Gdk.KEY_o:
            self._on_open_local_file()
            return True
        elif ctrl and event.keyval == Gdk.KEY_w:
            page_num = self.notebook.get_current_page()
            if page_num in self.tabs:
                self._close_tab(page_num)
            return True
        elif ctrl and event.keyval == Gdk.KEY_q:
            self._on_quit(None)
            return True
        elif ctrl and event.keyval == Gdk.KEY_f:
            self._show_search(show_replace=False)
            return True
        elif ctrl and event.keyval == Gdk.KEY_r:
            self._show_search(show_replace=True)
            return True
        elif ctrl and event.keyval == Gdk.KEY_g:
            self._on_goto_line()
            return True
        elif event.keyval == Gdk.KEY_Escape:
            if self._search_window:
                self._on_search_close()
                return True
        return False

    # -- Cleanup --------------------------------------------------------------

    def _on_quit(self, _widget):
        # Check for unsaved changes
        unsaved = [t for t in self.tabs.values() if t.modified]
        if unsaved:
            names = ', '.join(os.path.basename(t.remote_path) for t in unsaved)
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Unsaved Changes",
            )
            dlg.format_secondary_text(f"Files with unsaved changes: {names}\n\nQuit anyway?")
            resp = dlg.run()
            dlg.destroy()
            if resp != Gtk.ResponseType.YES:
                return True  # prevent close

        # Save session before closing
        self._save_session()

        if self.ftp_mgr:
            self.ftp_mgr.disconnect()
        # Clean temp files
        import shutil
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass
        Gtk.main_quit()


# --- Entry Point -------------------------------------------------------------

def main():
    # Suppress all GTK/GLib warning and critical messages from stderr
    import ctypes
    try:
        libc = ctypes.CDLL("libglib-2.0.so.0")
        libc.g_log_set_always_fatal(0)
        # Install a no-op log handler for Gtk and GtkSourceView domains
        LOG_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))
        _noop_handler = LOG_FUNC(lambda *a: None)
        # Keep reference alive
        main._log_handler = _noop_handler
        libc.g_log_set_handler(b"Gtk", 0xFF, _noop_handler, None)
        libc.g_log_set_handler(b"GtkSourceView", 0xFF, _noop_handler, None)
    except Exception:
        pass

    import warnings
    warnings.filterwarnings('ignore')

    GLib.set_prgname("synpad")
    GLib.set_application_name("SynPad")
    Gdk.set_program_class("synpad")
    win = SynPadWindow()
    win.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
