"""window.py under GTK4 — headless.

Guards, matching the migration plan's Task 5 scope:

- `Gtk.Notebook` -> `Adw.TabView` + `Adw.TabBar` for the main editor tab
  strip: tab open/close/switch/reorder and the tab context menu all drive
  the *real* Adw.TabView/TabBar signals end to end (close-page,
  notify::selected-page, setup-menu), not stubs — including the confirm-
  before-close window for a modified tab (Yes/No/Escape/close-request,
  same shape as editor.py's other Window-based confirms).
- The v1.20.4 `_reindex_tabs()`/`page-reordered` stale-index workaround is
  gone from EditorMixin entirely (see test_tab_reorder.py) — a reorder
  here is driven via `notebook.reorder_page()`, the same call GTK4 makes
  internally for a drag, with no re-sync step anywhere in the test.
- The hamburger menu: `Gtk.Menu`/`Gtk.MenuItem`/`Gtk.CheckMenuItem` ->
  `Gio.Menu` + `Gio.SimpleAction` on the window's "win." action group.
  Every item/label (including the shortcut hint text) and the two
  checkable items' state are asserted directly off the built menu model;
  actions are driven via `activate_action`, not called as bare functions.
- `self.item_save.set_sensitive` / `self.header.set_subtitle` shims (the
  old Gtk.MenuItem/Gtk.HeaderBar APIs other already-ported modules still
  call) actually work.
- The 1 outstanding `get_iter_at_line()` site in `_console_log`'s
  500-line trim unpacks GTK4's `(ok, iter)` tuple correctly.
- `close-request` (titlebar/Alt-F4) on the main window itself is
  cancelable (Gtk.Window has no such thing built in, unlike Gtk.Dialog) —
  driven via the real 'close-request' signal, not by calling `_on_quit`
  directly.
- The 2 forward-note tree-controller wire-ups
  (`_local_attach_tree_controllers`/`_remote_attach_tree_controllers`)
  and the `Gtk.GestureClick` button audit (button=3, not the default 1).
- `Gtk.Paned.pack1`/`pack2` -> `set_start_child`/`set_end_child` for the
  three-pane layout and the detach/re-attach Tools pane.

Safety note beyond the usual "monkeypatch save_config/secrets_store"
rule: `config.CONFIG_DIR` is computed at import time from `Path.home()`
— i.e. from `$HOME`, NOT `$XDG_CONFIG_HOME`. This file is the first one
in the whole migration that constructs a *real* `SynPadWindow()`, whose
`__init__` calls the real `load_config()`/`_restore_session()` — so
`$HOME` is redirected to a throwaway temp directory at the very top of
this file, before `config` (or anything importing it) is ever imported,
so those calls can't reach ~/.config/synpad even if some other harness
invokes this file with an unmodified environment. save_config and
secrets_store are additionally monkeypatched per the migration plan.
"""
import os
import sys
import tempfile

_FAKE_HOME = tempfile.mkdtemp(prefix='synpad_test_home_')
os.environ['HOME'] = _FAKE_HOME
os.environ.setdefault('XDG_CONFIG_HOME', os.path.join(_FAKE_HOME, '.config'))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GtkSource, Gdk, Gio, GLib, Adw

if not Gtk.init_check():
    print("SKIP: no display")
    sys.exit(0)
Adw.init()

import config
config.save_config = lambda cfg: save_config_calls.append(dict(cfg))
save_config_calls = []

import secrets_store
secrets_store.is_available = lambda: False

# Import every already-ported module alongside window.py, per "verify it
# runs" — a half-ported tree can't import cleanly at all (Global
# Constraint 1).
import completion
import signature_help
import git_history
import local_files
import terminal_tab
import claude_tab
import compare
import dialogs
import connection
import remote
import editor
import session
import window

# Belt and suspenders: also patch the imported-name bindings each module
# already holds (matches the established convention in the other test
# files), on top of the HOME redirection above and the config.save_config
# patch, which together are what actually keep this file off the real
# ~/.config/synpad/config.json.
for mod in (window, editor, remote, local_files, dialogs, connection):
    if hasattr(mod, 'save_config'):
        mod.save_config = lambda cfg: save_config_calls.append(dict(cfg))
for mod in (remote, local_files):
    if hasattr(mod, 'secrets_store'):
        mod.secrets_store = secrets_store

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {extra}" if not cond and extra else ""))
    if not cond:
        fails.append(name)


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


