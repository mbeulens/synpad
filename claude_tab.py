"""Claude integration — ask the local `claude -p` CLI about code from the
editor and stream the response into a tab in the Tools pane.

Forward-compat for v2 (conversation continuity): rendering is turn-block
based, conversation state is tracked from day one, and a single
`_claude_send` chokepoint owns the subprocess lifecycle. v2 adds an inline
follow-up entry that calls the same chokepoint with prior turns prepended."""

import datetime
import os
import shutil
import subprocess
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

# (key, label, prompt template). 'custom' has empty prompt — uses user input.
PRESETS = [
    ('find_bugs', 'Find bugs',
     'Review this code for bugs, edge cases, and quality issues. '
     'Be specific about line numbers and concrete problems.'),
    ('explain', 'Explain',
     'Explain what this code does, step by step, in plain language.'),
    ('refactor', 'Refactor',
     'Suggest refactoring improvements to make this code more readable and '
     'maintainable. Show before/after for key changes.'),
    ('add_types', 'Add types / docblocks',
     'Add type hints / docblocks where they would help readability. '
     'Return the updated code.'),
    ('tests', 'Generate tests',
     'Generate unit tests for this code, covering the main behavior and '
     'edge cases.'),
    ('custom', 'Custom prompt...', ''),
]
PRESET_PROMPTS = {key: prompt for key, _, prompt in PRESETS}
PRESET_LABELS = {key: lbl for key, lbl, _ in PRESETS}

LANG_BY_EXT = {
    'php': 'php', 'js': 'javascript', 'jsx': 'jsx',
    'ts': 'typescript', 'tsx': 'tsx',
    'py': 'python', 'html': 'html', 'css': 'css',
    'json': 'json', 'sh': 'bash', 'bash': 'bash',
    'yml': 'yaml', 'yaml': 'yaml', 'sql': 'sql', 'md': 'markdown',
    'xml': 'xml', 'rb': 'ruby', 'go': 'go', 'rs': 'rust', 'java': 'java',
    'c': 'c', 'h': 'c', 'cpp': 'cpp', 'hpp': 'cpp',
}


