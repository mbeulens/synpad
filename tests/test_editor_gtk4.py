"""editor.py under GTK4 — headless.

Guards, matching the migration plan's Task 4 scope:

- The 4 outstanding `buf.get_iter_at_line(n)` sites (`_on_goto_line`,
  `_try_expand_snippet`, `_try_expand_docblock` x2) unpack GTK4's
  `(ok, iter)` return instead of treating it as a bare iter.
- `_register_bundled_languages()` and the bundled TypeScript spec
  (.ts/.tsx/.mts/.cts) still resolve and highlight under GtkSourceView 5.
- The `MAX_HIGHLIGHT_LINE_LEN` long-line guard and its tab-context-menu
  "Enable Syntax Highlighting (slow)" item both still work (the guard
  itself is also covered by test_long_line_highlight.py).
- Tab close (`_close_tab`, chained by `_close_all_tabs` /
  `_close_all_tabs_except`), `_force_highlight`, `_confirm_then_refresh`,
  `_on_goto_line` and `_do_upload`'s `_ask_overwrite`: every Gtk.Dialog/
  Gtk.MessageDialog + .run() site becomes an async continuation with
  Escape and close-request (titlebar/Alt-F4) routed to the same decision
  as Cancel/No — driving the REAL widgets end to end, not stubs.
- Gtk.FileChooserDialog + .run() -> the native async Gtk.FileDialog for
  "Open Local File" / "Save As".

Updated for Task 5 (Gtk.Notebook -> Adw.TabView): self.tabs is now keyed
by Adw.TabPage, not integer page_num — the hand-built tab_box (label +
close button + GestureClick) is gone entirely, replaced by TabPage.title
and Adw.TabBar's own close button/middle-click/drag-reorder. The tab
context menu is Adw.TabView's built-in one (Gio.Menu via 'setup-menu',
driven here by emitting that signal directly, not a fake right-click
gesture), not a hand-rolled Gtk.PopoverMenu. This isolated EditorMixin
Host wires 'close-page'/'setup-menu' onto its own Adw.TabView itself,
exactly as window.py's _connect_signals() does in production.

Never touches the real ~/.config/synpad/config.json or the OS keyring —
save_config is monkeypatched at module level before any dialog is
exercised (editor.py calls save_config() from code paths these tests
exercise, per the migration plan's dialog-side-effect gotcha).
"""
import os, sys, tempfile, shutil, contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, GLib, Gio, GtkSource, Adw

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)
Adw.init()

import editor
from editor import EditorMixin, MAX_HIGHLIGHT_LINE_LEN
from tab import OpenTab

# Never touch the real config file from a headless test.
save_config_calls = []
editor.save_config = lambda cfg: save_config_calls.append(dict(cfg))

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


# -- shared helpers (same techniques as test_remote_gtk4.py / test_local_files_gtk4.py) --

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


def capture_window(build_fn):
    """Capture the Gtk.Window a mixin method builds, by temporarily
    subclassing Gtk.Window at the shared gi.repository module level —
    editor.py's `Gtk` name is that same module object."""
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


def capture_alert(build_fn):
    """Capture the real Adw.AlertDialog a mixin method builds (only
    __init__ is intercepted — choose()/close() below are the genuine
    libadwaita implementation)."""
    captured = []
    class _CapturingAlertDialog(Adw.AlertDialog):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured.append(self)
    _Real = editor.Adw.AlertDialog
    editor.Adw.AlertDialog = _CapturingAlertDialog
    try:
        build_fn()
    finally:
        editor.Adw.AlertDialog = _Real
    return captured[0] if captured else None