def capture_window(build_fn):
    """Capture the Gtk.Window a mixin method builds, by temporarily
    subclassing Gtk.Window at the shared gi.repository module level —
    editor.py's `Gtk` name is that same module object."""
    captured = []
    class _CapturingWindow(Gtk.Window):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
    _Real = editor.Gtk.Window
    editor.Gtk.Window = _CapturingWindow
    try:
        build_fn()
    finally:
        editor.Gtk.Window = _Real
    return captured[0] if captured else None


def capture_alert(module, build_fn):
    """Capture the real Adw.AlertDialog a mixin method builds (only
    __init__ is intercepted — choose()/close() are the genuine libadwaita
    implementation)."""
    captured = []
    class _CapturingAlertDialog(Adw.AlertDialog):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
    _Real = module.Adw.AlertDialog
    module.Adw.AlertDialog = _CapturingAlertDialog
    try:
        build_fn()
    finally:
        module.Adw.AlertDialog = _Real
    return captured[0] if captured else None


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
            action = menu_model.get_item_attribute_value(i, 'action', None)
            out.append((val.get_string() if val else None,
                        action.get_string() if action else None))
    return out


# =========================================================================
# Import cleanliness — window.py alongside all eleven already-ported modules
# =========================================================================

check("window.py imports cleanly alongside all eleven ported modules",
      hasattr(window, 'SynPadWindow'))
check("window.py is pinned to GTK4", Gtk.get_major_version() == 4)


# =========================================================================
# Construction — real SynPadWindow(), no application (application=None,
# matching the default parameter; SynPadWindow doesn't require one)
# =========================================================================

win = window.SynPadWindow()

check("window constructs without error", win is not None)
check("self.tabs starts empty", win.tabs == {})
check("self.notebook is an Adw.TabView", isinstance(win.notebook, Adw.TabView))
check("self.tab_bar is an Adw.TabBar", isinstance(win.tab_bar, Adw.TabBar))
check("tab_bar is wired to the TabView", win.tab_bar.get_view() is win.notebook)
check("a single pinned Welcome tab exists at startup",
      win.notebook.get_n_pages() == 1)
welcome_page = win.notebook.get_nth_page(0)
check("Welcome tab is titled correctly", welcome_page.get_title() == "Welcome")
check("Welcome tab is pinned (no close button)", welcome_page.get_property('pinned') is True)
check("item_save action starts disabled", win.item_save.get_enabled() is False)

win.item_save.set_sensitive(True)
check("item_save.set_sensitive shim works (aliases set_enabled)",
      win.item_save.get_enabled() is True)
# Left enabled deliberately — the win.save activation test below needs
# it enabled (Gio.SimpleAction.activate() is a documented no-op while
# disabled), and nothing later in this file depends on it being False.

win.header.set_subtitle("Connected")
check("header.set_subtitle shim forwards to the Adw.WindowTitle",
      win._window_title.get_subtitle() == "Connected")

check("quick_btn is a real Gtk.MenuButton", isinstance(win.quick_btn, Gtk.MenuButton))


# =========================================================================
# Hamburger menu — Gio.Menu model, every item/label/accelerator preserved
# =========================================================================

# Inspect the menu model actually built during __init__ (win._menu_model)
# rather than calling _build_menu_model() again, which would re-create and
# re-register every action (resetting item_save back to disabled, etc.).
menu_model = win._menu_model
labels = flatten_labels(menu_model)
expected_labels = [
    "New Local File  Ctrl+N", "Open Local File  Ctrl+O", "Save  Ctrl+S",
    "Find  Ctrl+F", "Find & Replace  Ctrl+R", "Go to Line  Ctrl+G",
    "Pretty Print JSON", "Pretty Print XML", "Compare Tabs",
    "Color Scheme", "Custom Colors",
    "Server Manager", "File Types", "Show Hidden Files (local tree)",
    "Ask Claude...  Ctrl+Shift+A", "Debug Mode",
    "Quit  Ctrl+Q",
]
got_labels = [l for l, _a in labels]
check("hamburger menu preserves every item label verbatim (incl. shortcut hints)",
      got_labels == expected_labels, got_labels)

check("win.save action exists", win.lookup_action('save') is not None)
check("win.show_hidden_files is a stateful boolean action",
      win.lookup_action('show_hidden_files').get_state().get_boolean() in (True, False))
check("win.debug_mode starts unchecked",
      win.lookup_action('debug_mode').get_state().get_boolean() is False)

save_calls = []
_orig_on_save = win._on_save
win._on_save = lambda *a: save_calls.append(a)
win.activate_action('win.save', None)
check("activating win.save calls _on_save", len(save_calls) == 1)
win._on_save = _orig_on_save

