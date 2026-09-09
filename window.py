"""SynPad main window — assembles all mixins into SynPadWindow.

GTK4 notes:

- `Gtk.Notebook` -> `Adw.TabView` + `Adw.TabBar` for the main editor tab
  strip (`self.notebook`). This is the Task 5 conversion the migration
  plan calls out: `Adw.TabView` addresses tabs by `Adw.TabPage` object,
  not integer index, so `self.tabs` is now keyed by `Adw.TabPage` instead
  of `page_num`. That eliminates the whole class of bug the v1.20.4
  `_reindex_tabs()` / `page-reordered` workaround existed to paper over
  (a drag reshuffling integer indices out from under `self.tabs`) — a
  `Adw.TabPage` key stays valid across reorders by construction, so that
  workaround is deleted, not ported (see editor.py's module docstring and
  task-5-report.md for the full accounting). Every module that reaches
  into `self.notebook`/`self.tabs` (editor.py, compare.py, remote.py,
  session.py, terminal_tab.py, claude_tab.py, local_files.py) was updated
  to the same addressing scheme as part of this task; none of them are on
  the migration plan's do-not-touch list (only completion.py,
  signature_help.py, git_history.py are).
- The console/Tools notebook (`self._console_notebook`, and
  `terminal_tab.py`'s per-terminal sub-tabs within it) is *not* part of
  this conversion — it has no reorder/close-by-drag bug to fix, `Gtk.
  Notebook` is still a real, working GTK4 widget (used unconverted
  elsewhere already, e.g. dialogs.py's Dark/Light Mode picker), and nothing
  in the migration plan calls for replacing it. Only the main editor tab
  strip moves to `Adw.TabView`.
- The hamburger menu converts `Gtk.Menu`/`Gtk.MenuItem`/`Gtk.CheckMenuItem`
  to a `Gio.Menu` model on `menu_btn` (a plain `Gtk.MenuButton`, so no
  manual `Gtk.PopoverMenu` needed — same as `remote.py`'s quick-connect
  menu) backed by `Gio.SimpleAction`s in the window's own ("win.") action
  group. Every item, label (including the shortcut hint text baked into
  the label itself, e.g. "New Local File  Ctrl+N") and the two checkable
  items' state are preserved exactly. Real key handling is untouched —
  it was never wired through these labels/accelerators, only through
  `_on_key_press`, which still does the same manual Ctrl+key dispatch.
  `self.item_save` is a `Gio.SimpleAction` now, not a `Gtk.MenuItem`, but
  every other module (editor.py, remote.py, compare.py, session.py) calls
  `self.item_save.set_sensitive(bool)` on it — rather than touch every
  call site, `set_sensitive` is aliased to `set_enabled` on the instance
  (the same pattern used for `self.header.set_subtitle` below).
- `Gtk.HeaderBar.set_title()`/`.set_subtitle()` are gone in GTK4 — the
  title area is a widget now (`set_title_widget`). `remote.py` and
  `editor.py` both call `self.header.set_subtitle(...)`; rather than
  change two already-ported modules for a header-bar-internals detail,
  `self.header` (still the real `Gtk.HeaderBar`, so `pack_start`/
  `pack_end` keep working unchanged) gets an `Adw.WindowTitle` as its
  title widget and a `set_subtitle` alias forwarding to it.
- `Gtk.Dialog` has an Escape-closes-with-`close`-signal built in;
  `Gtk.Window` (what `SynPadWindow` is, via `Gtk.ApplicationWindow`) does
  not, and titlebar-close raises `close-request`, not `destroy`. The old
  `self.connect('destroy', self._on_quit)` would not even reliably fire
  on a titlebar close in GTK4, and 'destroy' isn't cancelable in any case
  — so quitting is now driven by `close-request`, guarded by
  `self._quit_confirmed` so the confirm-then-actually-close continuation
  can call `self.close()` a second time (which re-raises close-request)
  without looping. The unsaved-changes confirmation itself is a one-shot
  decision with nothing chained on it (same shape as editor.py's
  `_force_highlight`/`_confirm_then_refresh`), so it's safe to use
  `Adw.AlertDialog` even though its `choose()` callback is known to never
  fire on Escape/close when the parent is a plain `Gtk.Window` — "Cancel"
  and "declined via Escape" both mean the same thing here (stay open),
  so a callback that silently never fires on Escape has no observable
  effect.
- The 3 raw GTK3 event-signal connections (`key-press-event` on the
  window, `button-press-event` on both tree views) become an
  `EventControllerKey`/`Gtk.GestureClick`, per the established pattern.
  The two tree right-click wire-ups are actually the two forward-note
  fixes from local_files.py/remote.py: `_local_attach_tree_controllers`/
  `_remote_attach_tree_controllers` replace the raw `button-press-event`
  connects outright.
- `Gtk.Paned.pack1`/`pack2` are gone — `set_start_child`/`set_end_child`
  plus separate `set_resize_*_child`/`set_shrink_*_child` calls replace
  the `resize=`/`shrink=` kwargs. `_apply_pane_layout`'s "detach whichever
  pane is currently attached, wherever it is" step needs a small
  `_paned_detach` helper since `Gtk.Paned` has no generic `.remove(w)`
  the way `Gtk.Box` still does.
- The 1 outstanding `get_iter_at_line()` site (`_console_log`'s 500-line
  trim) now unpacks the `(ok, iter)` tuple GTK4 returns.
- 4 `.run()` sites (`_prompt_reload_dirty_tab`, `_show_error`,
  `_show_info`, the unsaved-changes-on-quit confirm) all become
  `Adw.AlertDialog` + async `.choose()`; none of their callers depend on
  a return value, so no control-flow restructuring beyond that was
  needed.
"""

import os
import tempfile

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GtkSource, Gdk, GLib, Gio, Adw, Pango

from pathlib import Path

from config import APP_VERSION, load_config, save_config, CONFIG_DIR
from symbols import SYMBOL_EXTENSIONS, SYMBOL_ICONS, parse_symbols
from editor import EditorMixin
from remote import RemoteMixin
from local_files import LocalFilesMixin
from compare import CompareMixin
from dialogs import DialogsMixin
from session import SessionMixin
from signature_help import SignatureHelpMixin
from git_history import GitHistoryMixin
from terminal_tab import TerminalMixin
from claude_tab import ClaudeMixin


