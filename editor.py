"""SynPad editor mixin — tab management, save/upload, search, snippets.

GTK4 notes:

- `_close_tab`'s unsaved-changes confirmation is used both standalone (X
  button, tab-menu "Close") and chained by `_close_all_tabs` /
  `_close_all_tabs_except`, which must keep asking about the next tab
  regardless of how the previous confirmation resolved. It therefore takes
  an optional `callback`, invoked exactly once after the decision (close or
  don't) is made, and uses a plain `Gtk.Window` with explicit Yes/No
  buttons plus `close-request`/Escape wired to the same "don't close" path
  — not `Adw.AlertDialog`, whose Escape/close handling is known (see
  `remote.py`'s `_confirm_delete` docstring) to never invoke its
  `choose()` callback at all when its parent is a plain `Gtk.Window`
  (exactly what `SynPadWindow` is). A one-shot confirmation with no
  chained continuation waiting on it (`_force_highlight`,
  `_confirm_then_refresh`) is safe with `Adw.AlertDialog`: if Escape/close
  never invokes the callback, nothing happens, which is the same outcome
  as an explicit decline.
- `_on_open_local_file` / `_on_save_local`'s file pickers move from
  `Gtk.FileChooserDialog` + `.run()` (removed in GTK4, and the dialog
  itself deprecated since 4.10) to the native async `Gtk.FileDialog`,
  matching `connection.py`'s `_on_browse_key`. `Gtk.FileDialog.save()`
  always confirms overwrite itself, replacing the old
  `set_do_overwrite_confirmation(True)`.
- `_on_goto_line`'s dialog and `_do_upload`'s `_ask_overwrite` are
  custom-content dialogs (a spin button; a warning icon + four buttons),
  so per the migration plan's dialog shapes they're plain `Gtk.Window`s
  with explicit buttons, not `Adw.AlertDialog`. `_ask_overwrite` in
  particular blocks a *background* thread on `queue.Queue.get()` while the
  dialog is shown on the main thread via `GLib.idle_add` — closing it any
  way other than a button click (Escape, titlebar, Alt-F4) must still
  reach the queue or that worker thread hangs forever, so both are wired
  to the Cancel path explicitly.
- The tab-label context menu (right-click) and the editor's own
  right-click menu convert `Gtk.Menu`/`Gtk.MenuItem` to `Gio.Menu` +
  `Gtk.PopoverMenu` / `Gtk.TextView.set_extra_menu`, following
  `local_files.py`'s/`remote.py`'s established pattern. GTK4 removed
  `GtkTextView`'s `populate-popup` signal entirely (`GtkSource.View`
  inherits from `GtkTextView`); the declarative `set_extra_menu(Gio.Menu)`
  replaces it, built once per view instead of rebuilt on every popup.
- The 4 outstanding `buf.get_iter_at_line(n)` sites now unpack the
  `(ok, iter)` tuple GTK4 returns (`_on_goto_line`, `_try_expand_snippet`,
  `_try_expand_docblock` x2). No other `get_iter_at_*`/`get_*_iter`
  method here changed shape — verified by introspection.
- `Gtk.Notebook` is untouched here per the migration plan — Task 5 owns
  the `Adw.TabView` conversion.
"""

import hashlib
import json
import os
import re
import threading
import time
import traceback

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GtkSource, Gdk, GLib, Gio, Adw

from config import (save_config, find_server_by_guid, CONFIG_DIR,
                    MAX_HIGHLIGHT_LINE_LEN)
import secrets_store
from connection import FTPManager, SFTPManager
from completion import make_completion_providers, COMPLETION_LANGS
from symbols import SYMBOL_EXTENSIONS, parse_symbols, SYMBOL_ICONS
from tab import OpenTab


_LANG_PATH_REGISTERED = False


def _register_bundled_languages(lang_mgr):
    """Prepend SynPad's bundled language-specs dir to the LanguageManager
    search path so app-shipped syntax definitions (e.g. typescript.lang) are
    found. GtkSourceView ships most specs compiled into the library and has
    no TypeScript definition of its own, so .ts files would otherwise be
    unhighlighted. Idempotent — the default LanguageManager is a singleton."""
    global _LANG_PATH_REGISTERED
    if _LANG_PATH_REGISTERED:
        return
    spec_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'language-specs')
    if os.path.isdir(spec_dir):
        path = lang_mgr.get_search_path()
        if spec_dir not in path:
            lang_mgr.set_search_path([spec_dir] + path)
    _LANG_PATH_REGISTERED = True


def max_line_length(content):
    """Length in characters of the longest line in content."""
    if not content:
        return 0
    return max(len(line) for line in content.split('\n'))


def syntax_highlight_allowed(content):
    """False when a line is long enough that GtkSourceView's per-line
    highlighting would freeze the UI at open. See MAX_HIGHLIGHT_LINE_LEN."""
    return max_line_length(content) <= MAX_HIGHLIGHT_LINE_LEN


def apply_syntax_highlighting(buf, lang, content):
    """Attach lang to buf and enable highlighting unless content has a line
    long enough to stall the main loop. Returns True if highlighting is on.

    The language is attached either way, so turning highlighting back on later
    (tab right-click → Enable Syntax Highlighting) needs no language relookup."""
    if lang:
        buf.set_language(lang)
    enabled = bool(lang) and syntax_highlight_allowed(content)
    buf.set_highlight_syntax(enabled)
    return enabled


def _hl_log(msg):
    """Append a line to the highlight-debug log when SYNPAD_HL_DEBUG is set.
    SYNPAD_HL_DEBUG=1 writes to /tmp/synpad-highlight.log; any other value is
    treated as the target path. Flushed after every line so a SIGKILL or
    forced shutdown still leaves the tail on disk."""
    path = os.environ.get('SYNPAD_HL_DEBUG')
    if not path:
        return
    if path == '1':
        path = '/tmp/synpad-highlight.log'
    try:
        with open(path, 'a') as f:
            f.write(f"{time.monotonic():.6f} {msg}\n")
            f.flush()
    except Exception:
        pass