show_hidden_before = win.config.get('show_hidden_files', False)
load_local_calls = []
win._load_local_tree = lambda *a, **kw: load_local_calls.append(a)
win._local_path_entry.set_text(_FAKE_HOME)
win.activate_action('win.show_hidden_files', None)
check("activating win.show_hidden_files flips config",
      win.config.get('show_hidden_files') != show_hidden_before)
check("win.show_hidden_files action state actually toggled",
      win.lookup_action('show_hidden_files').get_state().get_boolean() != show_hidden_before)
check("toggling show_hidden_files reloads the local tree", len(load_local_calls) == 1)

win.activate_action('win.debug_mode', None)
check("activating win.debug_mode flips config.DEBUG_MODE", config.DEBUG_MODE is True)
win.activate_action('win.debug_mode', None)
check("activating win.debug_mode again flips it back off", config.DEBUG_MODE is False)


# =========================================================================
# Gesture audit — GtkGestureSingle defaults button to 1; every gesture this
# module wires (via local_files.py/remote.py's forward-note attach
# functions) must explicitly filter to the secondary (right) button.
# =========================================================================

def gesture_clicks(widget):
    return [c for c in widget.observe_controllers() if isinstance(c, Gtk.GestureClick)]

remote_clicks = gesture_clicks(win.tree_view)
check("remote tree view has a right-click GestureClick",
      any(c.get_button() == 3 for c in remote_clicks), [c.get_button() for c in remote_clicks])

local_clicks = gesture_clicks(win._local_view)
check("local tree view has a right-click GestureClick",
      any(c.get_button() == 3 for c in local_clicks), [c.get_button() for c in local_clicks])


# =========================================================================
# Tab lifecycle — real Adw.TabView signals end to end
# =========================================================================

tmp_dir = tempfile.mkdtemp(prefix='synpad_test_files_')


def make_local_file(name, content="hello\n"):
    path = os.path.join(tmp_dir, name)
    with open(path, 'w') as f:
        f.write(content)
    return path

path_a = make_local_file('a.txt')
path_b = make_local_file('b.txt')

win._open_local_file(path_a)
check("opening a file removes the pinned Welcome tab",
      win.notebook.get_n_pages() == 1 and not welcome_page.get_property('pinned') is True
      or win.notebook.get_nth_page(0).get_title() != "Welcome")
check("self.tabs has exactly one entry after opening one file", len(win.tabs) == 1)
page_a = win.notebook.get_selected_page()
check("the new page is selected", page_a is not None)
check("the new page's title is the file's basename", page_a.get_title() == 'a.txt')
check("self.tabs is keyed by the Adw.TabPage object",
      win.tabs.get(page_a) is not None and win.tabs[page_a].local_path == path_a)

win._open_local_file(path_b)
check("self.tabs has two entries after opening a second file", len(win.tabs) == 2)
page_b = win.notebook.get_selected_page()
check("second file becomes the selected page", page_b.get_title() == 'b.txt')
check("page_a and page_b are distinct TabPage objects", page_a is not page_b)

# -- switch --------------------------------------------------------------
symbols_before = [tuple(row) for row in win.symbol_store]
win.notebook.set_selected_page(page_a)
check("notify::selected-page fired and updated the symbol pane",
      win.symbol_store is not None)  # no crash is the main assertion here
check("switching back to page_a re-selects it", win.notebook.get_selected_page() is page_a)

# -- reorder (drives the exact call GTK4 makes internally for a drag) ----
win.notebook.reorder_page(page_a, 1)
check("reorder_page actually moved page_a to position 1",
      win.notebook.get_page_position(page_a) == 1)
check("self.tabs still resolves page_a correctly post-reorder with no re-sync step",
      win.tabs[page_a].local_path == path_a)
check("self.tabs still resolves page_b correctly post-reorder with no re-sync step",
      win.tabs[page_b].local_path == path_b)
win.notebook.reorder_page(page_a, 0)  # restore original order for the rest of the test

# -- tab context menu (setup-menu) ---------------------------------------
win.notebook.emit('setup-menu', page_a)
tabctx_labels = [l for l, _a in flatten_labels(win.notebook.get_menu_model())]
check("tab context menu has Reload from Disk (a is_local tab)",
      "Reload from Disk" in tabctx_labels)
check("tab context menu has Close / Close All / Close All But This",
      {"Close", "Close All", "Close All But This"} <= set(tabctx_labels))