def _estimate_tokens(text):
    return max(1, len(text) // 4)


class ClaudeMixin:
    """Adds the Claude integration. Lazy-init."""

    def _claude_init(self):
        if getattr(self, '_claude_initialized', False):
            return
        self._claude_initialized = True
        self._claude_state = {
            'turns': [],            # v2 reads this for conversation context
            'session_id': None,     # v2 will use --resume
            'streaming': False,
            'process': None,
        }
        self._claude_view = None
        self._claude_buffer = None
        self._claude_stop_btn = None

    def _claude_attach_view(self, view, buffer_):
        """Wired from window.py once the Claude tab buffer + view exist."""
        self._claude_init()
        self._claude_view = view
        self._claude_buffer = buffer_

    def _claude_make_stop_button(self):
        """Stop button placed in the Tools header. Hidden until streaming."""
        self._claude_init()
        btn = Gtk.Button()
        btn.set_image(Gtk.Image.new_from_icon_name(
            'process-stop-symbolic', Gtk.IconSize.SMALL_TOOLBAR))
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text("Stop Claude")
        btn.set_no_show_all(True)
        btn.connect('clicked', lambda _: self._claude_cancel())
        self._claude_stop_btn = btn
        return btn

    # -- Trigger entry points ---------------------------------------------

    def _claude_handle_trigger(self, preset_key=None):
        """Dispatched from right-click presets, Ctrl+Shift+A, hamburger.
        If preset_key is a non-custom preset, send directly. Else show modal."""
        code, label = self._claude_get_code_for_question()
        if code is None:
            self._set_status("Open or select code to ask Claude about")
            return
        if preset_key and preset_key != 'custom':
            self._claude_send(code, PRESET_PROMPTS[preset_key], label, preset_key)
        else:
            self._claude_show_dialog(
                code, label, default_preset=preset_key or 'find_bugs')

    def _claude_get_code_for_question(self):
        """Return (code, source_label) from active editor tab, or (None, None)."""
        page_num = self.notebook.get_current_page()
        tab = self.tabs.get(page_num)
        if not tab:
            return None, None
        buf = tab.buffer
        path = tab.local_path or tab.remote_path or 'untitled'
        bounds = buf.get_selection_bounds()
        if bounds:
            start, end = bounds
            code = buf.get_text(start, end, False)
            line_a = start.get_line() + 1
            line_b = end.get_line() + 1
            label = f"{os.path.basename(path)}:{line_a}-{line_b}"
        else:
            code = buf.get_text(buf.get_start_iter(),
                                buf.get_end_iter(), False)
            label = os.path.basename(path)
        if not code.strip():
            return None, None
        return code, label

    # -- Modal -------------------------------------------------------------

    def _claude_show_dialog(self, code, source_label, default_preset='find_bugs'):
        dlg = Gtk.Dialog(
            title="Ask Claude",
            transient_for=self,
            modal=True,
            use_header_bar=False,
        )
        dlg.set_default_size(680, 520)

        box = dlg.get_content_area()
        box.set_spacing(8)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)

        n_chars = len(code)
        n_tokens = _estimate_tokens(code)
        hdr = Gtk.Label()
        hdr.set_markup(
            f"<b>{GLib.markup_escape_text(source_label)}</b>  •  "
            f"{n_chars} chars  •  ~{n_tokens} tokens")
        hdr.set_halign(Gtk.Align.START)
        box.pack_start(hdr, False, False, 0)

        # Code preview
        preview_scroll = Gtk.ScrolledWindow()
        preview_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                  Gtk.PolicyType.AUTOMATIC)
        preview_scroll.set_size_request(-1, 220)
        preview_buf = Gtk.TextBuffer()
        preview_buf.set_text(code)
        preview_view = Gtk.TextView(buffer=preview_buf)
        preview_view.set_editable(False)
        preview_view.set_monospace(True)
        preview_view.set_wrap_mode(Gtk.WrapMode.NONE)
        preview_scroll.add(preview_view)
        box.pack_start(preview_scroll, True, True, 0)

        # Action dropdown
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_row.pack_start(Gtk.Label(label="Action:"), False, False, 0)
        action_combo = Gtk.ComboBoxText()
        for key, lbl, _ in PRESETS:
            action_combo.append(key, lbl)
        action_combo.set_active_id(default_preset)
        action_row.pack_start(action_combo, True, True, 0)
        box.pack_start(action_row, False, False, 0)

        # Custom prompt
        prompt_lbl = Gtk.Label(
            label="Additional prompt (optional, appended to the action):")
        prompt_lbl.set_halign(Gtk.Align.START)
        box.pack_start(prompt_lbl, False, False, 0)

        prompt_scroll = Gtk.ScrolledWindow()
        prompt_scroll.set_policy(Gtk.PolicyType.AUTOMATIC,
                                 Gtk.PolicyType.AUTOMATIC)
        prompt_scroll.set_size_request(-1, 80)
        prompt_buf = Gtk.TextBuffer()
        prompt_view = Gtk.TextView(buffer=prompt_buf)
        prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        prompt_scroll.add(prompt_view)
        box.pack_start(prompt_scroll, False, False, 0)

        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        send_btn = dlg.add_button("Send", Gtk.ResponseType.OK)
        send_btn.get_style_context().add_class('suggested-action')
        dlg.set_default_response(Gtk.ResponseType.OK)

        dlg.show_all()
        resp = dlg.run()
        final_prompt = None
        preset_key = action_combo.get_active_id()
        if resp == Gtk.ResponseType.OK:
            preset_text = PRESET_PROMPTS.get(preset_key, '')
            extra = prompt_buf.get_text(
                prompt_buf.get_start_iter(),
                prompt_buf.get_end_iter(), False).strip()
            if preset_key == 'custom':
                final_prompt = extra or 'Review this code.'
            elif extra:
                final_prompt = preset_text + '\n\n' + extra
            else:
                final_prompt = preset_text
        dlg.destroy()
        if final_prompt is not None:
            self._claude_send(code, final_prompt, source_label, preset_key)

    # -- Send + stream -----------------------------------------------------

    def _claude_send(self, code, prompt_text, source_label, preset_key='custom'):
        if self._claude_state['streaming']:
            self._set_status("Claude is already streaming — stop it first")
            return

        cmd_name = self.config.get('claude_command', 'claude')
        if shutil.which(cmd_name) is None:
            self._claude_clear_buffer()
            self._claude_append("claude CLI not found — install Claude Code.\n",
                                'error')
            self._claude_show_pane()
            self._set_status("claude CLI not found")
            return

        ext = source_label.rsplit('.', 1)[-1].lower() if '.' in source_label else ''
        lang = LANG_BY_EXT.get(ext, '')
        full = (
            f"File: {source_label}\n\n"
            f"```{lang}\n{code}\n```\n\n"
            f"{prompt_text}"
        )

        # v1: clear buffer per turn. v2 will stop clearing and just append.
        self._claude_clear_buffer()
        self._claude_show_pane()

        ts = datetime.datetime.now().strftime('%H:%M')
        action_label = PRESET_LABELS.get(preset_key, 'Custom prompt')
        self._claude_append(
            f"── [{ts}] You — {action_label} on {source_label} ──\n",
            'claude_header_you')
        self._claude_append(prompt_text + "\n\n", 'claude_dim')
        self._claude_append("── Claude ──\n", 'claude_header_claude')

        self._claude_state['streaming'] = True
        self._claude_set_stop_btn_visible(True)
        self._set_status("Claude is thinking…")

        def work():
            try:
                proc = subprocess.Popen(
                    [cmd_name, '-p', full],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                self._claude_state['process'] = proc
                for line in proc.stdout:
                    GLib.idle_add(self._claude_append, line, None)
                proc.stdout.close()
                rc = proc.wait()
                stderr_text = proc.stderr.read()
                proc.stderr.close()
                GLib.idle_add(self._claude_finish, rc, stderr_text,
                              code, prompt_text, source_label)
            except FileNotFoundError:
                GLib.idle_add(self._claude_finish, -1,
                              "claude CLI not found",
                              code, prompt_text, source_label)
            except Exception as e:
                GLib.idle_add(self._claude_finish, -1,
                              f"{type(e).__name__}: {e}",
                              code, prompt_text, source_label)

        threading.Thread(target=work, daemon=True).start()

    def _claude_finish(self, returncode, stderr, code, prompt_text, source_label):
        self._claude_state['streaming'] = False
        self._claude_state['process'] = None
        if returncode == -15:
            self._claude_append("\n[cancelled]\n", 'error')
        elif returncode != 0:
            self._claude_append(
                f"\n[claude exited with status {returncode}]\n", 'error')
            if stderr:
                self._claude_append(stderr.strip() + '\n', 'error')
        # Record turn (v2 will read this for conversation context)
        buf = self._claude_buffer
        full_response = ''
        if buf is not None:
            full_response = buf.get_text(
                buf.get_start_iter(), buf.get_end_iter(), False)
        self._claude_state['turns'].append({
            'user_prompt': prompt_text,
            'user_code': code,
            'source_label': source_label,
            'response': full_response,
            'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        })
        self._claude_set_stop_btn_visible(False)
        self._set_status(
            "Claude finished" if returncode == 0 else "Claude stopped")
        return False

    def _claude_cancel(self):
        proc = self._claude_state.get('process')
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # -- Buffer helpers ----------------------------------------------------

    def _claude_clear_buffer(self):
        if self._claude_buffer is not None:
            self._claude_buffer.set_text('')

    def _claude_append(self, text, tag=None):
        buf = self._claude_buffer
        if buf is None:
            return False
        end = buf.get_end_iter()
        if tag:
            buf.insert_with_tags_by_name(end, text, tag)
        else:
            buf.insert(end, text)
        if self._claude_view is not None:
            self._claude_view.scroll_mark_onscreen(buf.get_insert())
        return False

    # -- Pane integration --------------------------------------------------

    def _claude_show_pane(self):
        if not getattr(self, '_console_visible', True):
            self._on_toggle_console()
        if self._claude_view is None:
            return
        page = self._console_notebook.page_num(self._claude_view.get_parent())
        if page >= 0:
            self._console_notebook.set_current_page(page)

    def _claude_set_stop_btn_visible(self, visible):
        btn = self._claude_stop_btn
        if btn is None:
            return
        if visible:
            btn.show()
        else:
            btn.hide()