class EditorMixin:
    """Mixin for SynPadWindow — editor tabs, save, search, snippets."""

    def _create_editor_tab(self, remote_path, local_path, content,
                           is_local=False, server_guid=''):
        # Create source buffer with language
        lang_mgr = GtkSource.LanguageManager.get_default()
        _register_bundled_languages(lang_mgr)
        lang = self._detect_language(lang_mgr, remote_path)

        buf = GtkSource.Buffer()
        highlighted = apply_syntax_highlighting(buf, lang, content)

        # Set color scheme from config
        scheme = self._get_scheme()
        if scheme:
            buf.set_style_scheme(scheme)

        buf.set_text(content)
        buf.set_modified(False)

        # Highlight all case-sensitive matches of the current selection. Per
        # tab, with its own SearchSettings so it doesn't collide with the
        # global Ctrl+F context. Triggered when the selection is on a single
        # line, has no whitespace, and is at least 2 characters long.
        word_hl_settings = GtkSource.SearchSettings()
        word_hl_settings.set_case_sensitive(True)
        word_hl_settings.set_regex_enabled(False)
        word_hl_settings.set_wrap_around(True)
        word_hl_ctx = GtkSource.SearchContext.new(buf, word_hl_settings)
        word_hl_ctx.set_highlight(True)
        _hl_log(f"new-tab path={remote_path!r} chars={buf.get_char_count()} "
                f"ctx_id={id(word_hl_ctx):x} settings_id={id(word_hl_settings):x}")

        def _on_word_hl_mark_set(_buf, _iter, mark):
            try:
                name = mark.get_name() if mark else None
                if name not in ('insert', 'selection_bound'):
                    return
                bounds = buf.get_selection_bounds()
                if not bounds:
                    _hl_log(f"mark-set {name}: no-sel -> clear "
                            f"settings_id={id(word_hl_settings):x}")
                    word_hl_settings.set_search_text(None)
                    return
                start, end = bounds
                if start.get_line() != end.get_line():
                    _hl_log(f"mark-set {name}: multi-line -> clear")
                    word_hl_settings.set_search_text(None)
                    return
                text = buf.get_text(start, end, False)
                if len(text) < 2 or any(c.isspace() for c in text):
                    _hl_log(f"mark-set {name}: short/ws len={len(text)} -> clear")
                    word_hl_settings.set_search_text(None)
                    return
                _hl_log(f"mark-set {name}: apply text={text[:40]!r} "
                        f"len={len(text)} chars={buf.get_char_count()}")
                word_hl_settings.set_search_text(text)
                _hl_log(f"mark-set {name}: applied")
            except Exception:
                _hl_log("mark-set EXCEPTION:\n" + traceback.format_exc())
                raise

        buf.connect('mark-set', _on_word_hl_mark_set)

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
        view.add_css_class('editor-view')

        # Code completion — deferred until widget is realized
        def _setup_completion(*_args):
            ext = self._get_file_ext(remote_path)
            completion = view.get_completion()
            # GtkSourceView 5's GtkSource.Completion dropped the
            # 'show-headers' property entirely (verified by introspection —
            # it's not in the GObject property list at all, unlike GSV3/4);
            # there is nothing left to hide, so this call is simply removed.
            completion.set_property('select-on-show', True)

            # SYNPAD_COMPLETION_DEBUG=1 logs provider setup to stderr. The
            # completion machinery needs real focus and real key input, so it
            # cannot be exercised headlessly -- this is the only way to see
            # what it actually does in a running session.
            import os as _os
            _dbg = _os.environ.get('SYNPAD_COMPLETION_DEBUG')
            view._completion_setup_count = getattr(
                view, '_completion_setup_count', 0) + 1
            if _dbg:
                import sys as _sys
                print(f"[completion] _setup_completion run "
                      f"#{view._completion_setup_count} ext={ext!r} "
                      f"lang_table={'yes' if ext in COMPLETION_LANGS else 'no'}",
                      file=_sys.stderr, flush=True)

            providers, keep_alive = make_completion_providers(
                COMPLETION_LANGS.get(ext), view.get_buffer())
            for provider in providers:
                completion.add_provider(provider)

            if _dbg:
                import sys as _sys
                import completion as _C
                total = sum(
                    len(b.get_text(b.get_start_iter(), b.get_end_iter(),
                                   False).split())
                    for b in _C._LANG_SEEDS)
                print(f"[completion]   added {len(providers)} provider(s): "
                      f"{[p.get_title() for p in providers]}; "
                      f"indexed seed words={total}; "
                      f"warm pool remaining={len(_C._WARM_POOL)}",
                      file=_sys.stderr, flush=True)

                def _popup_state():
                    """What the completion popup actually is, right now."""
                    rows = 0; measures = []
                    try:
                        for top in Gtk.Window.get_toplevels():
                            def walk(w):
                                nonlocal rows
                                n = type(w).__name__
                                if 'CompletionListBoxRow' in n:
                                    rows += 1
                                elif 'GtkSourceCompletionList' == n:
                                    m = w.measure(Gtk.Orientation.HORIZONTAL, -1)
                                    measures.append((w.get_visible(), m.minimum,
                                                     m.natural, w.get_width()))
                                c = w.get_first_child()
                                while c:
                                    walk(c); c = c.get_next_sibling()
                            walk(top)
                    except Exception as e:
                        return f"<err {e}>"
                    return f"rows={rows} list(visible,min,nat,width)={measures}"

                def _watch(*_a):
                    ok, start, end = (True, None, None)
                    try:
                        b = view.get_buffer()
                        ins = b.get_iter_at_mark(b.get_insert())
                        line_start = ins.copy(); line_start.set_line_offset(0)
                        typed = b.get_text(line_start, ins, False)
                    except Exception as e:
                        typed = f"<err {e}>"
                    print(f"[completion] prefix={typed[-20:]!r} "
                          f"providers={len(getattr(view, '_completion_providers', []))} "
                          f"{_popup_state()}", file=_sys.stderr, flush=True)
                view.get_buffer().connect('changed', _watch)
            # CompletionWords does not own the buffers it scans, so the seeded
            # language buffer must outlive this function or its words vanish.
            view._completion_providers = providers
            view._completion_keepalive = keep_alive

        view.connect('realize', _setup_completion)

        # Intercept Ctrl+F/R/G/N/O/S and Tab (snippet expansion) before
        # GtkSourceView's/GtkText's own key handling. CAPTURE phase runs
        # top-down before the target widget's own (BUBBLE-phase) handling,
        # same intent as GTK3's plain connect() (which ran before the
        # class default handler).
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect('key-pressed', self._on_editor_key_press)
        view.add_controller(key_ctrl)

        # Right-click → "Ask Claude" submenu (presets + Custom). GTK4 has no
        # populate-popup signal; built once via set_extra_menu instead of
        # rebuilt on every popup.
        self._setup_editor_context_menu(view)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(view)

        # Remove welcome tab if present. It's untracked in self.tabs, so
        # this synchronously falls into the "unknown page" branch of
        # _on_tab_close_page below rather than the async confirm flow.
        if self.notebook.get_n_pages() == 1 and not self.tabs:
            self.notebook.close_page(self.notebook.get_nth_page(0))

        # Adw.TabView addresses tabs by TabPage object, not integer index —
        # title/close-button/context-menu are all driven off the TabPage
        # (via Adw.TabBar) instead of a hand-built tab_box widget, so there
        # is no more custom label/close-button/gesture construction here.
        page = self.notebook.append(scroll)
        page.set_title(os.path.basename(remote_path))
        self.notebook.set_selected_page(page)

        tab = OpenTab(remote_path, local_path, view, buf,
                      is_local=is_local, server_guid=server_guid)
        tab.highlight_suppressed = bool(lang) and not highlighted
        self.tabs[page] = tab

        # Track modification — reads tab.remote_path so renamed/saved tabs show correct name
        def on_modified_changed(_buf):
            name = os.path.basename(tab.remote_path)
            tab.modified = buf.get_modified()
            page.set_title(f"* {name}" if tab.modified else name)

        buf.connect('modified-changed', on_modified_changed)

        # Signature help popover — shows function signature under the cursor
        self._sighelp_attach(view, buf)

        if tab.highlight_suppressed:
            longest = max_line_length(content)
            self._set_status(
                f"Opened {remote_path} — syntax highlighting off "
                f"(longest line {longest:,} chars)")
            self._console_log(
                f"Syntax highlighting disabled for "
                f"'{os.path.basename(remote_path)}': longest line is "
                f"{longest:,} chars (limit {MAX_HIGHLIGHT_LINE_LEN:,}). "
                f"Highlighting it would freeze the editor. Enable it anyway "
                f"from the tab's right-click menu.", 'timestamp')
        else:
            self._set_status(f"Opened {remote_path}")
        self._update_symbols(tab)

    def _detect_language(self, lang_mgr, filepath):
        ext = filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
        mapping = {
            'php': 'php', 'js': 'js', 'ts': 'typescript',
            'tsx': 'typescript', 'mts': 'typescript', 'cts': 'typescript',
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

    def _add_welcome_tab(self):
        """Add the placeholder tab shown when no files are open. Pinned so
        Adw.TabBar gives it no close button — the old plain-Label tab had
        no close affordance either. Shared by window.py's initial
        _build_ui() and _on_tab_close_page() below (last tab closed)."""
        welcome = Gtk.Label(
            label="Connect to an FTP/SFTP server and open a file to start editing.")
        welcome.set_margin_top(40)
        page = self.notebook.append(welcome)
        page.set_title("Welcome")
        self.notebook.set_page_pinned(page, True)
        return page

    def _close_tab(self, page, callback=None):
        """Close a tab; if modified, confirm first ("Unsaved Changes" ->
        Yes/No). `callback` (if given) is invoked exactly once once the
        decision is resolved — whether the tab was actually closed or the
        close was declined — so callers that process several tabs in
        sequence (`_close_all_tabs`, `_close_all_tabs_except`) can chain
        through it.

        GTK4: `page` is now an Adw.TabPage, not an integer page number —
        see the module docstring. Closing itself is delegated to
        Adw.TabView.close_page(), which raises the 'close-page' signal
        (_on_tab_close_page, in window.py) synchronously; that's where the
        actual Yes/No confirmation and removal now live, so this method
        just registers the continuation and kicks the close off. The
        `page not in self.tabs` early-out preserves the old "close a page
        that's already gone" no-op instead of raising into TabView."""
        tab = self.tabs.get(page)
        self._debug(f"_close_tab: tab={'found: ' + os.path.basename(tab.remote_path) if tab else 'NOT FOUND'}, open tabs={len(self.tabs)}")
        if not tab:
            if callback:
                callback()
            return
        if callback:
            self._pending_close_callbacks[page] = callback
        self.notebook.close_page(page)

    # -- Tab close / context menu (Adw.TabView signals) -----------------------

    def _on_tab_close_page(self, view, page):
        """Central close handler for every close path: Adw.TabBar's
        built-in close button, the tab context menu's Close/Close All/
        Close All But This, and _close_tab()'s Ctrl+W caller — all of them
        end up at Adw.TabView.close_page(), which raises this signal.
        Returning True means we take responsibility for eventually calling
        close_page_finish() ourselves: synchronously for an unknown page
        (e.g. the pinned Welcome tab, never added to self.tabs) or an
        unmodified one, asynchronously once the confirm window below
        resolves for a modified one. This, plus TabPage being a stable
        object identity across reorders, is what let _reindex_tabs() and
        the page-reordered handler be deleted outright rather than ported
        — see the module docstring."""
        tab = self.tabs.get(page)
        if not tab:
            view.close_page_finish(page, True)
            return True

        def finish(do_close):
            if do_close:
                del self.tabs[page]
            view.close_page_finish(page, do_close)
            if do_close and view.get_n_pages() == 0:
                self._add_welcome_tab()
            cb = self._pending_close_callbacks.pop(page, None)
            if cb:
                cb()

        if not tab.modified:
            finish(True)
            return True

        # Modified — confirm before discarding changes. See module
        # docstring for why this is a plain Gtk.Window with explicit
        # buttons rather than Adw.AlertDialog.
        win = Gtk.Window(title="Unsaved Changes", transient_for=self, modal=True)
        win.set_default_size(360, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        win.set_child(box)

        label = Gtk.Label(
            label=f"'{os.path.basename(tab.remote_path)}' has unsaved "
                  f"changes. Close anyway?",
            wrap=True, halign=Gtk.Align.START)
        box.append(label)

        resolved = [False]

        def resolve(do_close):
            # Guards against being invoked twice — a button click calls
            # finish_and_close_window(), which calls resolve(); win.close()
            # below itself raises 'close-request', whose handler also
            # calls resolve(). The window is closed before finish() runs
            # (not after) so a raising continuation — finish() ends by
            # calling the caller's chained callback — can't strand this
            # modal=True window open.
            if resolved[0]:
                return
            resolved[0] = True
            win.close()
            finish(do_close)

        def finish_and_close_window(do_close):
            resolve(do_close)

        # ButtonsType.YES_NO rendered as [No, Yes] left-to-right in GTK3.
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_halign(Gtk.Align.END)
        btn_no = Gtk.Button(label="No")
        btn_no.connect('clicked', lambda _b: finish_and_close_window(False))
        btn_yes = Gtk.Button(label="Yes")
        btn_yes.add_css_class('suggested-action')
        btn_yes.connect('clicked', lambda _b: finish_and_close_window(True))
        btn_row.append(btn_no)
        btn_row.append(btn_yes)
        box.append(btn_row)

        # A bare Gtk.Window has no built-in Escape-to-close behavior
        # (unlike Gtk.Dialog) — wire it explicitly to the same "don't
        # close" path the No button takes.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                finish_and_close_window(False)
                return True
            return False
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        # Titlebar close/Alt-F4/destroyed transient parent all raise
        # 'close-request' without going through No/Yes/Escape — resolve
        # as "don't close" (same as No) so callback still fires exactly
        # once, and let default handling actually tear the window down.
        def on_close_request(_win):
            resolve(False)
            return False
        win.connect('close-request', on_close_request)

        win.present()
        return True

    def _on_tab_setup_menu(self, _view, page):
        """Built-in Adw.TabView context menu (right-click on a tab), set up
        fresh each time it's about to open. `page` is None when the menu is
        closing.

        GTK4: Gtk.Menu/Gtk.MenuItem -> Gio.Menu model + Gio.SimpleAction,
        same pattern as local_files.py's/remote.py's tree context menus —
        the difference is Adw.TabView owns the popover itself (via
        set_menu_model()/'setup-menu'), so there's no manual
        Gtk.PopoverMenu or right-click gesture to wire up here. Native
        middle-click-to-close and the tab drag-reorder are both handled by
        Adw.TabBar without any code on our side."""
        if page is None:
            return
        tab = self.tabs.get(page)
        if not tab:
            return
        reload_label = "Reload from Disk" if tab.is_local else "Reload from Server"

        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()

        def add_action(section, action_name, label, callback, enabled=True):
            action = Gio.SimpleAction.new(action_name, None)
            action.connect('activate', lambda _a, _p: callback())
            action.set_enabled(enabled)
            group.add_action(action)
            section.append(label, f'tabctx.{action_name}')

        sec1 = Gio.Menu()
        # A never-saved local tab has nothing on disk to reload from.
        reload_enabled = not (tab.is_local and
                              (not tab.local_path or not os.path.exists(tab.local_path)))
        add_action(sec1, 'reload', reload_label,
                   lambda: self._confirm_then_refresh(tab), enabled=reload_enabled)

        # Only offered when a long line made us skip highlighting — turning it
        # on can block the UI for a long time, so it stays an explicit choice.
        if tab.highlight_suppressed:
            add_action(sec1, 'enable_hl', "Enable Syntax Highlighting (slow)",
                       lambda: self._force_highlight(tab))
        menu.append_section(None, sec1)

        sec2 = Gio.Menu()
        add_action(sec2, 'close', "Close", lambda: self._close_tab(page))
        add_action(sec2, 'close_all', "Close All", lambda: self._close_all_tabs())
        add_action(sec2, 'close_others', "Close All But This",
                   lambda: self._close_all_tabs_except(page))
        menu.append_section(None, sec2)

        self.notebook.set_menu_model(menu)
        self.notebook.insert_action_group('tabctx', group)

    def _force_highlight(self, tab):
        """Turn syntax highlighting on for a tab where a long line suppressed it.

        Confirms first: GtkSourceView's per-line cost means this can lock the UI
        for a minute or more on the very files that triggered the guard.

        GTK4: a one-shot Yes/No decision with nothing chained on it, so
        Adw.AlertDialog is safe here (see module docstring) — if Escape/
        close never invokes the callback, highlighting simply stays off,
        the same outcome as clicking Cancel."""
        buf = tab.buffer
        longest = max_line_length(
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True))
        dlg = Adw.AlertDialog(
            heading="Enable syntax highlighting?",
            body=(f"{os.path.basename(tab.remote_path)} has a line of "
                  f"{longest:,} characters. Highlighting it may freeze "
                  f"SynPad for a long time and cannot be interrupted.\n\n"
                  f"Enable anyway?"),
        )
        dlg.add_response('cancel', "Cancel")
        dlg.add_response('ok', "OK")
        dlg.set_default_response('cancel')
        dlg.set_close_response('cancel')

        def on_response(dlg, res):
            try:
                response = dlg.choose_finish(res)
            except Exception:
                return
            if response != 'ok':
                return

            self._set_status(
                f"Highlighting {os.path.basename(tab.remote_path)} — this may take a while...")
            # Let the status text paint before the main loop stalls.
            ctx = GLib.MainContext.default()
            while ctx.pending():
                ctx.iteration(False)

            started = time.monotonic()
            buf.set_highlight_syntax(True)
            tab.highlight_suppressed = False
            self._console_log(
                f"Syntax highlighting forced on for "
                f"'{os.path.basename(tab.remote_path)}' "
                f"(longest line {longest:,} chars) after "
                f"{time.monotonic() - started:.1f}s", 'timestamp')
            self._set_status(f"Highlighting enabled for {os.path.basename(tab.remote_path)}")

        dlg.choose(self, None, on_response)

    def _close_all_tabs(self):
        """Close all open tabs, confirming per modified tab.

        GTK4 has no blocking confirm dialog, so this walks a precomputed
        queue (rightmost tab first, matching the old reverse-page-number
        order) one tab at a time — each tab's _close_tab confirmation
        continuation advances to the next. Adw.TabPage keys stay valid
        regardless of what order they're removed in (unlike the old
        integer page scheme), so precomputing the list up front is
        safe here for the same reason _reindex_tabs is no longer needed
        at all — see the module docstring."""
        pages = sorted(self.tabs.keys(), key=self.notebook.get_page_position, reverse=True)
        self._close_tab_chain(pages)

    def _close_all_tabs_except(self, keep_page):
        """Close all tabs except the given page. See _close_all_tabs for
        why this is now a queued async chain instead of a synchronous loop."""
        keep_tab = self.tabs.get(keep_page)
        if not keep_tab:
            return
        keep_path = keep_tab.remote_path
        pages = sorted(self.tabs.keys(), key=self.notebook.get_page_position, reverse=True)
        queue = [p for p in pages
                 if self.tabs.get(p) and self.tabs[p].remote_path != keep_path]
        self._close_tab_chain(queue)

    def _close_tab_chain(self, queue):
        """Close tabs in `queue` (Adw.TabPages, rightmost first) one at a
        time, waiting for each one's unsaved-changes confirmation to
        resolve before moving on to the next."""
        if not queue:
            return
        page, rest = queue[0], queue[1:]
        self._close_tab(page, callback=lambda: self._close_tab_chain(rest))

    # -- Reload tab contents --------------------------------------------------

    def _confirm_then_refresh(self, tab):
        """Reload a tab's contents from disk (local) or server (remote).
        Warns and requires confirmation before discarding unsaved edits.

        GTK4: a one-shot Cancel/Reload decision with nothing chained on it,
        so Adw.AlertDialog is safe here (see module docstring)."""
        if tab.modified:
            src = "disk" if tab.is_local else "server"
            dlg = Adw.AlertDialog(
                heading="File has unsaved changes",
                body=(f"{os.path.basename(tab.remote_path)}\n\n"
                      f"Reload from {src} and discard your unsaved changes?"),
            )
            dlg.add_response('cancel', "Cancel")
            dlg.add_response('reload', "Reload (discard my changes)")
            dlg.set_default_response('cancel')
            dlg.set_close_response('cancel')

            def on_response(dlg, res):
                try:
                    response = dlg.choose_finish(res)
                except Exception:
                    return
                if response == 'reload':
                    self._refresh_tab(tab)

            dlg.choose(self, None, on_response)
            return
        self._refresh_tab(tab)

    def _capture_view_state(self, tab):
        """Snapshot cursor offset + vertical scroll so a reload can restore them."""
        buf = tab.buffer
        offset = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        scroll_value = 0.0
        try:
            vadj = tab.source_view.get_vadjustment()
            if vadj is not None:
                scroll_value = vadj.get_value()
        except Exception:
            pass
        return offset, scroll_value

    def _apply_refreshed_content(self, tab, content, offset, scroll_value):
        """Replace buffer text and restore cursor + scroll (main thread only)."""
        buf = tab.buffer
        # Re-evaluate highlighting: the reloaded content may have gained (or
        # lost) a line long enough to stall the main loop.
        highlighted = apply_syntax_highlighting(buf, buf.get_language(), content)
        tab.highlight_suppressed = bool(buf.get_language()) and not highlighted
        buf.set_text(content)
        buf.set_modified(False)
        # Clamp the cursor in case the file got shorter on disk/server.
        offset = max(0, min(offset, buf.get_char_count()))
        buf.place_cursor(buf.get_iter_at_offset(offset))

        # Restore scroll once the view has re-laid-out with the new content.
        def _restore_scroll():
            try:
                vadj = tab.source_view.get_vadjustment()
                if vadj is not None:
                    max_value = max(0.0, vadj.get_upper() - vadj.get_page_size())
                    vadj.set_value(min(scroll_value, max_value))
            except Exception:
                pass
            return False
        GLib.idle_add(_restore_scroll)

    def _refresh_tab(self, tab):
        """Re-read the file behind a tab. Local reads are synchronous; remote
        downloads run off the UI thread (the SFTP/FTP op can block)."""
        offset, scroll_value = self._capture_view_state(tab)

        if tab.is_local:
            try:
                with open(tab.local_path, 'r', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                self._show_error("Reload Failed", str(e))
                return
            self._apply_refreshed_content(tab, content, offset, scroll_value)
            self._set_status(
                f"Reloaded {os.path.basename(tab.local_path)} from disk")
            self._console_log(f"RELOAD (disk) {tab.local_path}", 'success')
            return

        # Remote tab — needs a live connection.
        if not self.ftp_mgr or not self.ftp_mgr.connected:
            self._show_error("Not Connected", "Connect to the server first.")
            return
        remote_path = tab.remote_path
        local_path = tab.local_path
        mgr = self.ftp_mgr
        self._set_status(f"Reloading {remote_path} from server...")
        self._console_log(f"RELOAD (server) GET {remote_path}")

        def work():
            try:
                r_mtime = mgr.get_remote_mtime(remote_path)
                r_size = mgr.get_remote_size(remote_path)
                mgr.download(remote_path, local_path)
                with open(local_path, 'rb') as f:
                    r_hash = hashlib.sha256(f.read()).hexdigest()
                with open(local_path, 'r', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                GLib.idle_add(self._show_error, "Reload Failed", str(e))
                GLib.idle_add(self._set_status, "Reload failed")
                return

            def _finish():
                tab.remote_mtime = r_mtime
                tab.remote_size = r_size
                tab.remote_hash = r_hash
                self._apply_refreshed_content(tab, content, offset, scroll_value)
                self._set_status(
                    f"Reloaded {os.path.basename(remote_path)} from server")
                self._console_log(
                    f"RELOAD (server) done: {remote_path}", 'success')
            GLib.idle_add(_finish)

        threading.Thread(target=work, daemon=True).start()

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
        """Open a file from the local filesystem.

        GTK4: Gtk.FileChooserDialog + .run() -> the native async
        Gtk.FileDialog (see module docstring). The decision logic (only
        open when a file was actually chosen) is unchanged."""
        dlg = Gtk.FileDialog()
        dlg.set_title("Open Local File")

        filt_all = Gtk.FileFilter()
        filt_all.set_name("All files")
        filt_all.add_pattern("*")

        filt_code = Gtk.FileFilter()
        filt_code.set_name("Code files")
        for ext in ['php', 'js', 'ts', 'jsx', 'tsx', 'py', 'html', 'htm',
                     'css', 'json', 'xml', 'sql', 'sh', 'yml', 'yaml',
                     'md', 'txt', 'ini', 'conf', 'env']:
            filt_code.add_pattern(f"*.{ext}")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt_all)
        filters.append(filt_code)
        dlg.set_filters(filters)
        dlg.set_default_filter(filt_all)

        # Remember last folder
        last_dir = self.config.get('last_save_dir', '')
        if last_dir and os.path.isdir(last_dir):
            dlg.set_initial_folder(Gio.File.new_for_path(last_dir))

        dlg.open(self, None, self._on_open_local_file_response)

    def _on_open_local_file_response(self, dlg, result):
        try:
            file = dlg.open_finish(result)
        except GLib.Error:
            return  # cancelled, or the picker failed
        if file is None:
            return
        filepath = file.get_path()
        if not filepath:
            return
        self.config['last_save_dir'] = os.path.dirname(filepath)
        save_config(self.config)
        self._open_local_file(filepath)

    def _open_local_file(self, filepath):
        """Open a local file in an editor tab."""
        # Check if already open
        for page, tab in self.tabs.items():
            if tab.is_local and tab.local_path == filepath:
                self.notebook.set_selected_page(page)
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
        """Update the tab label text for a given tab.

        GTK4: Adw.TabPage.set_title() replaces the old widget-tree walk to
        find the tab's Gtk.Label — the title now lives on the TabPage
        itself, not a hand-built tab_box, so there's nothing left to walk."""
        page_widget = tab.source_view.get_parent()  # ScrolledWindow
        page = self.notebook.get_page(page_widget) if page_widget else None
        if page is None:
            return
        page.set_title(f"* {new_name}" if tab.modified else new_name)

    def _on_save(self, _btn):
        """Save the current file — locally or via upload depending on type."""
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if not tab:
            self._set_status("No file open to save")
            return
        if tab.is_local:
            self._on_save_local(tab)
        else:
            self._on_save_upload(None)

    def _on_save_local(self, tab):
        """Save a local file to disk. If untitled, ask where to save first.

        GTK4: Gtk.FileChooserDialog + .run() -> the native async
        Gtk.FileDialog (see module docstring); Gtk.FileDialog.save()
        confirms overwrite itself, replacing the old
        set_do_overwrite_confirmation(True). The actual disk write (the
        part of this method that used to run unconditionally after the
        dialog, whether or not one was shown) is now the shared
        `_write_local_file` helper so the untitled path can call it from
        the picker's async response."""
        if not tab.local_path:
            dlg = Gtk.FileDialog()
            dlg.set_title("Save As")
            dlg.set_initial_name(tab.remote_path)  # "Untitled 1" etc.
            last_dir = self.config.get('last_save_dir', '')
            if last_dir and os.path.isdir(last_dir):
                dlg.set_initial_folder(Gio.File.new_for_path(last_dir))
            dlg.save(self, None,
                     lambda d, r: self._on_save_local_response(d, r, tab))
            return

        self._write_local_file(tab)

    def _on_save_local_response(self, dlg, result, tab):
        try:
            file = dlg.save_finish(result)
        except GLib.Error:
            return  # cancelled, or the picker failed
        if file is None:
            return
        filepath = file.get_path()
        if not filepath:
            return
        # Save the folder for next time
        self.config['last_save_dir'] = os.path.dirname(filepath)
        save_config(self.config)
        tab.local_path = filepath
        tab.remote_path = filepath
        self._update_tab_label(tab, os.path.basename(filepath))
        self._write_local_file(tab)

    def _write_local_file(self, tab):
        """Write the buffer's content to tab.local_path. Shared tail of
        _on_save_local for both the already-has-a-path case and the
        untitled-file Save As continuation."""
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
            self._pending_upload = (tab, page, max_mb)
            vals = dict(srv)
            stored_pwd = secrets_store.get_password(srv['guid'])
            if stored_pwd is not None:
                vals['password'] = stored_pwd
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

        # No server switch needed — check connection.
        # is_alive() probes for silently-dropped sockets (NAT/idle timeouts)
        # so we fall into the reconnect branch instead of hanging the upload
        # on a dead connection.
        was_connected = bool(self.ftp_mgr and self.ftp_mgr.connected)
        if not self.ftp_mgr or not self.ftp_mgr.connected or not self.ftp_mgr.is_alive():
            if was_connected:
                self._console_log(
                    "Connection lost — reconnecting before upload...", 'error')
            if tab_guid:
                srv = find_server_by_guid(self.config, tab_guid)
                if srv:
                    self._pending_upload = (tab, page, max_mb)
                    vals = dict(srv)
                    stored_pwd = secrets_store.get_password(srv['guid'])
                    if stored_pwd is not None:
                        vals['password'] = stored_pwd
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
        self._do_upload(tab, page, max_mb)

    def _do_upload(self, tab, page, max_mb):
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
                    self._console_log("PUT step: checking remote mtime")
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
                        """GTK4: a custom-content dialog (icon + text + four
                        buttons), so per the migration plan's dialog shapes
                        this is a plain Gtk.Window, not Adw.AlertDialog. This
                        runs on the main thread while `work()` (a background
                        thread) blocks on result_q.get() — closing the
                        window any way other than a button click (Escape,
                        titlebar, Alt-F4) must still push to the queue or
                        that thread hangs forever, so both are wired to
                        Cancel explicitly."""
                        win = Gtk.Window(
                            title="File Modified on Server",
                            transient_for=self,
                            modal=True,
                        )
                        win.set_default_size(450, -1)

                        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                        box.set_margin_start(12)
                        box.set_margin_end(12)
                        box.set_margin_top(12)
                        box.set_margin_bottom(12)
                        win.set_child(box)

                        # Warning icon + text
                        msg_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                        icon = Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
                        icon.set_pixel_size(48)
                        msg_box.append(icon)

                        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                        title_lbl = Gtk.Label()
                        title_lbl.set_markup("<b>File Modified on Server</b>")
                        title_lbl.set_halign(Gtk.Align.START)
                        text_box.append(title_lbl)

                        desc_lbl = Gtk.Label(
                            label=f"'{os.path.basename(tab.remote_path)}' has been "
                                  f"modified on the server since you opened it.")
                        desc_lbl.set_halign(Gtk.Align.START)
                        desc_lbl.set_wrap(True)
                        text_box.append(desc_lbl)
                        msg_box.append(text_box)
                        box.append(msg_box)

                        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

                        # Buttons
                        btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

                        resolved = [False]

                        def resolve(choice):
                            if resolved[0]:
                                return
                            resolved[0] = True
                            result_q.put(choice)

                        def finish(choice):
                            resolve(choice)
                            win.close()

                        btn_overwrite = Gtk.Button(label="Overwrite server with my changes")
                        btn_overwrite.connect('clicked', lambda _b: finish(RESP_OVERWRITE))
                        btn_box.append(btn_overwrite)

                        btn_remote = Gtk.Button(label="Discard my changes, use server version")
                        btn_remote.connect('clicked', lambda _b: finish(RESP_USE_REMOTE))
                        btn_box.append(btn_remote)

                        btn_compare = Gtk.Button(label="Compare both versions")
                        btn_compare.add_css_class('suggested-action')
                        btn_compare.connect('clicked', lambda _b: finish(RESP_COMPARE))
                        btn_box.append(btn_compare)

                        btn_cancel = Gtk.Button(label="Cancel")
                        btn_cancel.connect('clicked', lambda _b: finish(RESP_CANCEL))
                        btn_box.append(btn_cancel)

                        box.append(btn_box)

                        def on_key(_ctrl, keyval, _keycode, _state):
                            if keyval == Gdk.KEY_Escape:
                                finish(RESP_CANCEL)
                                return True
                            return False
                        key_ctrl = Gtk.EventControllerKey()
                        key_ctrl.connect('key-pressed', on_key)
                        win.add_controller(key_ctrl)

                        def on_close_request(_win):
                            resolve(RESP_CANCEL)
                            return False
                        win.connect('close-request', on_close_request)

                        win.present()

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
                                # GTK4: TextBuffer.set_text() internally begins
                                # its own "irreversible action", which now
                                # conflicts with an already-open user action
                                # ("Cannot begin irreversible action while in
                                # user action") — delete+insert achieves the
                                # same single-undo-step full replace without it.
                                tab.buffer.begin_user_action()
                                tab.buffer.delete(tab.buffer.get_start_iter(),
                                                  tab.buffer.get_end_iter())
                                tab.buffer.insert(tab.buffer.get_start_iter(), remote_content)
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
                                    page, max_mb, mgr)
                                self.item_save.set_sensitive(True)
                            GLib.idle_add(_show_compare)
                        else:
                            GLib.idle_add(self.item_save.set_sensitive, True)
                        return
                    # RESP_OVERWRITE falls through to upload

                self._console_log("PUT step: uploading")
                mgr.upload(tab.remote_path, tab.local_path, max_mb)
                self._console_log("PUT step: upload finished, updating stats")
                # Update stored stats after successful upload
                tab.remote_mtime = mgr.get_remote_mtime(tab.remote_path)
                tab.remote_size = mgr.get_remote_size(tab.remote_path)
                # Hash the uploaded content
                with open(tab.local_path, 'rb') as f:
                    tab.remote_hash = hashlib.sha256(f.read()).hexdigest()
                GLib.idle_add(self._on_upload_done, tab, page)
            except Exception as e:
                GLib.idle_add(self._on_upload_failed, f"{type(e).__name__}: {e}")

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
            tab, page, max_mb = self._pending_upload
            self._pending_upload = None
            # Upload, and reload tree to the file's directory after upload completes
            self._pending_tree_reload = (vals, tab.remote_path)
            self._do_upload(tab, page, max_mb)
        else:
            # No pending upload — just reload tree
            start_dir = vals.get('home_directory', '').strip()
            if not start_dir and self.ftp_mgr:
                start_dir = self.ftp_mgr.home_dir
            if start_dir:
                self._load_tree(start_dir)

    def _on_upload_done(self, tab, page):
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
        self._show_error("Upload Failed", str(err))

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
        )
        win.set_default_size(420, -1)
        win.set_resizable(False)
        # GTK4 removed set_keep_above()/set_position(): window placement and
        # stacking are the compositor's job now, no direct replacement.

        def on_close_request(_win):
            self._on_search_close()
            return True
        win.connect('close-request', on_close_request)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', self._on_search_window_key)
        win.add_controller(key_ctrl)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        # --- Find row ---
        find_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        find_row.append(Gtk.Label(label="Find:", width_chars=8, halign=Gtk.Align.END))

        self._search_entry = Gtk.Entry(hexpand=True)
        self._search_entry.connect('activate', self._on_search_next)
        self._search_entry.connect('changed', self._on_search_changed)
        find_row.append(self._search_entry)

        btn_prev = Gtk.Button()
        btn_prev.set_icon_name('go-up-symbolic')
        btn_prev.add_css_class('flat')
        btn_prev.set_tooltip_text("Previous (Shift+Enter)")
        btn_prev.connect('clicked', self._on_search_prev)
        find_row.append(btn_prev)

        btn_next = Gtk.Button()
        btn_next.set_icon_name('go-down-symbolic')
        btn_next.add_css_class('flat')
        btn_next.set_tooltip_text("Next (Enter)")
        btn_next.connect('clicked', self._on_search_next)
        find_row.append(btn_next)

        box.append(find_row)

        # --- Replace row ---
        if show_replace:
            replace_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            replace_row.append(
                Gtk.Label(label="Replace:", width_chars=8, halign=Gtk.Align.END))

            self._replace_entry = Gtk.Entry(hexpand=True)
            replace_row.append(self._replace_entry)

            btn_replace = Gtk.Button(label="Replace")
            btn_replace.connect('clicked', self._on_replace_one)
            replace_row.append(btn_replace)

            btn_replace_all = Gtk.Button(label="All")
            btn_replace_all.connect('clicked', self._on_replace_all)
            replace_row.append(btn_replace_all)

            box.append(replace_row)

        # --- Options row ---
        opt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        opt_row.set_margin_start(70)

        self._chk_match_case = Gtk.CheckButton(label="Match case")
        self._chk_match_case.connect('toggled', self._on_search_option_changed)
        opt_row.append(self._chk_match_case)

        self._chk_regex = Gtk.CheckButton(label="Regex")
        self._chk_regex.connect('toggled', self._on_search_option_changed)
        opt_row.append(self._chk_regex)

        self._search_match_label = Gtk.Label(label="")
        self._search_match_label.set_hexpand(True)
        self._search_match_label.set_halign(Gtk.Align.END)
        opt_row.append(self._search_match_label)

        box.append(opt_row)

        win.set_child(box)
        self._search_window = win
        self._search_show_replace = show_replace

        # Search context
        self._search_settings = GtkSource.SearchSettings()
        self._search_settings.set_wrap_around(True)
        self._search_context = None

    def _on_search_window_key(self, _ctrl, keyval, _keycode, state):
        """Handle keys in the search window."""
        if keyval == Gdk.KEY_Escape:
            self._on_search_close()
            return True
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if keyval == Gdk.KEY_Return and shift:
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
            self._search_window.present()

        # Pre-fill with selected text
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
            page = self.notebook.get_selected_page()
            tab = self.tabs.get(page)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if tab:
            tab.source_view.grab_focus()

    # -- Pretty Print ---------------------------------------------------------

    def _on_pretty_print_json(self):
        """Pretty print the current buffer as JSON."""
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if not tab:
            return
        buf = tab.buffer
        start = buf.get_start_iter()
        end = buf.get_end_iter()
        text = buf.get_text(start, end, True)

        try:
            parsed = json.loads(text)
            pretty = json.dumps(parsed, indent=4, ensure_ascii=False)
            # GTK4: set_text() begins its own "irreversible action", which
            # conflicts with an already-open user action — delete+insert
            # keeps this a single undo step without it (see _do_upload's
            # _load_remote for the same fix).
            buf.begin_user_action()
            buf.delete(buf.get_start_iter(), buf.get_end_iter())
            buf.insert(buf.get_start_iter(), pretty)
            buf.end_user_action()
            self._set_status("JSON formatted")
        except json.JSONDecodeError as e:
            self._show_error("JSON Error", f"Invalid JSON:\n\n{e}")

    def _on_pretty_print_xml(self):
        """Pretty print the current buffer as XML."""
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
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
            buf.delete(buf.get_start_iter(), buf.get_end_iter())
            buf.insert(buf.get_start_iter(), pretty)
            buf.end_user_action()
            self._set_status("XML formatted")
        except Exception as e:
            self._show_error("XML Error", f"Invalid XML:\n\n{e}")

    # -- Go to Line -----------------------------------------------------------

    def _on_goto_line(self):
        """Show a small dialog to jump to a line number.

        GTK4: Gtk.Dialog + .run() -> a plain Gtk.Window with explicit
        Escape handling (this dialog has no Cancel button either, per the
        migration plan's dialog shapes for custom content — closing it any
        way just does nothing, same as before)."""
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if not tab:
            return

        win = Gtk.Window(title="Go to Line", transient_for=self, modal=True)
        win.set_default_size(250, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        win.set_child(box)

        total = tab.buffer.get_line_count()
        current = tab.buffer.get_iter_at_mark(
            tab.buffer.get_insert()).get_line() + 1

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(Gtk.Label(label="Line:"))

        spin = Gtk.SpinButton.new_with_range(1, total, 1)
        spin.set_value(current)
        spin.set_hexpand(True)
        row.append(spin)

        row.append(Gtk.Label(label=f"/ {total}"))

        def go(*_a):
            line = int(spin.get_value()) - 1
            ok, target = tab.buffer.get_iter_at_line(line)
            if ok:
                tab.buffer.place_cursor(target)
                tab.source_view.scroll_to_iter(target, 0.1, True, 0.0, 0.5)
                tab.source_view.grab_focus()
            win.close()

        spin.connect('activate', go)

        btn_go = Gtk.Button(label="Go")
        btn_go.add_css_class('suggested-action')
        btn_go.connect('clicked', go)
        row.append(btn_go)

        box.append(row)

        # A bare Gtk.Window has no built-in Escape-to-close behavior.
        def on_key(_ctrl, keyval, _keycode, _state):
            if keyval == Gdk.KEY_Escape:
                win.close()
                return True
            return False
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', on_key)
        win.add_controller(key_ctrl)

        win.present()

    # -- Docblock Generation ---------------------------------------------------

    def _try_expand_snippet(self, view):
        """Try to expand /// or /** on the current line. Returns True if expanded."""
        buf = view.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert())
        line_num = cursor.get_line()

        _, line_start = buf.get_iter_at_line(line_num)
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
        _, line_start = buf.get_iter_at_line(line_num)
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
        page = self.notebook.get_selected_page()
        tab = self.tabs.get(page)
        if not tab:
            return False
        ext = self._get_file_ext(tab.remote_path)
        if ext not in ('php', 'js', 'jsx', 'ts', 'tsx'):
            return False

        # Read the next non-empty line to find the function signature
        total_lines = buf.get_line_count()
        func_line = None
        for i in range(line_num + 1, min(line_num + 5, total_lines)):
            _, next_start = buf.get_iter_at_line(i)
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

    def _setup_editor_context_menu(self, view):
        """Add an 'Ask Claude' submenu to the editor's right-click context menu.

        GTK4 removed GtkTextView's 'populate-popup' signal entirely
        (GtkSource.View inherits from GtkTextView) — context-menu
        customization is now declarative via
        Gtk.TextView.set_extra_menu(Gio.Menu), which GTK merges into the
        view's built-in cut/copy/paste popup as its own trailing section
        every time it's shown. Built once per view here (the presets are
        static), instead of being rebuilt on every popup like the old
        populate-popup handler."""
        from claude_tab import PRESETS
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()
        submenu = Gio.Menu()
        for key, label, _prompt in PRESETS:
            action_name = f'ask_{key}'
            action = Gio.SimpleAction.new(action_name, None)
            action.connect('activate', lambda _a, _p, k=key: self._claude_handle_trigger(k))
            group.add_action(action)
            submenu.append(label, f'editorctx.{action_name}')
        menu.append_submenu("Ask Claude", submenu)
        view.insert_action_group('editorctx', group)
        view.set_extra_menu(menu)

    def _on_editor_key_press(self, ctrl, keyval, keycode, state):
        """Intercept keys on the source view before GtkSourceView/GtkText's
        own key handling (see the CAPTURE-phase controller set up in
        _create_editor_tab)."""
        view = ctrl.get_widget()
        # Tab on /// or /** line → expand snippet
        if keyval == Gdk.KEY_Tab:
            # Hide completion popup first so it doesn't consume the Tab
            completion = view.get_completion()
            completion.hide()
            if self._try_expand_snippet(view):
                return True
        control = state & Gdk.ModifierType.CONTROL_MASK
        if control and keyval == Gdk.KEY_f:
            self._show_search(show_replace=False)
            return True
        if control and keyval == Gdk.KEY_r:
            self._show_search(show_replace=True)
            return True
        if control and keyval == Gdk.KEY_g:
            self._on_goto_line()
            return True
        if control and keyval == Gdk.KEY_n:
            self._on_new_local_file()
            return True
        if control and keyval == Gdk.KEY_o:
            self._on_open_local_file()
            return True
        if control and keyval == Gdk.KEY_s:
            self._on_save(None)
            return True
        return False