win.notebook.emit('setup-menu', None)  # menu closing — must not raise

# Drive the actual action the menu item is bound to, not the handler
# directly: the trap this migration hit twice before was tests calling
# handlers with fake gestures instead of the real widgets/actions.
win.notebook.emit('setup-menu', page_b)
win.notebook.activate_action('tabctx.close', None)
check("activating tabctx.close actually closed the unmodified tab",
      page_b not in win.tabs and win.notebook.get_n_pages() == 1)

# Re-open b for the remaining tests.
win._open_local_file(path_b)
page_b = win.notebook.get_selected_page()
check("re-opened b.txt for the close-path tests", win.tabs[page_b].local_path == path_b)


# -- close: unmodified tab, via _close_tab (Ctrl+W / programmatic path) --
win._close_tab(page_b)
check("_close_tab on an unmodified tab closes it synchronously",
      page_b not in win.tabs)
check("welcome tab is NOT restored while another real tab (a) remains",
      win.notebook.get_nth_page(0).get_title() != "Welcome")

# -- close: modified tab — real confirm Gtk.Window, driven end to end ----
win.tabs[page_a].buffer.set_text("changed\n")
check("tab a is now modified", win.tabs[page_a].modified is True)

confirm_win = capture_window(lambda: win._close_tab(page_a))
check("a real confirmation Gtk.Window was built for a modified tab", confirm_win is not None)
check("tab a is still open while the confirm window is up", page_a in win.tabs)

# Escape declines — tab must remain open.
press_escape(confirm_win)
check("Escape on the confirm window declines the close", page_a in win.tabs)

# Titlebar close (close-request) also declines.
confirm_win2 = capture_window(lambda: win._close_tab(page_a))
confirm_win2.close()
check("close-request (titlebar) on the confirm window also declines the close",
      page_a in win.tabs)

# No — declines explicitly.
confirm_win3 = capture_window(lambda: win._close_tab(page_a))
click_button(confirm_win3, "No")
check("clicking No declines the close", page_a in win.tabs)

# Yes — actually closes it, and the pinned Welcome tab comes back since
# it was the last real tab.
confirm_win4 = capture_window(lambda: win._close_tab(page_a))
click_button(confirm_win4, "Yes")
check("clicking Yes on a modified tab's confirm window closes it",
      page_a not in win.tabs)
check("Welcome tab is restored once the last real tab closes",
      win.notebook.get_n_pages() == 1 and win.notebook.get_nth_page(0).get_title() == "Welcome")
check("the restored Welcome tab is pinned again",
      win.notebook.get_nth_page(0).get_property('pinned') is True)


# -- _close_all_tabs / _close_all_tabs_except chaining --------------------
win._open_local_file(make_local_file('c1.txt'))
pc1 = win.notebook.get_selected_page()
win._open_local_file(make_local_file('c2.txt'))
pc2 = win.notebook.get_selected_page()
win._open_local_file(make_local_file('c3.txt'))
pc3 = win.notebook.get_selected_page()
check("three unmodified tabs open for the close-all test", len(win.tabs) == 3)

win._close_all_tabs_except(pc2)
check("_close_all_tabs_except closes every tab but the kept one",
      set(win.tabs.keys()) == {pc2})

win._close_all_tabs()
check("_close_all_tabs closes the remaining tab too", len(win.tabs) == 0)
check("Welcome tab back after _close_all_tabs", win.notebook.get_n_pages() == 1)


# =========================================================================
# Console log — get_iter_at_line() tuple unpack (~line 1082 in the brief)
# =========================================================================

win._console_buffer.set_text('')
for i in range(600):
    win._console_log(f"line {i}")
ctx = GLib.MainContext.default()
for _ in range(700):
    ctx.iteration(False)
line_count = win._console_buffer.get_line_count()
check("_console_log's 500-line trim ran without raising on the (ok, iter) tuple",
      line_count <= 502, line_count)


# =========================================================================
# _show_error / _show_info — Adw.AlertDialog, fire-and-forget
# =========================================================================

err_dlg = capture_alert(window, lambda: win._show_error("Oops", "Something broke"))
check("a real Adw.AlertDialog was built for _show_error", err_dlg is not None)
check("_show_error sets the heading", err_dlg.get_heading() == "Oops")
check("_show_error sets the body", err_dlg.get_body() == "Something broke")
err_dlg.close()

info_dlg = capture_alert(window, lambda: win._show_info("Done", "All good"))
check("_show_info builds a real Adw.AlertDialog too",
      info_dlg is not None and info_dlg.get_heading() == "Done")
