"""SynPad main window — assembles all mixins into SynPadWindow."""

import os
import tempfile

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, GtkSource, Gdk, GLib, Pango

from pathlib import Path

from config import APP_VERSION, load_config, save_config, CONFIG_DIR
from symbols import SYMBOL_EXTENSIONS, SYMBOL_ICONS, parse_symbols
from editor import EditorMixin
from remote import RemoteMixin
from local_files import LocalFilesMixin
from compare import CompareMixin
from dialogs import DialogsMixin
from session import SessionMixin


class SynPadWindow(Gtk.ApplicationWindow, EditorMixin, RemoteMixin, LocalFilesMixin,
                   CompareMixin, DialogsMixin, SessionMixin):
    """Main application window."""

    def __init__(self, application=None):
        super().__init__(application=application, title="SynPad - PHP IDE")
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

    # -- Single-instance file opening -----------------------------------------

    def open_or_focus_file(self, filepath):
        """Open filepath as a tab. If already open, switch to that tab."""
        target = os.path.realpath(os.path.abspath(filepath))

        for page_num, tab in self.tabs.items():
            if not tab.is_local or not tab.local_path:
                continue
            existing = os.path.realpath(os.path.abspath(tab.local_path))
            if existing == target:
                self.notebook.set_current_page(page_num)
                return

        self._open_local_file(target)

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

        item_compare = Gtk.MenuItem(label="Compare Tabs")
        item_compare.connect('activate', lambda _: self._on_compare_tabs())
        menu.append(item_compare)

        menu.append(Gtk.SeparatorMenuItem())

        item_scheme = Gtk.MenuItem(label="Color Scheme")
        item_scheme.connect('activate', self._on_pick_scheme)
        menu.append(item_scheme)

        item_custom_colors = Gtk.MenuItem(label="Custom Colors")
        item_custom_colors.connect('activate', self._on_custom_colors)
        menu.append(item_custom_colors)

        menu.append(Gtk.SeparatorMenuItem())

        item_settings = Gtk.MenuItem(label="Server Manager")
        item_settings.connect('activate', self._on_open_settings)
        menu.append(item_settings)

        item_file_types = Gtk.MenuItem(label="File Types")
        item_file_types.connect('activate', self._on_edit_file_types)
        menu.append(item_file_types)

        self._debug_menu_item = Gtk.CheckMenuItem(label="Debug Mode")
        self._debug_menu_item.set_active(False)
        self._debug_menu_item.connect('toggled', self._on_toggle_debug)
        menu.append(self._debug_menu_item)

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
        sym_header = self.symbol_pane.get_children()[0]
        sym_header.pack_end(self.btn_refresh_symbols, False, False, 0)

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

        # 3) File tree — with local/remote toggle and connection controls
        self.files_pane = self._make_pane_wrapper('files', 'Files')

        # --- Toggle buttons: Remote / Local ---
        toggle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toggle_row.set_margin_start(4)
        toggle_row.set_margin_end(4)
        toggle_row.set_margin_bottom(2)

        self.btn_remote_tree = Gtk.ToggleButton()
        remote_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        remote_btn_box.pack_start(Gtk.Image.new_from_icon_name(
            'network-server-symbolic', Gtk.IconSize.SMALL_TOOLBAR), False, False, 0)
        remote_btn_box.pack_start(Gtk.Label(label="Remote"), False, False, 0)
        self.btn_remote_tree.add(remote_btn_box)
        self.btn_remote_tree.set_active(True)
        self.btn_remote_tree.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_remote_tree.set_tooltip_text("Show remote server files")
        toggle_row.pack_start(self.btn_remote_tree, True, True, 0)

        self.btn_local_tree = Gtk.ToggleButton()
        local_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        local_btn_box.pack_start(Gtk.Image.new_from_icon_name(
            'drive-harddisk-symbolic', Gtk.IconSize.SMALL_TOOLBAR), False, False, 0)
        local_btn_box.pack_start(Gtk.Label(label="Local"), False, False, 0)
        self.btn_local_tree.add(local_btn_box)
        self.btn_local_tree.set_active(False)
        self.btn_local_tree.set_relief(Gtk.ReliefStyle.NONE)
        self.btn_local_tree.set_tooltip_text("Show local files")
        toggle_row.pack_start(self.btn_local_tree, True, True, 0)

        self.files_pane.pack_start(toggle_row, False, False, 0)

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
        self.quick_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._rebuild_quick_menu()
        server_row.pack_start(self.quick_btn, True, True, 0)
        remote_box.pack_start(server_row, False, False, 0)

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
        remote_box.pack_start(self.scroll_tree, True, True, 0)

        conn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        conn_row.set_margin_start(4)
        conn_row.set_margin_end(4)
        conn_row.set_margin_bottom(4)

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

        remote_box.pack_end(conn_row, False, False, 0)
        self._file_stack.add_named(remote_box, 'remote')

        # --- Local file tree ---
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        local_path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        local_path_row.set_margin_start(4)
        local_path_row.set_margin_end(4)
        local_path_row.set_margin_bottom(2)

        btn_local_up = Gtk.Button()
        btn_local_up.set_image(Gtk.Image.new_from_icon_name(
            'go-up-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_local_up.set_relief(Gtk.ReliefStyle.NONE)
        btn_local_up.set_tooltip_text("Go to parent directory")
        btn_local_up.connect('clicked', self._on_local_up)
        local_path_row.pack_start(btn_local_up, False, False, 0)

        self._local_path_entry = Gtk.Entry()
        self._local_path_entry.set_text(str(Path.home()))
        self._local_path_entry.set_tooltip_text("Type a path and press Enter")
        self._local_path_entry.connect('activate', self._on_local_path_enter)
        local_path_row.pack_start(self._local_path_entry, True, True, 0)

        local_box.pack_start(local_path_row, False, False, 0)

        self._local_scroll = Gtk.ScrolledWindow()
        self._local_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._local_store = Gtk.TreeStore(str, str, str, bool, bool)
        self._local_view = Gtk.TreeView(model=self._local_store)
        self._local_view.set_headers_visible(False)

        local_col = Gtk.TreeViewColumn("Files")
        local_icon = Gtk.CellRendererPixbuf()
        local_text = Gtk.CellRendererText()
        local_col.pack_start(local_icon, False)
        local_col.pack_start(local_text, True)
        local_col.add_attribute(local_icon, 'icon-name', 1)
        local_col.add_attribute(local_text, 'text', 0)
        self._local_view.append_column(local_col)

        self._local_view.connect('row-activated', self._on_local_tree_activated)
        self._local_view.connect('row-expanded', self._on_local_tree_expanded)
        self._local_view.connect('button-press-event', self._on_local_tree_right_click)

        self._local_scroll.add(self._local_view)
        local_box.pack_start(self._local_scroll, True, True, 0)

        local_action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        local_action_row.set_margin_start(4)
        local_action_row.set_margin_end(4)
        local_action_row.set_margin_bottom(4)

        btn_local_refresh = Gtk.Button()
        btn_local_refresh.set_image(Gtk.Image.new_from_icon_name(
            'view-refresh-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_local_refresh.set_relief(Gtk.ReliefStyle.NONE)
        btn_local_refresh.set_tooltip_text("Refresh local files")
        btn_local_refresh.connect('clicked', self._on_local_refresh)
        local_action_row.pack_start(btn_local_refresh, False, False, 0)

        btn_local_home = Gtk.Button()
        btn_local_home.set_image(Gtk.Image.new_from_icon_name(
            'go-home-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn_local_home.set_relief(Gtk.ReliefStyle.NONE)
        btn_local_home.set_tooltip_text("Go to home directory")
        btn_local_home.connect('clicked', self._on_local_home)
        local_action_row.pack_start(btn_local_home, False, False, 0)

        local_box.pack_end(local_action_row, False, False, 0)
        self._file_stack.add_named(local_box, 'local')

        self._file_stack.set_visible_child_name('remote')
        self.files_pane.pack_start(self._file_stack, True, True, 0)

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
        GLib.idle_add(self._apply_pane_layout)

    def _apply_pane_layout(self):
        """Place panes into the two persistent Paneds based on config order."""
        order = self.config.get('pane_order', ['symbols', 'editor', 'files'])

        for pane_id in self._pane_widgets:
            w = self._pane_widgets[pane_id]
            parent = w.get_parent()
            if parent:
                parent.remove(w)

        left_w = self._pane_widgets[order[0]]
        mid_w = self._pane_widgets[order[1]]
        right_w = self._pane_widgets[order[2]]

        self._outer_paned.pack1(left_w, resize=False, shrink=True)
        self._inner_paned.pack1(mid_w, resize=True, shrink=True)
        self._inner_paned.pack2(right_w, resize=False, shrink=True)

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
            self.btn_theme.set_image(Gtk.Image.new_from_icon_name(
                'weather-clear-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        else:
            self.btn_theme.set_image(Gtk.Image.new_from_icon_name(
                'weather-clear-night-symbolic', Gtk.IconSize.SMALL_TOOLBAR))

    # -- Signals --------------------------------------------------------------

    def _connect_signals(self):
        self.connect('destroy', self._on_quit)
        self.connect('key-press-event', self._on_key_press)
        self.btn_connect.connect('clicked', self._on_connect)
        self.btn_disconnect.connect('clicked', self._on_disconnect)
        self.btn_refresh.connect('clicked', self._on_refresh)
        self.btn_remote_tree.connect('toggled', self._on_toggle_file_view, 'remote')
        self.btn_local_tree.connect('toggled', self._on_toggle_file_view, 'local')
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

    def _on_toggle_debug(self, item):
        import config
        config.DEBUG_MODE = item.get_active()
        if config.DEBUG_MODE:
            self._console_log("Debug mode ON", 'success')
            if not self._console_visible:
                self._on_toggle_console()
        else:
            self._console_log("Debug mode OFF")

    def _on_toggle_console(self, *_args):
        self._console_visible = not self._console_visible
        if self._console_visible:
            self._console_pane.set_no_show_all(False)
            self._console_pane.show_all()
            self._console_pane.set_no_show_all(True)
            alloc = self._main_vpaned.get_allocation()
            self._main_vpaned.set_position(alloc.height - 200)
        else:
            self._console_pane.set_visible(False)

    def _on_clear_console(self, *_args):
        self._console_buffer.set_text('')

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
                trim_start = self._console_buffer.get_iter_at_line(500)
                trim_end = self._console_buffer.get_end_iter()
                self._console_buffer.delete(trim_start, trim_end)
            return False

        GLib.idle_add(_do_log)

    # -- Keyboard Shortcuts ---------------------------------------------------

    def _on_key_press(self, _widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK

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
                return True

        self._save_session()

        if self.ftp_mgr:
            self.ftp_mgr.disconnect()
        import shutil
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass
        Gtk.main_quit()