class SynPadWindow(Gtk.ApplicationWindow, EditorMixin, RemoteMixin, LocalFilesMixin,
                   CompareMixin, DialogsMixin, SessionMixin, SignatureHelpMixin,
                   GitHistoryMixin, TerminalMixin, ClaudeMixin):
    """Main application window."""

    def __init__(self, application=None):
        super().__init__(application=application, title="SynPad - PHP IDE")
        self.set_default_size(1200, 750)
        self.config = load_config()
        self.ftp_mgr = None  # set on connect (FTPManager or SFTPManager)
        self.current_server_guid = ''  # GUID of currently connected server
        self._pending_upload = None   # (tab, page, max_mb) for auto-switch
        self._pending_tree_reload = None  # vals dict for tree reload after upload
        self.tabs = {}  # Adw.TabPage -> OpenTab
        self._pending_close_callbacks = {}  # Adw.TabPage -> callback, for _close_tab chains
        self._quit_confirmed = False  # guards close-request re-entry from _do_quit's self.close()
        self.current_remote_dir = '/'
        self.tmp_dir = tempfile.mkdtemp(prefix='synpad_')
        self._tools_window = None  # set when Tools pane is detached

        self._build_ui()
        self._connect_signals()
        self._apply_css()
        self._apply_gtk_theme()
        self._restore_session()

    # -- Single-instance file opening -----------------------------------------

    def open_or_focus_file(self, filepath):
        """Open filepath as a tab. If already open, switch to that tab.
        If the matched tab is dirty, prompt the user to reload from disk."""
        target = os.path.realpath(os.path.abspath(filepath))

        for page, tab in self.tabs.items():
            if not tab.is_local or not tab.local_path:
                continue
            existing = os.path.realpath(os.path.abspath(tab.local_path))
            if existing == target:
                self.notebook.set_selected_page(page)
                if tab.modified:
                    self._prompt_reload_dirty_tab(tab, target)
                return

        self._open_local_file(target)

    def _prompt_reload_dirty_tab(self, tab, filepath):
        """Ask whether to reload from disk, discarding the buffer's unsaved
        edits. One-shot decision, nothing chained on it — Adw.AlertDialog
        is safe here (see module docstring)."""
        dlg = Adw.AlertDialog(
            heading="File has unsaved changes",
            body=f"{filepath}\n\nReload from disk and discard your unsaved changes?",
        )
        dlg.add_response('cancel', "Cancel")
        dlg.add_response('reload', "Reload (discard my changes)")
        dlg.set_default_response('cancel')
        dlg.set_close_response('cancel')

        def on_response(d, res):
            try:
                response = d.choose_finish(res)
            except Exception:
                return
            if response == 'reload':
                self._reload_tab_from_disk(tab)

        dlg.choose(self, None, on_response)

    def _reload_tab_from_disk(self, tab):
        """Replace the tab's buffer contents with the on-disk file, clearing dirty flag."""
        try:
            with open(tab.local_path, 'r', errors='replace') as f:
                content = f.read()
        except Exception as e:
            self._show_error("Reload Failed", str(e))
            return

        tab.buffer.set_text(content)
        tab.buffer.set_modified(False)

    # -- UI Construction ------------------------------------------------------

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(vbox)

        # Toolbar
        toolbar = Gtk.HeaderBar()
        toolbar.set_show_title_buttons(True)
        self._window_title = Adw.WindowTitle(
            title=f"SynPad v{APP_VERSION}", subtitle="Disconnected")
        toolbar.set_title_widget(self._window_title)
        self.set_titlebar(toolbar)
        self.header = toolbar
        # remote.py/editor.py call self.header.set_subtitle(...) — a real
        # method on Gtk.HeaderBar in GTK3, gone in GTK4. Alias it onto the
        # Adw.WindowTitle that now owns the title area instead of touching
        # every already-ported caller.
        self.header.set_subtitle = self._window_title.set_subtitle

        # Hamburger menu button
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name('open-menu-symbolic')
        menu_btn.add_css_class('flat')
        menu_btn.set_menu_model(self._build_menu_model())
        toolbar.pack_start(menu_btn)

        self.btn_theme = Gtk.Button()
        self.btn_theme.add_css_class('flat')
        self._update_theme_icon()
        self.btn_theme.set_tooltip_text("Toggle light/dark theme")
        toolbar.pack_end(self.btn_theme)

        self.btn_console = Gtk.Button()
        self.btn_console.set_icon_name('utilities-terminal-symbolic')
        self.btn_console.add_css_class('flat')
        self.btn_console.set_tooltip_text("Toggle console (F12)")
        toolbar.pack_end(self.btn_console)

        # --- Build the three panes as independent widgets ---

        # 1) Symbol / function list pane
        self.symbol_pane = self._make_pane_wrapper('symbols', 'Functions')
        self.btn_refresh_symbols = Gtk.Button()
        self.btn_refresh_symbols.set_icon_name('view-refresh-symbolic')
        self.btn_refresh_symbols.add_css_class('flat')
        self.btn_refresh_symbols.set_tooltip_text("Refresh symbol list")
        self.btn_refresh_symbols.connect('clicked', self._on_refresh_symbols)
        sym_header = self.symbol_pane._header_box
        # Insert just before the "move pane right" arrow button, matching
        # the old pack_end() call's visual position (see the "reversed
        # pack_end order" note on the Tools header buttons below for why
        # this isn't just another append()).
        sym_header.insert_child_after(
            self.btn_refresh_symbols, sym_header.get_first_child().get_next_sibling())

        self.symbol_scroll = Gtk.ScrolledWindow()
        self.symbol_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.symbol_scroll.set_size_request(180, -1)

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

        self.symbol_view.add_css_class('symbol-pane')
        self.symbol_view.connect('row-activated', self._on_symbol_activated)
        self.symbol_scroll.set_child(self.symbol_view)
        self.symbol_pane.append(self.symbol_scroll)
        self.symbol_scroll.set_vexpand(True)

        # 2) Editor (Adw.TabView + Adw.TabBar)
        self.editor_pane = self._make_pane_wrapper('editor', 'Editor')
        self.notebook = Adw.TabView()
        self.notebook.set_size_request(300, -1)
        self.notebook.set_vexpand(True)
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.notebook)
        # GtkNotebook always showed its tab strip even with a single page;
        # Adw.TabBar's default autohide would hide it in that case instead.
        self.tab_bar.set_autohide(False)
        self._add_welcome_tab()
        self.editor_pane.append(self.tab_bar)
        self.editor_pane.append(self.notebook)

        # 3) File tree — with local/remote toggle and connection controls
        self.files_pane = self._make_pane_wrapper('files', 'Files')

        # --- Toggle buttons: Remote / Local ---
        toggle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toggle_row.set_margin_start(4)
        toggle_row.set_margin_end(4)
        toggle_row.set_margin_bottom(2)

        self.btn_remote_tree = Gtk.ToggleButton()
        remote_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        remote_icon = Gtk.Image.new_from_icon_name('network-server-symbolic')
        remote_icon.set_pixel_size(16)
        remote_btn_box.append(remote_icon)
        remote_btn_box.append(Gtk.Label(label="Remote"))
        self.btn_remote_tree.set_child(remote_btn_box)
        self.btn_remote_tree.set_active(True)
        self.btn_remote_tree.add_css_class('flat')
        self.btn_remote_tree.set_tooltip_text("Show remote server files")
        toggle_row.append(self.btn_remote_tree)
        self.btn_remote_tree.set_hexpand(True)

        self.btn_local_tree = Gtk.ToggleButton()
        local_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        local_icon = Gtk.Image.new_from_icon_name('drive-harddisk-symbolic')
        local_icon.set_pixel_size(16)
        local_btn_box.append(local_icon)
        local_btn_box.append(Gtk.Label(label="Local"))
        self.btn_local_tree.set_child(local_btn_box)
        self.btn_local_tree.set_active(False)
        self.btn_local_tree.add_css_class('flat')
        self.btn_local_tree.set_tooltip_text("Show local files")
        toggle_row.append(self.btn_local_tree)
        self.btn_local_tree.set_hexpand(True)

        self.files_pane.append(toggle_row)

        # --- Stack to switch between remote and local trees ---
        self._file_stack = Gtk.Stack()
        self._file_stack.set_transition_type(Gtk.StackTransitionType.NONE)

        # --- Remote tree ---
        remote_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        server_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        server_row.set_margin_start(4)
        server_row.set_margin_end(4)
        server_row.set_margin_bottom(2)

        self.quick_btn = Gtk.MenuButton(label="Quick Connect")
        self.quick_btn.set_tooltip_text("Quick connect to saved server")
        self.quick_btn.add_css_class('flat')
        self._rebuild_quick_menu()
        server_row.append(self.quick_btn)
        self.quick_btn.set_hexpand(True)
        remote_box.append(server_row)

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

        self.scroll_tree.set_child(self.tree_view)
        # 12px gutter so the root-level expander arrows clear Gtk.Paned's
        # divider grab area, which is wider than the visible handle and was
        # swallowing presses on the first ~10px of the tree.
        #
        # The margin goes on the ScrolledWindow, NOT the TreeView: inside the
        # viewport it would add to the tree's requested width and force a
        # permanent horizontal scrollbar even with everything collapsed.
        self.scroll_tree.set_margin_start(12)
        self.scroll_tree.set_vexpand(True)
        remote_box.append(self.scroll_tree)

        conn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        conn_row.set_margin_start(4)
        conn_row.set_margin_end(4)
        conn_row.set_margin_bottom(4)

        self.btn_connect = Gtk.Button()
        self.btn_connect.set_icon_name('network-server-symbolic')
        self.btn_connect.add_css_class('flat')
        self.btn_connect.set_tooltip_text("Connect to server")
        conn_row.append(self.btn_connect)

        self.btn_disconnect = Gtk.Button()
        self.btn_disconnect.set_icon_name('network-offline-symbolic')
        self.btn_disconnect.add_css_class('flat')
        self.btn_disconnect.set_tooltip_text("Disconnect")
        self.btn_disconnect.set_sensitive(False)
        conn_row.append(self.btn_disconnect)

        self.btn_refresh = Gtk.Button()
        self.btn_refresh.set_icon_name('view-refresh-symbolic')
        self.btn_refresh.add_css_class('flat')
        self.btn_refresh.set_tooltip_text("Refresh file tree")
        self.btn_refresh.set_sensitive(False)
        conn_row.append(self.btn_refresh)

        remote_box.append(conn_row)
        self._file_stack.add_named(remote_box, 'remote')

        # --- Local file tree ---
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        local_path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        local_path_row.set_margin_start(4)
        local_path_row.set_margin_end(4)
        local_path_row.set_margin_bottom(2)

        btn_local_up = Gtk.Button()
        btn_local_up.set_icon_name('go-up-symbolic')
        btn_local_up.add_css_class('flat')
        btn_local_up.set_tooltip_text("Go to parent directory")
        btn_local_up.connect('clicked', self._on_local_up)
        local_path_row.append(btn_local_up)

        self._local_path_entry = Gtk.Entry()
        self._local_path_entry.set_text(str(Path.home()))
        self._local_path_entry.set_tooltip_text("Type a path and press Enter")
        self._local_path_entry.connect('activate', self._on_local_path_enter)
        local_path_row.append(self._local_path_entry)
        self._local_path_entry.set_hexpand(True)

        local_box.append(local_path_row)

        self._local_scroll = Gtk.ScrolledWindow()
        self._local_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._local_store = Gtk.TreeStore(str, str, str, bool, bool)
        self._local_view = Gtk.TreeView(model=self._local_store)
        self._local_view.set_headers_visible(False)

        local_col = Gtk.TreeViewColumn("Files")
        local_icon_r = Gtk.CellRendererPixbuf()
        local_text = Gtk.CellRendererText()
        local_col.pack_start(local_icon_r, False)
        local_col.pack_start(local_text, True)
        local_col.add_attribute(local_icon_r, 'icon-name', 1)
        local_col.add_attribute(local_text, 'text', 0)
        self._local_view.append_column(local_col)

        self._local_view.connect('row-activated', self._on_local_tree_activated)
        self._local_view.connect('row-expanded', self._on_local_tree_expanded)
        self._local_attach_tree_controllers(self._local_view)

        self._local_scroll.set_child(self._local_view)
        self._local_scroll.set_margin_start(12)  # see scroll_tree above
        self._local_scroll.set_vexpand(True)
        local_box.append(self._local_scroll)

        local_action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        local_action_row.set_margin_start(4)
        local_action_row.set_margin_end(4)
        local_action_row.set_margin_bottom(4)

        btn_local_refresh = Gtk.Button()
        btn_local_refresh.set_icon_name('view-refresh-symbolic')
        btn_local_refresh.add_css_class('flat')
        btn_local_refresh.set_tooltip_text("Refresh local files")
        btn_local_refresh.connect('clicked', self._on_local_refresh)
        local_action_row.append(btn_local_refresh)

        btn_local_home = Gtk.Button()
        btn_local_home.set_icon_name('go-home-symbolic')
        btn_local_home.add_css_class('flat')
        btn_local_home.set_tooltip_text("Go to home directory")
        btn_local_home.connect('clicked', self._on_local_home)
        local_action_row.append(btn_local_home)

        local_box.append(local_action_row)
        self._file_stack.add_named(local_box, 'local')

        self._file_stack.set_visible_child_name('remote')
        self._file_stack.set_vexpand(True)
        self.files_pane.append(self._file_stack)

        # Map pane IDs to widgets
        self._pane_widgets = {
            'symbols': self.symbol_pane,
            'editor':  self.editor_pane,
            'files':   self.files_pane,
        }

        # Two persistent Paneds: outer(child1, inner(child2, child3))
        self._inner_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._outer_paned.set_end_child(self._inner_paned)
        self._outer_paned.set_resize_end_child(True)
        self._outer_paned.set_shrink_end_child(True)

        # Vertical paned: top = editor panes, bottom = console
        self._main_vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._main_vpaned.set_start_child(self._outer_paned)
        self._main_vpaned.set_resize_start_child(True)
        self._main_vpaned.set_shrink_start_child(True)

        # Console pane
        console_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        console_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        console_header.set_margin_start(6)
        console_header.set_margin_end(6)
        console_header.set_margin_top(2)
        console_header.set_margin_bottom(2)

        lbl = Gtk.Label()
        lbl.set_markup("<b>Tools</b>")
        console_header.append(lbl)
        # The other half of the pack_end->append conversion rule (see
        # _make_pane_wrapper's lbl.set_hexpand(True), the working
        # precedent): without this, the label claims only its natural
        # width and the four buttons appended after it bunch up right
        # next to it instead of sitting flush against the right edge.
        lbl.set_hexpand(True)

        btn_clear_console = Gtk.Button()
        btn_clear_console.set_icon_name('edit-clear-symbolic')
        btn_clear_console.add_css_class('flat')
        btn_clear_console.set_tooltip_text("Clear console")
        btn_clear_console.connect('clicked', self._on_clear_console)

        # New-terminal button (only present when VTE is available)
        self._terminal_init()
        add_term_btn = self._terminal_make_add_button()

        # Stop-Claude button (hidden until streaming)
        stop_btn = self._claude_make_stop_button()

        # Detach / re-attach Tools pane
        self._tools_dock_btn = Gtk.Button()
        self._tools_dock_btn.set_icon_name('view-fullscreen-symbolic')
        self._tools_dock_btn.add_css_class('flat')
        self._tools_dock_btn.set_tooltip_text("Detach Tools pane to its own window")
        self._tools_dock_btn.connect('clicked', self._on_toggle_tools_dock)

        # GTK3's repeated pack_end() calls each land closer to the center
        # than the one before, so the original call order (clear, add_term,
        # stop, tools_dock) rendered right-to-left as [tools_dock, stop,
        # add_term, clear] with clear at the true edge. append() has no
        # such reversal, so appending in that same final order reproduces
        # the old layout exactly.
        console_header.append(self._tools_dock_btn)
        console_header.append(stop_btn)
        if add_term_btn is not None:
            console_header.append(add_term_btn)
        console_header.append(btn_clear_console)

        console_box.append(console_header)
        console_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Console tab
        self._console_scroll = Gtk.ScrolledWindow()
        self._console_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._console_buffer = Gtk.TextBuffer()
        self._console_view = Gtk.TextView(buffer=self._console_buffer)
        self._console_view.set_editable(False)
        self._console_view.set_cursor_visible(False)
        self._console_view.set_monospace(True)
        self._console_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._console_view.add_css_class('console-view')
        self._console_buffer.create_tag('timestamp', foreground='#888888')
        self._console_buffer.create_tag('error', foreground='#ef2929')
        self._console_buffer.create_tag('success', foreground='#8ae234')
        self._console_scroll.set_child(self._console_view)

        # Git History tab
        git_scroll = Gtk.ScrolledWindow()
        git_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._git_history_buffer = Gtk.TextBuffer()
        git_view = Gtk.TextView(buffer=self._git_history_buffer)
        git_view.set_editable(False)
        git_view.set_cursor_visible(False)
        git_view.set_monospace(True)
        git_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        git_view.add_css_class('console-view')
        self._git_history_buffer.create_tag(
            'git_header', foreground='#888888', weight=Pango.Weight.BOLD)
        self._git_history_buffer.create_tag('git_hash', foreground='#f57c00')
        self._git_history_buffer.create_tag('git_date', foreground='#8ae234')
        self._git_history_buffer.create_tag('git_author', foreground='#2196F3')
        self._git_history_buffer.create_tag('error', foreground='#ef2929')
        self._git_history_buffer.create_tag('timestamp', foreground='#888888')
        self._git_history_buffer.create_tag('git_hover', paragraph_background='#3c4858')
        git_scroll.set_child(git_view)
        self._git_attach_click_handler(git_view)

        # Claude tab
        claude_scroll = Gtk.ScrolledWindow()
        claude_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        claude_buffer = Gtk.TextBuffer()
        claude_view = Gtk.TextView(buffer=claude_buffer)
        claude_view.set_editable(False)
        claude_view.set_cursor_visible(False)
        claude_view.set_monospace(True)
        claude_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        claude_view.add_css_class('console-view')
        claude_buffer.create_tag(
            'claude_header_you', foreground='#888888', weight=Pango.Weight.BOLD)
        claude_buffer.create_tag(
            'claude_header_claude', foreground='#2196F3', weight=Pango.Weight.BOLD)
        claude_buffer.create_tag('claude_dim', foreground='#999999')
        claude_buffer.create_tag('error', foreground='#ef2929')
        claude_scroll.set_child(claude_view)
        self._claude_attach_view(claude_view, claude_buffer)

        self._console_notebook = Gtk.Notebook()
        self._console_notebook.set_scrollable(False)
        self._console_notebook.append_page(
            self._console_scroll,
            self._make_tab_label_icon('utilities-terminal-symbolic', "Console"))
        self._console_notebook.append_page(
            git_scroll,
            self._make_tab_label_file(
                os.path.join(os.path.dirname(__file__), 'icons', 'git.svg'),
                "Git History"))
        self._console_notebook.append_page(
            claude_scroll,
            self._make_tab_label_file(
                os.path.join(os.path.dirname(__file__), 'icons', 'claude.svg'),
                "Claude"))
        console_box.append(self._console_notebook)
        self._console_notebook.set_vexpand(True)

        self._console_pane = console_box
        self._main_vpaned.set_end_child(self._console_pane)
        self._main_vpaned.set_resize_end_child(False)
        self._main_vpaned.set_shrink_end_child(True)

        vbox.append(self._main_vpaned)
        self._main_vpaned.set_vexpand(True)

        # Console starts hidden
        self._console_visible = False
        self._console_pane.set_visible(False)

        # Apply saved order
        self._apply_pane_layout()

        # Search window (created on demand)
        self._search_window = None

        # Status bar
        self.statusbar = Gtk.Statusbar()
        vbox.append(self.statusbar)
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
            background-color: @window_bg_color;
            padding: 2px 0px;
        }
        .pane-header:hover {
            background-color: alpha(@window_fg_color, 0.08);
        }
        .console-view {
            font-family: "Source Code Pro", "DejaVu Sans Mono", "Consolas", monospace;
            font-size: 11px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
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
        header_box.add_css_class('pane-header')
        wrapper._header_box = header_box  # symbol pane's refresh button is
                                           # inserted into this after the fact

        btn_left = Gtk.Button()
        btn_left.set_icon_name('go-previous-symbolic')
        btn_left.add_css_class('flat')
        btn_left.set_tooltip_text("Move pane left")
        btn_left.connect('clicked', self._on_move_pane, pane_id, -1)
        header_box.append(btn_left)

        lbl = Gtk.Label()
        lbl.set_markup(f"<b>{title}</b>")
        header_box.append(lbl)
        lbl.set_hexpand(True)

        btn_right = Gtk.Button()
        btn_right.set_icon_name('go-next-symbolic')
        btn_right.add_css_class('flat')
        btn_right.set_tooltip_text("Move pane right")
        btn_right.connect('clicked', self._on_move_pane, pane_id, +1)
        header_box.append(btn_right)

        wrapper.append(header_box)
        wrapper.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
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
        GLib.idle_add(self._apply_pane_layout)

    def _paned_detach(self, paned, w):
        """Detach `w` from whichever side of `paned` it's on. Gtk.Paned has
        no generic .remove(child) in GTK4 (unlike Gtk.Box) — pack1/pack2
        are gone too, replaced by set_start_child/set_end_child."""
        if paned.get_start_child() is w:
            paned.set_start_child(None)
        elif paned.get_end_child() is w:
            paned.set_end_child(None)

    def _apply_pane_layout(self):
        """Place panes into the two persistent Paneds based on config order."""
        order = self.config.get('pane_order', ['symbols', 'editor', 'files'])

        for pane_id in self._pane_widgets:
            w = self._pane_widgets[pane_id]
            parent = w.get_parent()
            if parent is self._outer_paned:
                self._paned_detach(self._outer_paned, w)
            elif parent is self._inner_paned:
                self._paned_detach(self._inner_paned, w)

        left_w = self._pane_widgets[order[0]]
        mid_w = self._pane_widgets[order[1]]
        right_w = self._pane_widgets[order[2]]

        self._outer_paned.set_start_child(left_w)
        self._outer_paned.set_resize_start_child(False)
        self._outer_paned.set_shrink_start_child(True)
        self._inner_paned.set_start_child(mid_w)
        self._inner_paned.set_resize_start_child(True)
        self._inner_paned.set_shrink_start_child(True)
        self._inner_paned.set_end_child(right_w)
        self._inner_paned.set_resize_end_child(False)
        self._inner_paned.set_shrink_end_child(True)

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

    # -- Hamburger Menu ---------------------------------------------------------

    def _build_menu_model(self):
        """Build the hamburger menu's Gio.Menu model and Gio.SimpleActions
        (installed on the window's own "win." action group). See module
        docstring for the overall Gtk.Menu -> Gio.Menu rationale."""

        def add_simple(name, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda _a, _p: callback())
            self.add_action(action)

        add_simple('new_local_file', self._on_new_local_file)
        add_simple('open_local_file', self._on_open_local_file)

        self.item_save = Gio.SimpleAction.new('save', None)
        # Every other module calls .set_sensitive(bool) on self.item_save,
        # matching the old Gtk.MenuItem API — alias rather than touch them.
        self.item_save.set_sensitive = self.item_save.set_enabled
        self.item_save.set_sensitive(False)
        self.item_save.connect('activate', lambda _a, _p: self._on_save(None))
        self.add_action(self.item_save)

        add_simple('find', lambda: self._show_search(show_replace=False))
        add_simple('replace', lambda: self._show_search(show_replace=True))
        add_simple('goto_line', self._on_goto_line)

        add_simple('pretty_json', self._on_pretty_print_json)
        add_simple('pretty_xml', self._on_pretty_print_xml)
        add_simple('compare_tabs', self._on_compare_tabs)

        add_simple('color_scheme', lambda: self._on_pick_scheme(None))
        add_simple('custom_colors', lambda: self._on_custom_colors(None))

        add_simple('server_manager', lambda: self._on_open_settings(None))
        add_simple('file_types', lambda: self._on_edit_file_types(None))

        self._show_hidden_action = Gio.SimpleAction.new_stateful(
            'show_hidden_files', None,
            GLib.Variant.new_boolean(self.config.get('show_hidden_files', False)))
        self._show_hidden_action.connect('activate', self._on_toggle_show_hidden)
        self.add_action(self._show_hidden_action)

        add_simple('ask_claude', self._claude_handle_trigger)

        self._debug_action = Gio.SimpleAction.new_stateful(
            'debug_mode', None, GLib.Variant.new_boolean(False))
        self._debug_action.connect('activate', self._on_toggle_debug)
        self.add_action(self._debug_action)

        add_simple('quit', lambda: self._on_quit(None))

        menu = Gio.Menu()

        sec1 = Gio.Menu()
        sec1.append("New Local File  Ctrl+N", 'win.new_local_file')
        sec1.append("Open Local File  Ctrl+O", 'win.open_local_file')
        sec1.append("Save  Ctrl+S", 'win.save')
        menu.append_section(None, sec1)

        sec2 = Gio.Menu()
        sec2.append("Find  Ctrl+F", 'win.find')
        sec2.append("Find & Replace  Ctrl+R", 'win.replace')
        sec2.append("Go to Line  Ctrl+G", 'win.goto_line')
        menu.append_section(None, sec2)

        sec3 = Gio.Menu()
        sec3.append("Pretty Print JSON", 'win.pretty_json')
        sec3.append("Pretty Print XML", 'win.pretty_xml')
        sec3.append("Compare Tabs", 'win.compare_tabs')
        menu.append_section(None, sec3)

        sec4 = Gio.Menu()
        sec4.append("Color Scheme", 'win.color_scheme')
        sec4.append("Custom Colors", 'win.custom_colors')
        menu.append_section(None, sec4)

        sec5 = Gio.Menu()
        sec5.append("Server Manager", 'win.server_manager')
        sec5.append("File Types", 'win.file_types')
        sec5.append("Show Hidden Files (local tree)", 'win.show_hidden_files')
        sec5.append("Ask Claude...  Ctrl+Shift+A", 'win.ask_claude')
        sec5.append("Debug Mode", 'win.debug_mode')
        menu.append_section(None, sec5)

        sec6 = Gio.Menu()
        sec6.append("Quit  Ctrl+Q", 'win.quit')
        menu.append_section(None, sec6)

        self._menu_model = menu  # kept for introspection (e.g. tests); not required by any caller
        return menu

    # -- Color Scheme Helpers -------------------------------------------------

    def _get_active_custom_colors(self):
        """Return the custom colors dict for the current theme mode."""
        if self.config.get('dark_theme', True):
            return self.config.get('custom_colors_dark', {})
        return self.config.get('custom_colors_light', {})

    def _get_scheme(self):
        """Return the GtkSource scheme, applying custom color overrides."""
        custom = self._get_active_custom_colors()
        if custom:
            return self._build_custom_scheme()
        mgr = GtkSource.StyleSchemeManager.get_default()
        scheme_id = self.config.get('color_scheme', 'oblivion')
        return mgr.get_scheme(scheme_id) or mgr.get_scheme('classic')

    def _build_custom_scheme(self):
        """Generate a custom GtkSourceView scheme XML from config overrides."""
        base_id = self.config.get('color_scheme', 'oblivion')
        custom = self._get_active_custom_colors()

        mgr = GtkSource.StyleSchemeManager.get_default()
        base = mgr.get_scheme(base_id)

        lines = ['<?xml version="1.0" encoding="UTF-8"?>']
        lines.append(f'<style-scheme id="synpad-custom" name="SynPad Custom" version="1.0">')
        lines.append(f'  <author>SynPad</author>')
        lines.append(f'  <description>Custom colors based on {base_id}</description>')

        if base:
            if 'text' not in custom:
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
                if default_style not in custom:
                    style = base.get_style(default_style)
                    if style:
                        parts = self._style_to_attrs(style)
                        if parts:
                            lines.append(f'  <style name="{default_style}" {" ".join(parts)}/>')

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

        has_search_match = any('search-match' in l for l in lines)
        if not has_search_match:
            lines.append('  <style name="search-match" foreground="#000000" background="#ffff00"/>')

        lines.append('</style-scheme>')

        xml_path = os.path.join(CONFIG_DIR, 'synpad-custom.xml')
        with open(xml_path, 'w') as f:
            f.write('\n'.join(lines))

        search_paths = list(mgr.get_search_path())
        if CONFIG_DIR not in search_paths:
            search_paths.insert(0, CONFIG_DIR)
            mgr.set_search_path(search_paths)
        mgr.force_rescan()

        return mgr.get_scheme('synpad-custom')

    @staticmethod
    def _is_valid_color(val):
        if not val:
            return False
        rgba = Gdk.RGBA()
        return rgba.parse(val)

    def _style_to_attrs(self, style):
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
            self.btn_theme.set_icon_name('weather-clear-symbolic')
        else:
            self.btn_theme.set_icon_name('weather-clear-night-symbolic')

    # -- Signals --------------------------------------------------------------

    def _connect_signals(self):
        self.connect('close-request', self._on_close_request)
        key_ctrl = Gtk.EventControllerKey()
        # CAPTURE, not the default BUBBLE: GTK4 dispatches BUBBLE bottom-up
        # from the focus widget, so a target-widget binding wins before it
        # ever reaches a toplevel-level BUBBLE handler. Concretely,
        # GtkSourceView/GtkTextView's own built-in ShortcutController binds
        # <Shift><Control>a to select-all(FALSE), which always returns
        # TRUE — with this controller at BUBBLE, Ctrl+Shift+A ("Ask
        # Claude") was silently swallowed by the view's own select-all
        # whenever the editor had focus. The same BUBBLE ordering let
        # Vte.Terminal's own key controller swallow F12/Ctrl+W/Ctrl+Q/
        # Ctrl+S while a terminal had focus. CAPTURE runs this controller
        # top-down, before any descendant widget's own key handling —
        # exactly how GTK3's `self.connect('key-press-event', ...)` on the
        # toplevel behaved (raw key events land on the toplevel first;
        # only the *default* class handler, which runs after a plain
        # connect()'d handler, forwards to the focus widget).
        #
        # This intentionally now also runs *before* editor.py's own
        # CAPTURE-phase controller on the source view (added directly to
        # the view, so it fires after this one, being the nested
        # descendant) for the keys they both handle (Ctrl+F/R/G/N/O/S):
        # both call the identical underlying methods, so whichever one
        # gets there first is not an observable difference. Keys this
        # handler does not recognize (Tab-to-expand-snippet, plain
        # typing, Ctrl+C/D and everything else a terminal needs) fall
        # through with `return False`, so CAPTURE continues on down to
        # the view/terminal's own handling exactly as before — verified
        # by tests/test_window_gtk4.py driving both a source view and a
        # Vte.Terminal.
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_key_press)
        self.add_controller(key_ctrl)
        # Kept as an attribute so tests can identify *this* controller
        # specifically, rather than asserting "any CAPTURE-phase
        # EventControllerKey exists on the window" — GtkWindow ships its
        # own unnamed CAPTURE-phase EventControllerKey (for mnemonics),
        # so that broader assertion would pass whether or not this fix
        # is actually present (fix round 2 finding).
        self._key_ctrl = key_ctrl
        self.btn_connect.connect('clicked', self._on_connect)
        self.btn_disconnect.connect('clicked', self._on_disconnect)
        self.btn_refresh.connect('clicked', self._on_refresh)
        self.btn_remote_tree.connect('toggled', self._on_toggle_file_view, 'remote')
        self.btn_local_tree.connect('toggled', self._on_toggle_file_view, 'local')
        self.btn_theme.connect('clicked', self._on_toggle_theme)
        self.btn_console.connect('clicked', self._on_toggle_console)
        self.tree_view.connect('row-activated', self._on_tree_row_activated)
        self.tree_view.connect('row-expanded', self._on_tree_row_expanded)
        self._remote_attach_tree_controllers(self.tree_view)
        self.notebook.connect('notify::selected-page', self._on_tab_switched)
        self.notebook.connect('close-page', self._on_tab_close_page)
        self.notebook.connect('setup-menu', self._on_tab_setup_menu)

    # -- Status ---------------------------------------------------------------

    def _set_status(self, msg):
        ctx = self.statusbar.get_context_id('main')
        self.statusbar.pop(ctx)
        self.statusbar.push(ctx, msg)

    def _show_error(self, title, msg):
        dlg = Adw.AlertDialog(heading=title, body=msg)
        dlg.add_response('ok', "OK")
        dlg.set_default_response('ok')
        dlg.set_close_response('ok')
        dlg.choose(self, None, lambda d, r: None)

    def _show_info(self, title, msg):
        dlg = Adw.AlertDialog(heading=title, body=msg)
        dlg.add_response('ok', "OK")
        dlg.set_default_response('ok')
        dlg.set_close_response('ok')
        dlg.choose(self, None, lambda d, r: None)

    # -- Utility --------------------------------------------------------------

    def _get_file_ext(self, filepath):
        return filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''

    def _is_editor_file(self, filepath):
        name = os.path.basename(filepath).lower()
        if name in ('dockerfile', 'makefile', 'vagrantfile', 'gemfile',
                     '.gitignore', '.htaccess', '.env'):
            return True
        ext = self._get_file_ext(filepath)
        if not ext:
            return True
        return ext in self.config.get('editor_extensions', [])

    def _open_external(self, filepath):
        import subprocess
        try:
            subprocess.Popen(['xdg-open', filepath],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._console_log(f"EXTERNAL {filepath}")
        except Exception as e:
            self._show_error("Open Failed", str(e))

    # -- Symbol Pane ----------------------------------------------------------

    def _update_symbols(self, tab):
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

    def _on_tab_switched(self, view, _pspec):
        page = view.get_selected_page()
        tab = self.tabs.get(page)
        self._update_symbols(tab)

    def _on_refresh_symbols(self, _btn):
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        self._update_symbols(tab)

    def _on_symbol_activated(self, _view, path, _col):
        tree_iter = self.symbol_store.get_iter(path)
        offset = self.symbol_store[tree_iter][3]
        if offset <= 0 and self.symbol_store[tree_iter][2] <= 0:
            return
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if not tab:
            return
        buf = tab.buffer
        target_iter = buf.get_iter_at_offset(offset)
        target_iter.set_line_offset(0)
        buf.place_cursor(target_iter)
        tab.source_view.grab_focus()

        def _do_scroll():
            insert_mark = buf.get_insert()
            tab.source_view.scroll_to_mark(insert_mark, 0.0, True, 0.0, 0.5)
            return False

        GLib.idle_add(_do_scroll)

    # -- Console --------------------------------------------------------------

    def _on_toggle_debug(self, action, _param):
        import config
        new_state = not action.get_state().get_boolean()
        action.set_state(GLib.Variant.new_boolean(new_state))
        config.DEBUG_MODE = new_state
        if config.DEBUG_MODE:
            self._console_log("Debug mode ON", 'success')
            if not self._console_visible:
                self._on_toggle_console()
        else:
            self._console_log("Debug mode OFF")

    def _on_toggle_show_hidden(self, action, _param):
        new_state = not action.get_state().get_boolean()
        action.set_state(GLib.Variant.new_boolean(new_state))
        self.config['show_hidden_files'] = new_state
        save_config(self.config)
        # Reload the local tree at its current root
        current = self._local_path_entry.get_text().strip()
        if current and os.path.isdir(current):
            self._load_local_tree(current)

    def _on_toggle_console(self, *_args):
        # If detached, F12 toggles the detached window's visibility
        if self._tools_window is not None:
            if self._tools_window.is_visible():
                self._tools_window.set_visible(False)
                self._console_visible = False
            else:
                self._tools_window.set_visible(True)
                self._tools_window.present()
                self._console_visible = True
            return
        self._console_visible = not self._console_visible
        if self._console_visible:
            self._console_pane.set_visible(True)
            self._main_vpaned.set_position(self._main_vpaned.get_height() - 200)
        else:
            self._console_pane.set_visible(False)

    def _on_toggle_tools_dock(self, _btn):
        if self._tools_window is None:
            self._tools_detach()
        else:
            self._tools_attach()

    def _tools_detach(self):
        if self._tools_window is not None:
            return
        self._main_vpaned.set_end_child(None)
        win = Gtk.Window(title="SynPad — Tools")
        win.set_default_size(800, 400)
        win.set_child(self._console_pane)
        self._console_pane.set_visible(True)
        win.connect('close-request', self._on_tools_window_delete)
        win.present()
        self._tools_window = win
        self._console_visible = True
        self._tools_dock_btn.set_icon_name('view-restore-symbolic')
        self._tools_dock_btn.set_tooltip_text("Re-attach Tools pane")

    def _tools_attach(self):
        if self._tools_window is None:
            return
        self._tools_window.set_child(None)
        self._main_vpaned.set_end_child(self._console_pane)
        self._main_vpaned.set_resize_end_child(False)
        self._main_vpaned.set_shrink_end_child(True)
        self._console_pane.set_visible(True)
        self._main_vpaned.set_position(max(100, self._main_vpaned.get_height() - 200))
        self._tools_window.destroy()
        self._tools_window = None
        self._console_visible = True
        self._tools_dock_btn.set_icon_name('view-fullscreen-symbolic')
        self._tools_dock_btn.set_tooltip_text("Detach Tools pane to its own window")

    def _on_tools_window_delete(self, *_args):
        self._tools_attach()
        return True  # we destroyed the window ourselves

    def _on_clear_console(self, *_args):
        page = self._console_notebook.get_current_page()
        if page == 0:
            self._console_buffer.set_text('')
        elif page == 1:
            self._git_history_buffer.set_text('')
        elif page == 2 and self._claude_buffer is not None:
            self._claude_buffer.set_text('')

    def _make_tab_label_icon(self, icon_name, text):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        box.append(icon)
        box.append(Gtk.Label(label=text))
        return box

    def _make_tab_label_file(self, path, text):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        try:
            from gi.repository import GdkPixbuf
            pix = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 16, 16)
            img = Gtk.Image.new_from_pixbuf(pix)
        except Exception:
            img = Gtk.Image.new_from_icon_name('image-missing-symbolic')
            img.set_pixel_size(16)
        box.append(img)
        box.append(Gtk.Label(label=text))
        return box

    def _debug(self, message):
        import config
        if config.DEBUG_MODE:
            self._console_log(f"[DEBUG] {message}", 'timestamp')

    def _console_log(self, message, tag=None):
        def _do_log():
            import datetime
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            start = self._console_buffer.get_start_iter()
            if tag:
                self._console_buffer.insert_with_tags_by_name(start, f"{message}\n", tag)
            else:
                self._console_buffer.insert(start, f"{message}\n")
            start = self._console_buffer.get_start_iter()
            self._console_buffer.insert_with_tags_by_name(start, f"[{ts}] ", 'timestamp')
            if self._console_buffer.get_line_count() > 500:
                ok, trim_start = self._console_buffer.get_iter_at_line(500)
                if ok:
                    trim_end = self._console_buffer.get_end_iter()
                    self._console_buffer.delete(trim_start, trim_end)
            return False

        GLib.idle_add(_do_log)

    # -- Keyboard Shortcuts ---------------------------------------------------

    def _on_key_press(self, _ctrl, keyval, _keycode, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK

        if ctrl and keyval == Gdk.KEY_s:
            self._on_save(None)
            return True
        elif ctrl and keyval == Gdk.KEY_n:
            self._on_new_local_file()
            return True
        elif ctrl and keyval == Gdk.KEY_o:
            self._on_open_local_file()
            return True
        elif ctrl and keyval == Gdk.KEY_w:
            page = self.notebook.get_selected_page()
            if page in self.tabs:
                self._close_tab(page)
            return True
        elif ctrl and keyval == Gdk.KEY_q:
            self._on_quit(None)
            return True
        elif ctrl and keyval == Gdk.KEY_f:
            self._show_search(show_replace=False)
            return True
        elif ctrl and keyval == Gdk.KEY_r:
            self._show_search(show_replace=True)
            return True
        elif ctrl and keyval == Gdk.KEY_g:
            self._on_goto_line()
            return True
        elif keyval == Gdk.KEY_F12:
            self._on_toggle_console()
            return True
        elif (ctrl and (state & Gdk.ModifierType.SHIFT_MASK)
              and keyval in (Gdk.KEY_A, Gdk.KEY_a)):
            self._claude_handle_trigger()
            return True
        elif keyval == Gdk.KEY_Escape:
            if self._search_window:
                self._on_search_close()
                return True
        return False

    # -- Cleanup --------------------------------------------------------------

    def _on_close_request(self, _win):
        """Titlebar close / Alt-F4 / WM close. Gtk.Window has no built-in
        cancelable close the way Gtk.Dialog does — this IS that mechanism.
        Returning True stops the close; _do_quit() sets _quit_confirmed and
        calls self.close() itself once the user has actually agreed to
        quit, which re-raises close-request but takes the fast path below
        instead of looping."""
        if self._quit_confirmed:
            return False
        self._on_quit(None)
        return True

    def _on_quit(self, _widget):
        unsaved = [t for t in self.tabs.values() if t.modified]
        if unsaved:
            names = ', '.join(os.path.basename(t.remote_path) for t in unsaved)
            dlg = Adw.AlertDialog(
                heading="Unsaved Changes",
                body=f"Files with unsaved changes: {names}\n\nQuit anyway?",
            )
            dlg.add_response('no', "No")
            dlg.add_response('yes', "Yes")
            dlg.set_default_response('no')
            dlg.set_close_response('no')

            def on_response(d, res):
                try:
                    response = d.choose_finish(res)
                except Exception:
                    return
                if response == 'yes':
                    self._do_quit()

            dlg.choose(self, None, on_response)
            return

        self._do_quit()

    def _do_quit(self):
        self._save_session()

        # Tear down detached Tools window if any
        if self._tools_window is not None:
            try:
                self._tools_window.destroy()
            except Exception:
                pass
            self._tools_window = None

        if self.ftp_mgr:
            self.ftp_mgr.disconnect()
        import shutil
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

        self._quit_confirmed = True
        app = self.get_application()
        self.close()
        if app is not None:
            app.quit()