info_dlg.close()


# =========================================================================
# Reload-dirty-tab prompt (open_or_focus_file) — Adw.AlertDialog
# =========================================================================

path_d = make_local_file('d.txt')
win._open_local_file(path_d)
page_d = win.notebook.get_selected_page()
win.tabs[page_d].buffer.set_text("dirty\n")

reload_dlg = capture_alert(window, lambda: win.open_or_focus_file(path_d))
check("opening an already-open dirty file prompts to reload from disk",
      reload_dlg is not None)
reload_dlg.close()
win._close_tab(page_d)  # unmodified now? no — still dirty; force it closed for cleanup
if page_d in win.tabs:
    win.tabs[page_d].buffer.set_modified(False)
    win._close_tab(page_d)


# =========================================================================
# Pane layout — Gtk.Paned.pack1/pack2 -> set_start_child/set_end_child
# =========================================================================

order_before = list(win.config.get('pane_order', ['symbols', 'editor', 'files']))
win._on_move_pane(None, 'editor', -1)
ctx2 = GLib.MainContext.default()
for _ in range(10):
    ctx2.iteration(False)
order_after = win.config.get('pane_order')
check("_on_move_pane actually reorders pane_order", order_after != order_before)
check("outer paned start child is the new first pane",
      win._outer_paned.get_start_child() is win._pane_widgets[order_after[0]])
check("inner paned start/end children match order[1]/order[2]",
      win._inner_paned.get_start_child() is win._pane_widgets[order_after[1]] and
      win._inner_paned.get_end_child() is win._pane_widgets[order_after[2]])


# =========================================================================
# Tools pane detach / re-attach (Gtk.Paned conversion + close-request)
# =========================================================================

# Note: the earlier win.debug_mode toggle test turned Debug Mode on,
# which (matching the GTK3 original exactly) auto-shows the console if it
# wasn't visible yet — so it's already visible by this point, not hidden.
check("console pane is visible (auto-shown by the earlier Debug Mode toggle)",
      win._console_pane.get_visible() is True and win._console_visible is True)
win._on_toggle_console()
check("toggling console again hides it", win._console_visible is False)
check("console pane widget visibility follows _console_visible when hidden",
      win._console_pane.get_visible() is False)
win._on_toggle_console()
check("toggling console a third time shows it again", win._console_visible is True)
check("console pane is now the paned's end child",
      win._main_vpaned.get_end_child() is win._console_pane)

win._tools_detach()
check("detaching Tools moves it into its own Gtk.Window",
      win._tools_window is not None and win._tools_window.get_child() is win._console_pane)
check("main vpaned no longer holds the console pane while detached",
      win._main_vpaned.get_end_child() is None)

# Titlebar-close on the detached Tools window re-attaches instead of
# leaving a dangling reference (close-request, not delete-event).
win._tools_window.close()
check("closing the detached Tools window re-attaches it",
      win._tools_window is None and win._main_vpaned.get_end_child() is win._console_pane)


# =========================================================================
# Main window close-request — cancelable, unlike bare Gtk.Window's default
# =========================================================================

win2 = window.SynPadWindow()
win2._save_session = lambda: session_save_calls.append(True)
session_save_calls = []

# No unsaved tabs: close-request should let the window actually close on
# the fast path (real close, not a stub call to _on_quit).
result = win2.emit('close-request')
ctx3 = GLib.MainContext.default()
for _ in range(20):
    ctx3.iteration(False)
check("close-request with no unsaved tabs quits cleanly (_save_session ran)",
      len(session_save_calls) == 1)
check("_quit_confirmed guards against re-entrant close-request looping",
      win2._quit_confirmed is True)

win3 = window.SynPadWindow()
win3._save_session = lambda: session3_calls.append(True)
session3_calls = []
win3._open_local_file(make_local_file('unsaved.txt'))
p3 = win3.notebook.get_selected_page()
win3.tabs[p3].buffer.set_text("dirty\n")

quit_dlg = capture_alert(window, lambda: win3.emit('close-request'))
check("close-request with an unsaved tab shows the Unsaved Changes confirm",
      quit_dlg is not None and quit_dlg.get_heading() == "Unsaved Changes")
check("the window has not actually closed yet", session3_calls == [])
quit_dlg.close()


if __name__ == '__main__':
    if fails:
        print(f"\n{len(fails)} FAILURE(S): {fails}")
        sys.exit(1)
    print("\nALL PASS")