@contextlib.contextmanager
def capture_c_stderr():
    """Capture C-level stderr (fd 2) written during the block.

    GLib's default log handler (what prints "Gtk-WARNING **: ...") writes
    straight to the process's stderr file descriptor via fprintf(), which
    bypasses Python's sys.stderr object entirely — redirecting sys.stderr
    (e.g. contextlib.redirect_stderr) sees nothing. Only fd-level
    redirection catches it. Comparing buffer *content* before/after a
    set_text()-in-user-action regression is not enough to detect it — the
    content is byte-identical either way, only the emitted warning differs
    — this is what actually distinguishes "no warning" from "warning
    ignored". Yields a dict; after the block, result['output'] holds
    whatever was written to fd 2."""
    sys.stderr.flush()
    stderr_fd = sys.stderr.fileno()
    saved_fd = os.dup(stderr_fd)
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, stderr_fd)
    os.close(write_fd)
    result = {'output': ''}
    try:
        yield result
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, stderr_fd)
        os.close(saved_fd)
        os.set_blocking(read_fd, False)
        chunks = []
        try:
            while True:
                chunk = os.read(read_fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except BlockingIOError:
            pass
        os.close(read_fd)
        result['output'] = b''.join(chunks).decode('utf-8', errors='replace')


def fake_choose_response(response):
    """Monkeypatch Adw.AlertDialog.choose so it fires synchronously with
    a given response id, mirroring test_remote_gtk4.py's technique."""
    captured = {}
    def fake_choose(self, parent_win, cancellable, callback):
        captured['dlg'] = self
        self.choose_finish = lambda res: response
        callback(self, object())
    return fake_choose, captured


class FakeWidget:
    def __init__(self):
        self.sensitive = []
    def set_sensitive(self, v): self.sensitive.append(v)
    def get_sensitive(self):
        return self.sensitive[-1] if self.sensitive else True


class FakeHeader:
    def __init__(self): self.subtitle = None
    def set_subtitle(self, s): self.subtitle = s


class FakeFtpMgr:
    def __init__(self, connected=True):
        self.connected = connected
        self.home_dir = '/home/user'
        self.calls = []
    def is_alive(self): return self.connected


class Host(EditorMixin, Gtk.Window):
    """A real Gtk.Window subclass — every dialog/window in editor.py uses
    transient_for=self, so the host must be an actual window. Also the
    exact class shape SynPadWindow has in production: a plain Gtk.Window,
    never Adw.Window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self.config = {}
        self.tabs = {}
        self.ftp_mgr = None
        self.current_server_guid = ''
        # Task 5: Gtk.Notebook -> Adw.TabView. In production, window.py's
        # _connect_signals() wires 'close-page'/'setup-menu' onto
        # self.notebook; this isolated EditorMixin-only harness has no
        # window.py, so it must wire them itself, exactly as SynPadWindow
        # does — otherwise self.notebook.close_page()/_close_tab() would
        # hit no signal handler at all and TabView's default (unconfirmed,
        # immediate) close would run instead.
        self.notebook = Adw.TabView()
        self.notebook.connect('close-page', self._on_tab_close_page)
        self.notebook.connect('setup-menu', self._on_tab_setup_menu)
        self._pending_close_callbacks = {}
        self.set_child(self.notebook)
        self.item_save = FakeWidget()
        self.header = FakeHeader()
        self.btn_connect = FakeWidget()
        self.btn_disconnect = FakeWidget()
        self.btn_refresh = FakeWidget()
        self._pending_upload = None
        self._pending_tree_reload = None
        self._search_window = None
        self._search_show_replace = False

        self.status = []
        self.errors = []
        self.console = []
        self.debug_msgs = []
        self.symbol_updates = []
        self.claude_triggers = []
        self.conflict_diff_calls = []

    def _set_status(self, m): self.status.append(m)
    def _show_error(self, title, msg): self.errors.append((title, msg))
    def _console_log(self, msg, tag=None): self.console.append((msg, tag))
    def _debug(self, msg): self.debug_msgs.append(msg)
    def _get_scheme(self): return None
    def _get_file_ext(self, filepath):
        return filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
    def _update_symbols(self, tab): self.symbol_updates.append(tab)
    def _sighelp_attach(self, view, buf): pass
    def _claude_handle_trigger(self, preset_key=None): self.claude_triggers.append(preset_key)
    def _show_conflict_diff(self, tab, local_content, remote_content, page_num, max_mb, mgr):
        self.conflict_diff_calls.append(
            (tab, local_content, remote_content, page_num, max_mb, mgr))
    def _load_tree(self, path): pass
    def _load_tree_and_expand(self, path, vals): pass


h = Host()
h.present()


def force_close_tab(page):
    """Test-only cleanup: remove one tab without going through the async
    confirm dialog at all, replacing the old _do_close_tab (folded into
    _on_tab_close_page's finish() closure by the Task 5 Adw.TabView port,
    so it's no longer a standalone method). Adw.TabPage keys need no
    ordering care the way the old integer page_num scheme's
    _reindex_tabs did — closing one never renumbers any other."""
    tab = h.tabs.get(page)
    if tab is None:
        return
    tab.modified = False  # _on_tab_close_page only confirms modified tabs
    h.notebook.close_page(page)


def force_close_all_tabs():
    """Test-only cleanup: remove every open tab, unconditionally."""
    for page in list(h.tabs.keys()):
        force_close_tab(page)


def make_tab(name, content='hello\nworld\n', is_local=True, modified=False):
    """Create a real editor tab via _create_editor_tab and return
    (page, tab) — page is the Adw.TabPage Task 5 now keys self.tabs by."""
    h._create_editor_tab(name, name if is_local else '', content, is_local=is_local)
    page = h.notebook.get_selected_page()
    tab = h.tabs[page]
    if modified:
        tab.buffer.set_text(content + 'x')
    return page, tab


# =====================================================================
# GtkSource is at version 5; TypeScript bundled language spec resolves
# — and is genuinely SynPad's bundled spec, not GtkSourceView 5's own
# native typescript.lang (GSV5 ships one at
# /usr/share/gtksourceview-5/language-specs/typescript.lang — a
# do-nothing _register_bundled_languages would still resolve
# get_language('typescript') to *that*, and both the "resolves" check and
# the four extension-mapping checks below would still pass, since neither
# cares which spec answered. A fresh LanguageManager (not the process-wide
# default(), which other code in this file may have already prepended
# onto) plus checking search-path order and the resolved language's style
# ids — SynPad's spec defines exactly 3
# (typescript:built-in-type/keyword/type); GSV5's native one defines ~34
# unrelated ones (typescript:enum-declaration, typescript:decorator, ...)
# — is what actually tells the two apart.
# =====================================================================

check("GtkSource major version is 5", GtkSource.MAJOR_VERSION == 5, GtkSource.MAJOR_VERSION)

spec_dir = os.path.join(os.path.dirname(os.path.abspath(editor.__file__)), 'language-specs')
fresh_lang_mgr = GtkSource.LanguageManager()
check("bundled spec dir is not already on a fresh manager's search path "
      "(clean baseline for the next check)",
      spec_dir not in fresh_lang_mgr.get_search_path(),
      fresh_lang_mgr.get_search_path())

editor._register_bundled_languages(fresh_lang_mgr)
after_path = fresh_lang_mgr.get_search_path()
check("_register_bundled_languages actually prepends the bundled spec dir "
      "(a no-op body would still pass every check below, since GSV5 ships "
      "its own native typescript.lang)",
      after_path[0] == spec_dir, after_path)

ts_lang_fresh = fresh_lang_mgr.get_language('typescript')
check("bundled TypeScript language spec resolves under GtkSourceView 5",
      ts_lang_fresh is not None, ts_lang_fresh)

bundled_style_ids = set(ts_lang_fresh.get_style_ids()) if ts_lang_fresh else set()
check("resolved TypeScript language's style ids are SynPad's bundled "
      "spec's own 3 (built-in-type/keyword/type), not GSV5's native "
      "typescript.lang's ~34 unrelated ones -- proves the *bundled* spec "
      "answered, not GSV5's native one shadowing it",
      bundled_style_ids == {'typescript:built-in-type', 'typescript:keyword',
                             'typescript:type'},
      bundled_style_ids)

lang_mgr = GtkSource.LanguageManager.get_default()
editor._register_bundled_languages(lang_mgr)
ts_lang = lang_mgr.get_language('typescript')
check("bundled TypeScript language spec also resolves via the shared "
      "default LanguageManager (what _create_editor_tab actually uses)",
      ts_lang is not None, ts_lang)

for ext, expected in [('foo.ts', 'typescript'), ('foo.tsx', 'typescript'),
                       ('foo.mts', 'typescript'), ('foo.cts', 'typescript')]:
    lang = h._detect_language(lang_mgr, ext)
    check(f"{ext} detected as {expected}", lang is not None and lang.get_id() == expected,
          lang.get_id() if lang else None)


# =====================================================================
# _create_editor_tab — packing/show_all/EventBox conversions
# =====================================================================

page_num, tab = make_tab('sample.py', 'print(1)\n')
scroll = tab.source_view.get_parent()
check("view lives in a ScrolledWindow via set_child (was scroll.add)",
      isinstance(scroll, Gtk.ScrolledWindow) and scroll.get_child() is tab.source_view)

# Task 5: Gtk.Notebook -> Adw.TabView. There is no more hand-built
# tab_box/close-button/GestureClick at all — the tab's title lives on its
# Adw.TabPage, and Adw.TabBar supplies the close button, middle-click
# close and drag-reorder itself; the only thing left to assert here is
# that the TabPage exists and is titled correctly.
tab_page = h.notebook.get_page(scroll)
check("_create_editor_tab's page is a real Adw.TabPage", isinstance(tab_page, Adw.TabPage))
check("tab title is the file's basename (was the tab_box Gtk.Label's text)",
      tab_page.get_title() == 'sample.py', tab_page.get_title())
check("self.tabs is keyed by the Adw.TabPage object, not an integer",
      page_num is tab_page)

key_ctrls_on_view = [c for c in tab.source_view.observe_controllers()
                     if isinstance(c, Gtk.EventControllerKey)]
check("source view has an EventControllerKey (key-press-event replacement)",
      len(key_ctrls_on_view) >= 1, key_ctrls_on_view)
our_key_ctrl = [c for c in key_ctrls_on_view if c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE]
# GtkSourceView installs its own controllers too (same "count, don't
# index [0]" shape as the GtkSource.View focus-controller gotcha) — at
# least one CAPTURE-phase controller is ours.
check("at least one key controller runs in CAPTURE phase (ours, to intercept before GtkSourceView)",
      len(our_key_ctrl) >= 1, [c.get_propagation_phase() for c in key_ctrls_on_view])

extra_menu = tab.source_view.get_extra_menu()
check("editor view has an extra_menu (populate-popup replacement)", extra_menu is not None)


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


from claude_tab import PRESETS
check("extra_menu has an 'Ask Claude' submenu with all presets",
      set(flatten_labels(extra_menu)) == {lbl for _, lbl, _ in PRESETS},
      flatten_labels(extra_menu))

names = action_names(extra_menu, 'editorctx.')
check("Ask Claude submenu actions activate _claude_handle_trigger with the right key",
      set(names) == {f'ask_{k}' for k, _, _ in PRESETS}, names)
h.claude_triggers.clear()
ok = tab.source_view.activate_action('editorctx.ask_explain', None)
check("activating 'Explain' action calls _claude_handle_trigger('explain')",
      ok and h.claude_triggers == ['explain'], h.claude_triggers)

check("_create_editor_tab reports status for a normal file",
      h.status and h.status[-1] == f"Opened sample.py", h.status)


# =====================================================================
# MAX_HIGHLIGHT_LINE_LEN guard + tab-context-menu "Enable Syntax
# Highlighting (slow)" item
# =====================================================================

long_line_content = 'x = "%s"' % ('y' * (MAX_HIGHLIGHT_LINE_LEN + 1))
page_num_long, tab_long = make_tab('bigline.py', long_line_content)
check("long-line tab has highlighting suppressed", tab_long.highlight_suppressed is True)
check("status reports highlighting disabled with the longest-line count",
      'syntax highlighting off' in h.status[-1], h.status[-1])


# =====================================================================
# _on_tab_setup_menu — Gtk.Menu/Gtk.MenuItem -> Gio.Menu on Adw.TabView's
# built-in context-menu popover (Task 5). Adw.TabView owns right-click and
# middle-click on a tab itself (via Adw.TabBar) — there is no more
# tab_box/GestureClick/Gtk.PopoverMenu of our own to drive; the 'setup-menu'
# signal (emitted by the library right before it shows its popover) is the
# real integration point, so it's emitted directly here instead of faking a
# right-click gesture.
# =====================================================================

page_long = h.notebook.get_page(tab_long.source_view.get_parent())
h.notebook.emit('setup-menu', page_long)
menu_model = h.notebook.get_menu_model()
labels = flatten_labels(menu_model)
check("tab menu includes Reload/Enable-HL/Close/Close All/Close All But This",
      labels == ["Reload from Disk", "Enable Syntax Highlighting (slow)",
                 "Close", "Close All", "Close All But This"], labels)

# 'setup-menu' also fires with page=None when the menu closes — must not raise.
try:
    h.notebook.emit('setup-menu', None)
    setup_none_ok = True
except Exception:
    setup_none_ok = False
check("setup-menu with page=None (menu closing) does not raise", setup_none_ok)

# A normal (non-suppressed) tab has no "Enable Syntax Highlighting" item.
page_normal = h.notebook.get_page(tab.source_view.get_parent())
h.notebook.emit('setup-menu', page_normal)
labels2 = flatten_labels(h.notebook.get_menu_model())
check("normal tab's menu has no Enable-Syntax-Highlighting item",
      "Enable Syntax Highlighting (slow)" not in labels2, labels2)
check("normal tab's Reload label is 'Reload from Disk' (is_local tab)",
      "Reload from Disk" in labels2, labels2)

# Untitled local tab (no local_path / file doesn't exist yet) -> Reload disabled.
h._create_editor_tab('Untitled 1', '', '', is_local=True)
untitled_page = h.notebook.get_selected_page()
untitled_tab = h.tabs[untitled_page]
h.notebook.emit('setup-menu', untitled_page)
# Gio.Menu shows a disabled action's item as insensitive; verify the
# disabled state by its practical effect instead: activating the action
# (via the real action group Adw.TabView installed) must not call through
# to _confirm_then_refresh at all.
_orig_confirm = Host._confirm_then_refresh
confirm_calls = []
Host._confirm_then_refresh = lambda self, tab: confirm_calls.append(tab)
try:
    h.notebook.activate_action('tabctx.reload', None)
finally:
    Host._confirm_then_refresh = _orig_confirm
check("Reload action disabled for a never-saved untitled tab "
      "(activating it does not call _confirm_then_refresh)",
      confirm_calls == [], confirm_calls)

# Close action closes the right (currently-set-up) page.
h.notebook.emit('setup-menu', untitled_page)
h.notebook.activate_action('tabctx.close', None)
check("tabctx.close closes the tab the menu was set up for",
      untitled_page not in h.tabs)

# Middle-click-to-close and drag-to-reorder are Adw.TabBar's own native
# behavior now (no code of ours left to drive here — see task-5-report.md
# for how this was verified instead: a real Adw.TabBar hosted in a shown
# window, since there is no headless way to synthesize the exact internal
# gesture AdwTabBox listens for without a real pointer device).


# =====================================================================
# _close_tab / _close_all_tabs / _close_all_tabs_except — async confirm,
# guaranteed continuation via callback, Escape + close-request routed to
# the same "don't close" path as No.
# =====================================================================

def modified_tab(name):
    pn, t = make_tab(name, 'orig')
    t.buffer.set_text('orig-changed')  # modified-changed sets tab.modified
    return pn, t

# Unmodified tab: closes with no dialog, callback still fires exactly once.
pn, t = make_tab('unmod.txt', 'x')
seen = []
h._close_tab(pn, callback=lambda: seen.append('done'))
check("unmodified tab closes with no dialog", pn not in h.tabs)
check("callback fires for the no-dialog path", seen == ['done'], seen)

# Modified tab, click "Yes": closes, callback fires once.
pn, t = modified_tab('mod1.txt')
seen.clear()
win = capture_window(lambda: h._close_tab(pn, callback=lambda: seen.append('done')))
check("close on a modified tab opens a confirm window", win is not None)
check("confirm window heading is 'Unsaved Changes'", win.get_title() == "Unsaved Changes")
buttons = labeled_buttons(win)
labels = [b.get_label() for b in buttons]
check("button visual order is [No, Yes] (matches GTK3 ButtonsType.YES_NO)",
      labels == ["No", "Yes"], labels)
by_label = {b.get_label(): b for b in buttons}
by_label["Yes"].emit('clicked')
check("'Yes' actually closes the tab", pn not in h.tabs)
check("'Yes' calls the callback exactly once", seen == ['done'], seen)
check("'Yes' also closes the window", win.get_visible() is False)

# Modified tab, click "No": stays open, callback still fires.
pn, t = modified_tab('mod2.txt')
seen.clear()
win = capture_window(lambda: h._close_tab(pn, callback=lambda: seen.append('done')))
by_label = {b.get_label(): b for b in labeled_buttons(win)}
by_label["No"].emit('clicked')
check("'No' does not close the tab", pn in h.tabs)
check("'No' still calls the callback (guaranteed continuation)", seen == ['done'], seen)
force_close_tab(pn)  # clean up

# Modified tab, Escape: stays open, callback still fires (bare Gtk.Window
# has no built-in Escape-to-close, unlike Gtk.Dialog).
pn, t = modified_tab('mod3.txt')
seen.clear()
win = capture_window(lambda: h._close_tab(pn, callback=lambda: seen.append('done')))
press_escape(win)
check("Escape does not close the tab", pn in h.tabs)
check("Escape still calls the callback", seen == ['done'], seen)
check("Escape closes the confirm window itself", win.get_visible() is False)
force_close_tab(pn)  # clean up

# Modified tab, titlebar/Alt-F4 close (win.close()): stays open, callback fires.
pn, t = modified_tab('mod4.txt')
seen.clear()
win = capture_window(lambda: h._close_tab(pn, callback=lambda: seen.append('done')))
win.close()
check("closing via win.close() does not close the tab", pn in h.tabs)
check("closing via win.close() still calls the callback", seen == ['done'], seen)
force_close_tab(pn)  # clean up

# No callback given (X button / tab-menu "Close" call sites): must not raise.
pn, t = modified_tab('mod5.txt')
win = capture_window(lambda: h._close_tab(pn))
try:
    by_label = {b.get_label(): b for b in labeled_buttons(win)}
    by_label["Yes"].emit('clicked')
    ok = True
except Exception:
    ok = False
check("_close_tab with no callback does not raise", ok)
check("...and still closes on Yes", pn not in h.tabs)

# _close_all_tabs: mix of modified/unmodified tabs, confirmed in reverse
# page order, one dialog at a time.
force_close_all_tabs()
check("all tabs closed before the close-all scenario", h.tabs == {})

pn_a, ta = make_tab('a.txt', 'a')
pn_b, tb = modified_tab('b.txt')
pn_c, tc = make_tab('c.txt', 'c')
order_before = sorted(h.tabs.keys(), reverse=True)

windows = []
_orig_Window = Gtk.Window
class _QueueCapture(Gtk.Window):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        windows.append(self)
editor.Gtk.Window = _QueueCapture
try:
    h._close_all_tabs()
    # Only the modified tab (b) should have opened a confirm window so far.
    check("_close_all_tabs only confirms the modified tab", len(windows) == 1, len(windows))
    check("unmodified tabs after b in reverse order are already gone",
          pn_c not in h.tabs, list(h.tabs.keys()))
    by_label = {b.get_label(): b for b in labeled_buttons(windows[0])}
    by_label["Yes"].emit('clicked')
finally:
    editor.Gtk.Window = _orig_Window
check("after confirming, every tab is closed", h.tabs == {})

# _close_all_tabs_except: declining the one confirm still lets the chain
# finish (guaranteed continuation), keeping the declined tab open.
pn_a, ta = make_tab('a2.txt', 'a')
pn_b, tb = modified_tab('b2.txt')
pn_c, tc = make_tab('c2.txt', 'c')
windows.clear()
editor.Gtk.Window = _QueueCapture
try:
    h._close_all_tabs_except(pn_a)
    by_label = {b.get_label(): b for b in labeled_buttons(windows[0])}
    by_label["No"].emit('clicked')
finally:
    editor.Gtk.Window = _orig_Window
check("kept tab (a2.txt) still open", pn_a in h.tabs)
check("declined modified tab (b2.txt) stays open (No)", pn_b in h.tabs)
check("unmodified other tab (c2.txt) closed", pn_c not in h.tabs)
force_close_all_tabs()


# =====================================================================
# _force_highlight — Adw.AlertDialog, one-shot (safe if Escape/close never
# invoke choose()'s callback).
# =====================================================================

_, tab_hl = make_tab('big2.py', long_line_content)
check("new tab has highlighting suppressed", tab_hl.highlight_suppressed is True)

fake_choose, captured = fake_choose_response('cancel')
_orig_choose = Adw.AlertDialog.choose
Adw.AlertDialog.choose = fake_choose
try:
    h._force_highlight(tab_hl)
    check("Cancel leaves highlighting off", tab_hl.buffer.get_highlight_syntax() is False)
    check("Cancel does not clear highlight_suppressed", tab_hl.highlight_suppressed is True)
    dlg = captured['dlg']
    check("heading is the enable-highlighting question",
          dlg.get_heading() == "Enable syntax highlighting?", dlg.get_heading())
    check("close_response is 'cancel'", dlg.get_close_response() == 'cancel')
    check("default_response is 'cancel'", dlg.get_default_response() == 'cancel')
finally:
    Adw.AlertDialog.choose = _orig_choose

fake_choose_ok, captured2 = fake_choose_response('ok')
Adw.AlertDialog.choose = fake_choose_ok
try:
    h._force_highlight(tab_hl)
    check("OK enables highlighting", tab_hl.buffer.get_highlight_syntax() is True)
    check("OK clears highlight_suppressed", tab_hl.highlight_suppressed is False)
    check("status reports highlighting enabled",
          'Highlighting enabled' in h.status[-1], h.status[-1])
finally:
    Adw.AlertDialog.choose = _orig_choose

# Real Adw.AlertDialog: closing it (Escape/close path) never invokes the
# choose() callback on this stack (parent is a plain Gtk.Window) — verified
# independently of any SynPad code (see remote.py's _confirm_delete). The
# safe outcome (highlighting stays off) is unaffected since nothing here
# is chained on the callback firing.
_, tab_hl2 = make_tab('big3.py', long_line_content)
real_dlg = capture_alert(lambda: h._force_highlight(tab_hl2))
check("real Adw.AlertDialog constructed", real_dlg is not None)
real_dlg.close()
check("closing the real dialog does not raise", True)
check("closing the real dialog leaves highlighting off (inert callback)",
      tab_hl2.buffer.get_highlight_syntax() is False)


# =====================================================================
# _confirm_then_refresh — Adw.AlertDialog, one-shot Cancel/Reload.
# =====================================================================

refresh_calls = []
_orig_refresh_tab = Host._refresh_tab
Host._refresh_tab = lambda self, tab: refresh_calls.append(tab)
try:
    pn_r, tab_r = modified_tab('refresh1.txt')
    fake_choose_cancel, cap = fake_choose_response('cancel')
    Adw.AlertDialog.choose = fake_choose_cancel
    try:
        h._confirm_then_refresh(tab_r)
        check("Cancel does not refresh", refresh_calls == [], refresh_calls)
        dlg = cap['dlg']
        check("heading is the unsaved-changes question",
              dlg.get_heading() == "File has unsaved changes", dlg.get_heading())
    finally:
        Adw.AlertDialog.choose = _orig_choose

    fake_choose_reload, cap2 = fake_choose_response('reload')
    Adw.AlertDialog.choose = fake_choose_reload
    try:
        h._confirm_then_refresh(tab_r)
        check("Reload does refresh", refresh_calls == [tab_r], refresh_calls)
    finally:
        Adw.AlertDialog.choose = _orig_choose

    refresh_calls.clear()
    pn_r2, tab_r2 = make_tab('refresh2.txt', 'x')
    h._confirm_then_refresh(tab_r2)
    check("unmodified tab refreshes without any dialog", refresh_calls == [tab_r2], refresh_calls)
finally:
    Host._refresh_tab = _orig_refresh_tab
    force_close_tab(pn_r)
    force_close_tab(pn_r2)

# Real (unstubbed) end-to-end reload: _refresh_tab -> _capture_view_state /
# _apply_refreshed_content -> GLib.idle_add(_restore_scroll), pumping the
# default main context so the idle callback actually runs.
tmp_dir3 = tempfile.mkdtemp()
try:
    reload_path = os.path.join(tmp_dir3, 'reload.txt')
    with open(reload_path, 'w') as f:
        f.write('original content\n')
    pn_rl, tab_rl = make_tab('reload.txt', 'original content\n', is_local=True)
    tab_rl.local_path = reload_path
    tab_rl.buffer.set_text('local edits not yet saved')  # modified=True
    with open(reload_path, 'w') as f:
        f.write('new disk content\n')

    fake_choose_reload2, _cap = fake_choose_response('reload')
    Adw.AlertDialog.choose = fake_choose_reload2
    try:
        h._confirm_then_refresh(tab_rl)
    finally:
        Adw.AlertDialog.choose = _orig_choose

    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)

    reloaded_text = tab_rl.buffer.get_text(
        tab_rl.buffer.get_start_iter(), tab_rl.buffer.get_end_iter(), True)
    check("real _refresh_tab re-reads the file from disk",
          reloaded_text == 'new disk content\n', reloaded_text)
    check("real _refresh_tab marks the buffer unmodified",
          tab_rl.buffer.get_modified() is False)
    check("real _refresh_tab reports status", 'Reloaded' in h.status[-1], h.status[-1])
    force_close_tab(pn_rl)
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)


# =====================================================================
# _on_goto_line — bare Gtk.Window (no Cancel button); get_iter_at_line
# tuple-unpack correctness; Escape does nothing.
# =====================================================================

_, tab_goto = make_tab('goto.py', '\n'.join(f'line{i}' for i in range(20)))
h.notebook.set_selected_page(h.notebook.get_page(tab_goto.source_view.get_parent()))

win = capture_window(lambda: h._on_goto_line())
check("goto-line window created", win is not None)
spin = find_all(win, Gtk.SpinButton)[0]
spin.set_value(5)
go_btn = labeled_buttons(win)[0]
check("only a 'Go' button (no Cancel, matches GTK3 original)",
      [b.get_label() for b in labeled_buttons(win)] == ["Go"])
go_btn.emit('clicked')
cursor_line = tab_goto.buffer.get_iter_at_mark(tab_goto.buffer.get_insert()).get_line()
check("Go jumps the cursor to line 5 - 1 (0-indexed) without AttributeError",
      cursor_line == 4, cursor_line)
check("Go closes the window", win.get_visible() is False)

# Escape: no jump, window closes.
tab_goto.buffer.place_cursor(tab_goto.buffer.get_start_iter())
win2 = capture_window(lambda: h._on_goto_line())
find_all(win2, Gtk.SpinButton)[0].set_value(10)
press_escape(win2)
cursor_line2 = tab_goto.buffer.get_iter_at_mark(tab_goto.buffer.get_insert()).get_line()
check("Escape does not move the cursor", cursor_line2 == 0, cursor_line2)
check("Escape closes the goto-line window", win2.get_visible() is False)


# =====================================================================
# _try_expand_snippet / _try_expand_docblock — get_iter_at_line tuple
# unpack (the other 3 outstanding sites)
# =====================================================================

_, tab_snip = make_tab('snip.py', '///')
tab_snip.buffer.place_cursor(tab_snip.buffer.get_end_iter())
ok = h._try_expand_snippet(tab_snip.source_view)
text = tab_snip.buffer.get_text(tab_snip.buffer.get_start_iter(), tab_snip.buffer.get_end_iter(), True)
check("/// expands to a separator line without AttributeError",
      ok and text.startswith('//---'), (ok, text))

_, tab_doc = make_tab('doc.js', '/**\nfunction add(a, b) {\n')
it = tab_doc.buffer.get_iter_at_line(0)[1]
tab_doc.buffer.place_cursor(it)
ok2 = h._try_expand_snippet(tab_doc.source_view)
text2 = tab_doc.buffer.get_text(tab_doc.buffer.get_start_iter(), tab_doc.buffer.get_end_iter(), True)
check("/** expands to a JSDoc block (docblock generation reads the next line "
      "via get_iter_at_line without AttributeError)",
      ok2 and '@param' in text2 and '@returns' in text2, (ok2, text2))

# Tab key on the source view triggers snippet expansion via the
# CAPTURE-phase EventControllerKey (drives the real handler, not a stub).
_, tab_tabkey = make_tab('tabkey.py', '///')
tab_tabkey.buffer.place_cursor(tab_tabkey.buffer.get_end_iter())
key_ctrl = [c for c in tab_tabkey.source_view.observe_controllers()
            if isinstance(c, Gtk.EventControllerKey)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE][0]
handled = key_ctrl.emit('key-pressed', Gdk.KEY_Tab, 0, 0)
text3 = tab_tabkey.buffer.get_text(tab_tabkey.buffer.get_start_iter(), tab_tabkey.buffer.get_end_iter(), True)
check("Tab key on /// expands the snippet via the real key controller",
      text3.startswith('//---'), text3)

# Ctrl+F / Ctrl+G through the same real controller.
h._search_window = None
handled = key_ctrl.emit('key-pressed', Gdk.KEY_f, 0, Gdk.ModifierType.CONTROL_MASK)
check("Ctrl+F opens the search window via the real key controller",
      h._search_window is not None, h._search_window)
h._on_search_close()


# =====================================================================
# _build_search_window / _on_search_window_key — packing, close-request,
# Escape/Shift+Enter key handling
# =====================================================================

_, tab_search = make_tab('search.txt', 'hello world\nhello there\n')
h.notebook.set_selected_page(h.notebook.get_page(tab_search.source_view.get_parent()))
h._show_search(show_replace=True)
check("search window built", h._search_window is not None)
check("Find and Replace entries present",
      hasattr(h, '_search_entry') and hasattr(h, '_replace_entry'))
check("search window has an EventControllerKey", len(escape_key_controllers(h._search_window)) >= 1)

# Escape via close-request path: _on_search_close destroys it and clears state.
search_win = h._search_window
press_escape(search_win)
check("Escape closes the search window", h._search_window is None)

h._show_search(show_replace=False)
search_win2 = h._search_window
search_win2.close()  # titlebar / Alt-F4 path -> close-request
check("titlebar close also clears search window state (close-request wired)",
      h._search_window is None)


# =====================================================================
# _update_tab_label — Task 5: Adw.TabPage.set_title() replaces the old
# get_first_child()/get_next_sibling() widget-tree walk to find the tab's
# Gtk.Label entirely (there's no tab_box left to walk).
# =====================================================================

_, tab_lbl = make_tab('label.txt', 'x')
h._update_tab_label(tab_lbl, "renamed.txt")
page_lbl = h.notebook.get_page(tab_lbl.source_view.get_parent())
check("_update_tab_label sets the Adw.TabPage's title directly",
      page_lbl.get_title() == "renamed.txt", page_lbl.get_title())


# =====================================================================
# _on_open_local_file / _on_save_local — Gtk.FileChooserDialog + .run()
# -> native async Gtk.FileDialog (same technique as test_connection_gtk4.py)
# =====================================================================

tmp_dir = tempfile.mkdtemp()
try:
    captured_fd = {}
    class FakeFileDialog:
        def __init__(self):
            self.filters = None
            self.default_filter = None
            self.initial_name = None
            self.initial_folder = None
        def set_title(self, t): self.title = t
        def set_filters(self, f): self.filters = f
        def set_default_filter(self, f): self.default_filter = f
        def set_initial_name(self, n): self.initial_name = n
        def set_initial_folder(self, f): self.initial_folder = f
        def open(self, parent, cancellable, callback):
            captured_fd['mode'] = 'open'
            captured_fd['dlg'] = self
            captured_fd['callback'] = callback
        def save(self, parent, cancellable, callback):
            captured_fd['mode'] = 'save'
            captured_fd['dlg'] = self
            captured_fd['callback'] = callback

    _orig_FileDialog = editor.Gtk.FileDialog
    editor.Gtk.FileDialog = FakeFileDialog

    class _Chosen:
        def __init__(self, path): self._path = path
        def get_path(self): return self._path

    try:
        h.config['last_save_dir'] = ''
        h._on_open_local_file()
        check("Open Local File opens the native picker", captured_fd.get('mode') == 'open')
        fd = captured_fd['dlg']
        check("2 filters set (All files / Code files)", fd.filters.get_n_items() == 2)

        opened_path = os.path.join(tmp_dir, 'opened.py')
        with open(opened_path, 'w') as f:
            f.write('print(2)\n')
        fd.open_finish = lambda result: _Chosen(opened_path)
        h._on_open_local_file_response(fd, object())
        check("choosing a file opens it in a tab",
              any(t.is_local and t.local_path == opened_path for t in h.tabs.values()))
        check("last_save_dir is remembered and saved",
              h.config['last_save_dir'] == tmp_dir and save_config_calls, h.config)

        # Cancelled picker (GLib.Error) -> no-op, no crash.
        before = len(h.tabs)
        fd.open_finish = lambda result: (_ for _ in ()).throw(GLib.Error("dismissed"))
        h._on_open_local_file_response(fd, object())
        check("cancelling Open Local File is a no-op", len(h.tabs) == before)

        # Save As (untitled local file).
        captured_fd.clear()
        _, untitled = make_tab('Untitled 9', 'saved content', is_local=True)
        untitled.local_path = ''
        h._on_save_local(untitled)
        check("Save As on an untitled tab opens the native save picker",
              captured_fd.get('mode') == 'save')
        fd2 = captured_fd['dlg']
        check("initial name is the untitled tab's display name",
              fd2.initial_name == 'Untitled 9', fd2.initial_name)
        save_path = os.path.join(tmp_dir, 'saved.py')
        fd2.save_finish = lambda result: _Chosen(save_path)
        h._on_save_local_response(fd2, object(), untitled)
        check("Save As writes the file to the chosen path", os.path.exists(save_path))
        with open(save_path) as f:
            check("saved content matches the buffer", f.read() == 'saved content', f.read())
        check("tab's local_path/remote_path updated", untitled.local_path == save_path)
        check("buffer marked unmodified after save", untitled.buffer.get_modified() is False)

        # Save on a tab that already has a path writes immediately (no picker).
        captured_fd.clear()
        _, existing = make_tab('existing.py', 'v1', is_local=True)
        existing.local_path = os.path.join(tmp_dir, 'existing.py')
        h._on_save_local(existing)
        check("saving an already-pathed tab does not open a picker", captured_fd == {})
        check("content written to disk", open(existing.local_path).read() == 'v1')
    finally:
        editor.Gtk.FileDialog = _orig_FileDialog
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


# =====================================================================
# _do_upload's _ask_overwrite — custom-content Gtk.Window driven from a
# background thread blocked on queue.Queue.get(); Escape/close-request
# must reach the queue or the worker thread hangs forever.
# =====================================================================

class SyncThread:
    """Runs the target synchronously in the calling thread — lets the test
    drive the dialog (built via a faked GLib.idle_add, also synchronous)
    before the 'background' code resumes."""
    def __init__(self, target=None, daemon=None): self._t = target
    def start(self): self._t()


def run_upload_conflict(button_label_or_action, tab, page_num, max_mb, mgr):
    """Runs _do_upload with threading/idle_add faked synchronous, capturing
    the _ask_overwrite window and invoking `button_label_or_action` on it
    (a callable receiving the window) before work() resumes past
    result_q.get()."""
    windows = []
    class _Capture(Gtk.Window):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            windows.append(self)

    _orig_thread = editor.threading.Thread
    _orig_idle = editor.GLib.idle_add
    _orig_window = editor.Gtk.Window
    editor.threading.Thread = SyncThread
    editor.Gtk.Window = _Capture

    def fake_idle_add(fn, *a):
        fn(*a)  # build + present the window synchronously
        if windows:
            button_label_or_action(windows[-1])
        return 0
    editor.GLib.idle_add = fake_idle_add
    try:
        h._do_upload(tab, page_num, max_mb)
    finally:
        editor.threading.Thread = _orig_thread
        editor.GLib.idle_add = _orig_idle
        editor.Gtk.Window = _orig_window
    return windows[0] if windows else None


class FakeUploadMgr:
    def __init__(self, remote_bytes, remote_mtime=100, local_hash_mismatch=True):
        self.connected = True
        self.remote_bytes = remote_bytes
        self.remote_mtime = remote_mtime
        self.uploaded = []
    def get_remote_mtime(self, path): return self.remote_mtime
    def get_remote_size(self, path): return len(self.remote_bytes)
    def download(self, remote_path, local_path):
        with open(local_path, 'wb') as f:
            f.write(self.remote_bytes)
    def upload(self, remote_path, local_path, max_mb):
        self.uploaded.append((remote_path, local_path))

tmp_dir2 = tempfile.mkdtemp()
try:
    local_path = os.path.join(tmp_dir2, 'conflict.txt')
    with open(local_path, 'w') as f:
        f.write('local version')
    pn_up, tab_up = make_tab('conflict.txt', 'local version', is_local=True)
    tab_up.local_path = local_path
    tab_up.remote_path = '/remote/conflict.txt'
    tab_up.remote_mtime = 50
    tab_up.remote_hash = 'stale-hash-does-not-match'

    mgr = FakeUploadMgr(remote_bytes=b'remote version', remote_mtime=200)
    h.ftp_mgr = mgr
    h.item_save.sensitive.clear()

    win = run_upload_conflict(
        lambda w: [b for b in labeled_buttons(w) if b.get_label() == "Cancel"][0].emit('clicked'),
        tab_up, pn_up, 5, mgr)
    check("conflict dialog window created", win is not None)
    check("Cancel does not upload", mgr.uploaded == [])
    check("Cancel re-enables Save", h.item_save.sensitive and h.item_save.sensitive[-1] is True)

    win2 = run_upload_conflict(
        lambda w: press_escape(w), tab_up, pn_up, 5, mgr)
    check("Escape on the conflict dialog does not upload either "
          "(bare Gtk.Window has no built-in Escape; wired explicitly)",
          mgr.uploaded == [])

    win3 = run_upload_conflict(lambda w: w.close(), tab_up, pn_up, 5, mgr)
    check("closing via titlebar/Alt-F4 (close-request) does not hang and does not upload",
          mgr.uploaded == [])

    win4 = run_upload_conflict(
        lambda w: [b for b in labeled_buttons(w)
                   if b.get_label() == "Overwrite server with my changes"][0].emit('clicked'),
        tab_up, pn_up, 5, mgr)
    check("'Overwrite' proceeds to upload",
          mgr.uploaded == [('/remote/conflict.txt', local_path)], mgr.uploaded)

    mgr.uploaded.clear()
    win5 = run_upload_conflict(
        lambda w: [b for b in labeled_buttons(w)
                   if b.get_label() == "Discard my changes, use server version"][0].emit('clicked'),
        tab_up, pn_up, 5, mgr)
    check("'Discard, use server version' does not upload", mgr.uploaded == [])
    check("'Discard, use server version' loads the remote content into the buffer",
          tab_up.buffer.get_text(tab_up.buffer.get_start_iter(),
                                  tab_up.buffer.get_end_iter(), True) == 'remote version')

    h.conflict_diff_calls.clear()
    tab_up.remote_hash = 'stale-hash-does-not-match'
    win6 = run_upload_conflict(
        lambda w: [b for b in labeled_buttons(w)
                   if b.get_label() == "Compare both versions"][0].emit('clicked'),
        tab_up, pn_up, 5, mgr)
    check("'Compare both versions' calls _show_conflict_diff instead of uploading",
          len(h.conflict_diff_calls) == 1, h.conflict_diff_calls)
finally:
    shutil.rmtree(tmp_dir2, ignore_errors=True)


# =====================================================================
# Pretty Print JSON/XML — GTK4's TextBuffer.set_text() begins its own
# "irreversible action", which raises a Gtk-WARNING ("Cannot begin
# irreversible action while in user action") if called inside an already
# -open begin_user_action()/end_user_action() pair (as _do_upload's
# _load_remote also did — see its fix above). delete()+insert() replaces
# set_text() in both to avoid it. Buffer *content* is byte-identical
# whether or not the warning fires, so the content-only checks below don't
# by themselves prove the warning is gone — capture_c_stderr() around each
# call does (GLib writes Gtk-WARNING straight to the C stderr fd, bypassing
# sys.stderr, so it must be caught at the fd level, not via redirect_stderr).
# =====================================================================

with capture_c_stderr() as stderr_cap:
    _, tab_json = make_tab('pretty.json', '{"b": 2, "a": 1}')
    h._on_pretty_print_json()
check("JSON pretty-print emits no Gtk-WARNING at all (fd-level stderr capture)",
      stderr_cap['output'] == '', stderr_cap['output'])
check("...specifically not the set_text-in-user-action one",
      'irreversible action' not in stderr_cap['output'], stderr_cap['output'])
pretty_text = tab_json.buffer.get_text(
    tab_json.buffer.get_start_iter(), tab_json.buffer.get_end_iter(), True)
check("JSON pretty-print reformats correctly",
      pretty_text == '{\n    "b": 2,\n    "a": 1\n}', pretty_text)
check("JSON pretty-print reports status", h.status[-1] == "JSON formatted", h.status[-1])

_, tab_badjson = make_tab('bad.json', '{not json')
h.errors.clear()
h._on_pretty_print_json()
check("invalid JSON reports an error instead of crashing",
      h.errors and h.errors[-1][0] == "JSON Error", h.errors)

with capture_c_stderr() as stderr_cap2:
    _, tab_xml = make_tab('pretty.xml', '<a><b>1</b></a>')
    h._on_pretty_print_xml()
check("XML pretty-print emits no Gtk-WARNING at all (fd-level stderr capture)",
      stderr_cap2['output'] == '', stderr_cap2['output'])
check("...specifically not the set_text-in-user-action one",
      'irreversible action' not in stderr_cap2['output'], stderr_cap2['output'])
pretty_xml = tab_xml.buffer.get_text(
    tab_xml.buffer.get_start_iter(), tab_xml.buffer.get_end_iter(), True)
check("XML pretty-print reformats correctly",
      '<a>' in pretty_xml and '<b>' in pretty_xml and pretty_xml != '<a><b>1</b></a>',
      pretty_xml)
check("XML pretty-print reports status", h.status[-1] == "XML formatted", h.status[-1])

# Prove capture_c_stderr() itself actually detects the warning (otherwise
# an empty-string result above would be meaningless): temporarily revert
# to the old set_text()-inside-a-user-action shape and confirm it's caught.
def _regressed_pretty_print_json(self):
    page = self.notebook.get_selected_page()
    tab = self.tabs.get(page)
    buf = tab.buffer
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
    import json as _json
    pretty = _json.dumps(_json.loads(text), indent=4)
    buf.begin_user_action()
    buf.set_text(pretty)  # the pre-fix shape
    buf.end_user_action()

pn_regress, tab_regress = make_tab('regress.json', '{"a": 1}')
with capture_c_stderr() as regress_cap:
    _regressed_pretty_print_json(h)
check("capture_c_stderr() harness actually detects the set_text-in-user-action "
      "warning when it's reintroduced (proves the two checks above aren't vacuous)",
      'irreversible action' in regress_cap['output'], regress_cap['output'])
force_close_tab(pn_regress)

force_close_all_tabs()


print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
