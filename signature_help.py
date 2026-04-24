"""Signature help popover — shows function signature with current parameter
bolded while the cursor is inside a call.

Lookups use the language-specific completion dicts from completion.py. The
popover is a single Gtk.Popover reused across all tabs; it re-anchors to the
current view on each update and hides automatically once the cursor leaves
the call or the view loses focus."""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

from completion import PHP_COMPLETIONS, JS_COMPLETIONS


CONTROL_KEYWORDS = {
    'if', 'while', 'for', 'foreach', 'switch', 'catch', 'elseif', 'else',
    'return', 'print', 'echo', 'and', 'or', 'not', 'xor',
    'isset', 'empty', 'unset', 'array', 'list', 'declare', 'use',
    'function', 'class', 'interface', 'trait', 'namespace',
}


class SignatureHelpMixin:
    """Attach to SynPadWindow's mixin chain. Lazy-initializes on first use."""

    def _sighelp_ensure_state(self):
        if getattr(self, '_sighelp_initialized', False):
            return
        self._sighelp_initialized = True
        self._sighelp_popover = None
        self._sighelp_label = None
        self._sighelp_pending = False

    def _sighelp_attach(self, view, buf):
        """Wire signature-help listeners to this view/buffer."""
        self._sighelp_ensure_state()
        buf.connect('insert-text', lambda *_a: self._sighelp_schedule(view))
        buf.connect('delete-range', lambda *_a: self._sighelp_schedule(view))
        buf.connect('notify::cursor-position',
                    lambda *_a: self._sighelp_schedule(view))
        view.connect('focus-out-event', lambda *_a: self._sighelp_hide())

    # -- Scheduling / entry point --------------------------------------------

    def _sighelp_schedule(self, view):
        if self._sighelp_pending:
            return
        self._sighelp_pending = True
        GLib.idle_add(self._sighelp_run, view)

    def _sighelp_run(self, view):
        self._sighelp_pending = False
        try:
            self._sighelp_update(view)
        except Exception:
            # Never let sighelp break the editor
            self._sighelp_hide()
        return False

    def _sighelp_update(self, view):
        buf = view.get_buffer()
        lang = buf.get_language()
        lang_id = lang.get_id() if lang else None

        analysis = self._sighelp_analyze(buf)
        if analysis is None:
            self._sighelp_hide()
            return
        func_name, _paren_pos, param_index = analysis

        sig = self._sighelp_lookup(func_name, lang_id)
        if not sig:
            self._sighelp_hide()
            return

        markup = '<tt>' + self._sighelp_format(sig, param_index) + '</tt>'
        self._sighelp_ensure_popover(view)
        self._sighelp_label.set_markup(markup)
        self._sighelp_position(view)
        self._sighelp_popover.popup()

    def _sighelp_hide(self):
        pop = getattr(self, '_sighelp_popover', None)
        if pop is not None and pop.get_visible():
            pop.popdown()

    # -- Analysis: walk buffer up to cursor, find innermost call -------------

    def _sighelp_analyze(self, buf):
        """Return (func_name, paren_pos, param_index) or None."""
        cursor_iter = buf.get_iter_at_mark(buf.get_insert())
        start = buf.get_start_iter()
        text = buf.get_text(start, cursor_iter, False)

        # Stack of open grouping chars with per-level comma counts
        stack = []  # list of [char, pos, commas]
        in_str = None  # None or the quote char that opened the string
        in_line_comment = False
        in_block_comment = False
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            nxt = text[i + 1] if i + 1 < n else ''

            if in_line_comment:
                if c == '\n':
                    in_line_comment = False
                i += 1
                continue
            if in_block_comment:
                if c == '*' and nxt == '/':
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str is not None:
                if c == '\\':
                    i += 2
                    continue
                if c == in_str:
                    in_str = None
                i += 1
                continue

            if c == '/' and nxt == '/':
                in_line_comment = True
                i += 2
                continue
            if c == '/' and nxt == '*':
                in_block_comment = True
                i += 2
                continue
            if c == '#':
                in_line_comment = True
                i += 1
                continue
            if c in ('"', "'", '`'):
                in_str = c
                i += 1
                continue

            if c in '([{':
                stack.append([c, i, 0])
            elif c in ')]}':
                if stack:
                    stack.pop()
            elif c == ',' and stack:
                stack[-1][2] += 1

            i += 1

        # Innermost unmatched `(`
        paren_pos = -1
        param_index = 0
        for entry in reversed(stack):
            if entry[0] == '(':
                paren_pos = entry[1]
                param_index = entry[2]
                break
        if paren_pos < 0:
            return None

        # Read identifier before `(`
        j = paren_pos - 1
        while j >= 0 and text[j] in ' \t':
            j -= 1
        end_id = j + 1
        while j >= 0 and (text[j].isalnum() or text[j] in '_\\'):
            j -= 1
        func_name = text[j + 1:end_id]
        if not func_name:
            return None
        if '\\' in func_name:
            func_name = func_name.rsplit('\\', 1)[-1]
        if func_name in CONTROL_KEYWORDS:
            return None

        return func_name, paren_pos, param_index

    # -- Dict lookup ---------------------------------------------------------

    def _sighelp_lookup(self, func_name, lang_id):
        if lang_id == 'php':
            d = PHP_COMPLETIONS
        elif lang_id in ('js', 'javascript', 'typescript', 'ts'):
            d = JS_COMPLETIONS
        else:
            return None
        sig = d.get(func_name)
        if not isinstance(sig, str):
            return None
        if not sig.startswith('('):
            return None
        return sig

    # -- Markup: bold the current parameter ----------------------------------

    def _sighelp_format(self, sig, param_index):
        # Split out param list from optional return-type suffix
        depth = 0
        close = -1
        for i, c in enumerate(sig):
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close < 0:
            return GLib.markup_escape_text(sig)

        params_str = sig[1:close]
        rest = sig[close + 1:]

        params = []
        cur = ''
        depth = 0
        for c in params_str:
            if c in '([{':
                depth += 1
            elif c in ')]}':
                depth -= 1
            if c == ',' and depth == 0:
                params.append(cur.strip())
                cur = ''
            else:
                cur += c
        if cur.strip():
            params.append(cur.strip())

        pieces = []
        for i, p in enumerate(params):
            esc = GLib.markup_escape_text(p)
            if i == param_index:
                pieces.append(f'<b>{esc}</b>')
            else:
                pieces.append(esc)
        return '(' + ', '.join(pieces) + ')' + GLib.markup_escape_text(rest)

    # -- Popover (lazy-created, re-parented to current view each update) -----

    def _sighelp_ensure_popover(self, view):
        if self._sighelp_popover is None:
            pop = Gtk.Popover()
            pop.set_modal(False)
            pop.set_position(Gtk.PositionType.BOTTOM)
            lbl = Gtk.Label()
            lbl.set_use_markup(True)
            lbl.set_selectable(False)
            lbl.set_margin_start(8)
            lbl.set_margin_end(8)
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            pop.add(lbl)
            lbl.show()
            self._sighelp_popover = pop
            self._sighelp_label = lbl
        if self._sighelp_popover.get_relative_to() is not view:
            self._sighelp_popover.set_relative_to(view)

    def _sighelp_position(self, view):
        buf = view.get_buffer()
        cursor_iter = buf.get_iter_at_mark(buf.get_insert())
        rect = view.get_iter_location(cursor_iter)
        win_x, win_y = view.buffer_to_window_coords(
            Gtk.TextWindowType.WIDGET, rect.x, rect.y + rect.height)
        pointing = Gdk.Rectangle()
        pointing.x = win_x
        pointing.y = win_y
        pointing.width = 1
        pointing.height = 1
        self._sighelp_popover.set_pointing_to(pointing)
