"""SynPad editor mixin — tab management, save/upload, search, snippets."""

import hashlib
import json
import os
import re
import threading

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GtkSource, Gdk, GLib

from config import save_config, find_server_by_guid, CONFIG_DIR
from connection import FTPManager, SFTPManager
from completion import SynPadCompletionProvider, DocumentWordProvider, COMPLETION_LANGS
from symbols import SYMBOL_EXTENSIONS, parse_symbols, SYMBOL_ICONS
from tab import OpenTab


class EditorMixin:
    """Mixin for SynPadWindow — editor tabs, save, search, snippets."""

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

        # Track modification — reads tab.remote_path so renamed/saved tabs show correct name
        def on_modified_changed(_buf):
            name = os.path.basename(tab.remote_path)
            if buf.get_modified():
                tab_label.set_markup(f"<b>* {name}</b>")
                tab.modified = True
            else:
                tab_label.set_text(name)
                tab.modified = False

        buf.connect('modified-changed', on_modified_changed)

        # Close button — find the current page_num dynamically, not from closure
        def on_close(_btn):
            # Find the actual page number for this tab's scroll widget
            scroll_widget = tab.source_view.get_parent()
            current_page = self.notebook.page_num(scroll_widget)
            self._debug(f"Close X clicked: tab={os.path.basename(tab.remote_path)}, page_num={current_page}")
            if current_page >= 0:
                self._close_tab(current_page)

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
        self._debug(f"_close_tab: page_num={page_num}, tab={'found: ' + os.path.basename(tab.remote_path) if tab else 'NOT FOUND'}, tabs={list(self.tabs.keys())}")
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
            for old_num, tab in list(self.tabs.items()):
                scroll = tab.source_view.get_parent()
                if scroll is widget:
                    new_tabs[i] = tab
                    break
        self._debug(f"_reindex_tabs: {list(self.tabs.keys())} -> {list(new_tabs.keys())}")
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
        """Create a new untitled tab. File location chosen on first save."""
        # Find lowest available untitled number
        used = set()
        for tab in self.tabs.values():
            if tab.remote_path.startswith('Untitled '):
                try:
                    num = int(tab.remote_path.split(' ', 1)[1])
                    used.add(num)
                except ValueError:
                    pass
        n = 1
        while n in used:
            n += 1
        name = f"Untitled {n}"
        self._create_editor_tab(name, '', '', is_local=True)
        self.item_save.set_sensitive(True)

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

        # Remember last folder
        last_dir = self.config.get('last_save_dir', '')
        if last_dir and os.path.isdir(last_dir):
            dlg.set_current_folder(last_dir)

        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            filepath = dlg.get_filename()
            self.config['last_save_dir'] = os.path.dirname(filepath)
            save_config(self.config)
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

    def _update_tab_label(self, tab, new_name):
        """Update the tab label text for a given tab."""
        page_widget = tab.source_view.get_parent()  # ScrolledWindow
        tab_widget = self.notebook.get_tab_label(page_widget)  # EventBox
        if not tab_widget:
            return
        # Walk: EventBox -> Box -> find Label
        def _find_label(widget):
            if isinstance(widget, Gtk.Label):
                return widget
            if hasattr(widget, 'get_children'):
                for child in widget.get_children():
                    found = _find_label(child)
                    if found:
                        return found
            if hasattr(widget, 'get_child'):
                child = widget.get_child()
                if child:
                    return _find_label(child)
            return None

        label = _find_label(tab_widget)
        if label:
            if tab.modified:
                label.set_markup(f"<b>* {new_name}</b>")
            else:
                label.set_text(new_name)

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
        """Save a local file to disk. If untitled, ask where to save first."""
        # Untitled file — no path yet
        if not tab.local_path:
            dlg = Gtk.FileChooserDialog(
                title="Save As",
                transient_for=self,
                action=Gtk.FileChooserAction.SAVE,
            )
            dlg.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
            )
            dlg.set_do_overwrite_confirmation(True)
            dlg.set_current_name(tab.remote_path)  # "Untitled 1" etc.
            # Remember last save folder
            last_dir = self.config.get('last_save_dir', '')
            if last_dir and os.path.isdir(last_dir):
                dlg.set_current_folder(last_dir)

            resp = dlg.run()
            if resp == Gtk.ResponseType.OK:
                filepath = dlg.get_filename()
                # Save the folder for next time
                self.config['last_save_dir'] = os.path.dirname(filepath)
                save_config(self.config)
                dlg.destroy()
                tab.local_path = filepath
                tab.remote_path = filepath
                # Update tab label — walk EventBox > Box > children
                self._update_tab_label(tab, os.path.basename(filepath))
            else:
                dlg.destroy()
                return

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
                # Check if file was modified on server since we opened it
                no_stats = (tab.remote_mtime is None and tab.remote_hash is None)
                file_changed = False
                mtime_changed = False

                # Step 1: fast mtime check (informational)
                if tab.remote_mtime is not None:
                    current_mtime = mgr.get_remote_mtime(tab.remote_path)
                    if current_mtime and current_mtime > tab.remote_mtime:
                        self._console_log(f"COMPARE mtime changed: {tab.remote_mtime} -> {current_mtime}")
                        mtime_changed = True
                    else:
                        self._console_log(f"COMPARE mtime unchanged")

                # Step 2: definitive hash check — always runs
                remote_content = None
                if True:
                    # Download remote file to temp for hash and compare
                    self._console_log(f"COMPARE GET {tab.remote_path}")
                    remote_tmp = tab.local_path + '.remote_tmp'
                    try:
                        mgr.download(tab.remote_path, remote_tmp)
                        with open(remote_tmp, 'rb') as f:
                            remote_bytes = f.read()
                        current_hash = hashlib.sha256(remote_bytes).hexdigest()
                        remote_content = remote_bytes.decode('utf-8', errors='replace')
                        self._console_log(f"COMPARE remote hash: {current_hash[:16]}...")
                    except Exception as e:
                        current_hash = None
                        remote_content = None
                        self._console_log(f"COMPARE GET failed: {e}", 'error')
                    finally:
                        try:
                            os.unlink(remote_tmp)
                        except Exception:
                            pass

                    if current_hash and tab.remote_hash and current_hash != tab.remote_hash:
                        self._console_log(
                            f"COMPARE CHANGED — stored: {tab.remote_hash[:16]}... "
                            f"server: {current_hash[:16]}...", 'error')
                        file_changed = True
                    elif current_hash and tab.remote_hash and current_hash == tab.remote_hash:
                        self._console_log(f"COMPARE OK — hashes match", 'success')
                        file_changed = False
                    elif current_hash and no_stats:
                        # Session-restored tab: compare remote hash with local content hash
                        with open(tab.local_path, 'rb') as f:
                            local_hash = hashlib.sha256(f.read()).hexdigest()
                        self._console_log(
                            f"COMPARE session tab — local: {local_hash[:16]}... "
                            f"server: {current_hash[:16]}...")
                        if current_hash != local_hash:
                            self._console_log(
                                f"COMPARE CHANGED — files differ", 'error')
                            file_changed = True
                        else:
                            self._console_log(f"COMPARE OK — files match", 'success')
                            file_changed = False
                        # Store the hash now for future checks
                        tab.remote_hash = local_hash

                if file_changed:
                    import queue
                    # result: 'overwrite', 'use_remote', 'cancel', or 'compare'
                    result_q = queue.Queue()
                    # Get local content for potential compare
                    with open(tab.local_path, 'r', errors='replace') as f:
                        local_content = f.read()

                    RESP_OVERWRITE = 1
                    RESP_USE_REMOTE = 2
                    RESP_COMPARE = 3
                    RESP_CANCEL = 4

                    def _ask_overwrite():
                        dlg = Gtk.Dialog(
                            title="File Modified on Server",
                            transient_for=self,
                            modal=True,
                            use_header_bar=False,
                        )
                        dlg.set_default_size(450, -1)

                        box = dlg.get_content_area()
                        box.set_spacing(8)
                        box.set_margin_start(12)
                        box.set_margin_end(12)
                        box.set_margin_top(12)
                        box.set_margin_bottom(12)

                        # Warning icon + text
                        msg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                        icon = Gtk.Image.new_from_icon_name('dialog-warning-symbolic',
                                                            Gtk.IconSize.DIALOG)
                        msg_box.pack_start(icon, False, False, 0)

                        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                        title_lbl = Gtk.Label()
                        title_lbl.set_markup("<b>File Modified on Server</b>")
                        title_lbl.set_halign(Gtk.Align.START)
                        text_box.pack_start(title_lbl, False, False, 0)

                        desc_lbl = Gtk.Label(
                            label=f"'{os.path.basename(tab.remote_path)}' has been "
                                  f"modified on the server since you opened it.")
                        desc_lbl.set_halign(Gtk.Align.START)
                        desc_lbl.set_line_wrap(True)
                        text_box.pack_start(desc_lbl, False, False, 0)
                        msg_box.pack_start(text_box, True, True, 0)
                        box.pack_start(msg_box, False, False, 0)

                        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                                       False, False, 4)

                        # Buttons
                        btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

                        btn_overwrite = Gtk.Button(label="Overwrite server with my changes")
                        btn_overwrite.connect('clicked',
                            lambda _: [result_q.put(RESP_OVERWRITE), dlg.destroy()])
                        btn_box.pack_start(btn_overwrite, False, False, 0)

                        btn_remote = Gtk.Button(label="Discard my changes, use server version")
                        btn_remote.connect('clicked',
                            lambda _: [result_q.put(RESP_USE_REMOTE), dlg.destroy()])
                        btn_box.pack_start(btn_remote, False, False, 0)

                        btn_compare = Gtk.Button(label="Compare both versions")
                        btn_compare.get_style_context().add_class('suggested-action')
                        btn_compare.connect('clicked',
                            lambda _: [result_q.put(RESP_COMPARE), dlg.destroy()])
                        btn_box.pack_start(btn_compare, False, False, 0)

                        btn_cancel = Gtk.Button(label="Cancel")
                        btn_cancel.connect('clicked',
                            lambda _: [result_q.put(RESP_CANCEL), dlg.destroy()])
                        btn_box.pack_start(btn_cancel, False, False, 0)

                        box.pack_start(btn_box, False, False, 0)
                        dlg.connect('delete-event',
                            lambda *a: [result_q.put(RESP_CANCEL), True])
                        dlg.show_all()

                    GLib.idle_add(_ask_overwrite)
                    choice = result_q.get()

                    if choice == RESP_CANCEL:
                        GLib.idle_add(self.item_save.set_sensitive, True)
                        GLib.idle_add(self._set_status, "Upload cancelled")
                        self._console_log("PUT CANCELLED — server file was modified", 'error')
                        return
                    elif choice == RESP_USE_REMOTE:
                        # Replace local content with remote
                        if remote_content:
                            def _load_remote():
                                tab.buffer.begin_user_action()
                                tab.buffer.set_text(remote_content)
                                tab.buffer.end_user_action()
                                tab.buffer.set_modified(False)
                                tab.remote_hash = hashlib.sha256(
                                    remote_content.encode('utf-8')).hexdigest()
                                tab.remote_mtime = mgr.get_remote_mtime(tab.remote_path)
                                tab.remote_size = mgr.get_remote_size(tab.remote_path)
                                self._set_status(f"Loaded server version of {os.path.basename(tab.remote_path)}")
                                self._console_log(f"Loaded server version: {tab.remote_path}", 'success')
                                self.item_save.set_sensitive(True)
                            GLib.idle_add(_load_remote)
                        else:
                            GLib.idle_add(self.item_save.set_sensitive, True)
                        return
                    elif choice == RESP_COMPARE:
                        # Show diff, then ask again
                        if remote_content:
                            def _show_compare():
                                self._show_conflict_diff(
                                    tab, local_content, remote_content,
                                    page_num, max_mb, mgr)
                                self.item_save.set_sensitive(True)
                            GLib.idle_add(_show_compare)
                        else:
                            GLib.idle_add(self.item_save.set_sensitive, True)
                        return
                    # RESP_OVERWRITE falls through to upload

                mgr.upload(tab.remote_path, tab.local_path, max_mb)
                # Update stored stats after successful upload
                tab.remote_mtime = mgr.get_remote_mtime(tab.remote_path)
                tab.remote_size = mgr.get_remote_size(tab.remote_path)
                # Hash the uploaded content
                with open(tab.local_path, 'rb') as f:
                    tab.remote_hash = hashlib.sha256(f.read()).hexdigest()
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
            # Upload, and reload tree to the file's directory after upload completes
            self._pending_tree_reload = (vals, tab.remote_path)
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

        # If there's a pending tree reload (from server switch), navigate
        # to the directory containing the uploaded file
        if self._pending_tree_reload:
            vals, remote_path = self._pending_tree_reload
            self._pending_tree_reload = None
            file_dir = os.path.dirname(remote_path)
            if file_dir:
                self._load_tree_and_expand(file_dir, vals)
            else:
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

    # -- Search & Replace -----------------------------------------------------

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

    # -- Pretty Print ---------------------------------------------------------

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

    # -- Go to Line -----------------------------------------------------------

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

    def _try_expand_snippet(self, view):
        """Try to expand /// or /** on the current line. Returns True if expanded."""
        buf = view.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        line_num = cursor.get_line()

        line_start = buf.get_iter_at_line(line_num)
        line_end = line_start.copy()
        if not line_end.ends_line():
            line_end.forward_to_line_end()
        line_text = buf.get_text(line_start, line_end, False)
        stripped = line_text.strip()

        # /// -> separator line
        if stripped == '///':
            buf.begin_user_action()
            buf.delete(line_start, line_end)
            buf.insert(line_start,
                       '//-----------------------------------------------------------------------------+')
            buf.end_user_action()
            return True

        # /** -> docblock
        if stripped == '/**':
            return self._try_expand_docblock(view)

        return False

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
        # Tab on /// or /** line → expand snippet
        if event.keyval == Gdk.KEY_Tab:
            # Hide completion popup first so it doesn't consume the Tab
            completion = _view.get_completion()
            completion.hide()
            if self._try_expand_snippet(_view):
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
