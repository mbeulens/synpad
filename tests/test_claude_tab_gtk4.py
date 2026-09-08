"""claude_tab.py under GTK4 — headless.

Guards the "Ask Claude" modal's .run()/.destroy() -> Gtk.Window + explicit
button-click callbacks migration (decision logic that builds
`final_prompt` must be identical), the stop-button's
set_no_show_all()/show()/hide() -> set_visible() migration, and the
Gtk.Image icon-size-argument removal.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib

if not Gtk.init_check():
    print("SKIP: no display"); sys.exit(0)

import claude_tab
from claude_tab import ClaudeMixin, PRESET_PROMPTS

fails = []
def check(n, c, extra=""):
    print(f"{'PASS' if c else 'FAIL'}: {n}" + (f" — {extra}" if not c and extra else ""))
    if not c: fails.append(n)


class FakeTab:
    def __init__(self, buf, local_path=None, remote_path=None):
        self.buffer = buf
        self.local_path = local_path
        self.remote_path = remote_path


class Host(ClaudeMixin, Gtk.Window):
    """A real Gtk.Window subclass — the Ask Claude dialog uses
    `transient_for=self`, so the host must be an actual window."""
    def __init__(self):
        Gtk.Window.__init__(self)
        self._claude_init()
        self.config = {}
        self.status = []
        self.tabs = {}
        self.notebook = Gtk.Notebook()
        self._console_notebook = Gtk.Notebook()
        self._console_visible = True
        self.toggles = 0
        self.dialogs_shown = []
        self.sends = []

    def _set_status(self, m): self.status.append(m)
    def _on_toggle_console(self):
        self.toggles += 1
        self._console_visible = not self._console_visible


h = Host()
h.present()

claude_buf = Gtk.TextBuffer()
for tag in ('error', 'claude_header_you', 'claude_dim', 'claude_header_claude'):
    claude_buf.create_tag(tag)
claude_view = Gtk.TextView(buffer=claude_buf)
claude_scroll = Gtk.ScrolledWindow()
claude_scroll.set_child(claude_view)
h._console_notebook.append_page(claude_scroll, Gtk.Label(label="Claude"))
h._claude_attach_view(claude_view, claude_buf)

# --- stop button: icon-only, starts hidden, no Gtk.IconSize argument ------
stop_btn = h._claude_make_stop_button()
check("stop button uses process-stop-symbolic icon",
      stop_btn.get_icon_name() == 'process-stop-symbolic', stop_btn.get_icon_name())
check("stop button is flat (was set_relief(NONE))", stop_btn.has_css_class('flat'))
check("stop button starts hidden (was set_no_show_all(True))",
      stop_btn.get_visible() is False)

h._claude_set_stop_btn_visible(True)
check("_claude_set_stop_btn_visible(True) shows it (was .show())",
      stop_btn.get_visible() is True)
h._claude_set_stop_btn_visible(False)
check("_claude_set_stop_btn_visible(False) hides it (was .hide())",
      stop_btn.get_visible() is False)

# --- code-for-question extraction (pure logic, guards the port) ----------
buf1 = Gtk.TextBuffer()
buf1.set_text("line one\nline two\nline three\n")
h.tabs[0] = FakeTab(buf1, local_path='/tmp/foo.py')
h.notebook.append_page(Gtk.Label(), Gtk.Label())  # page 0 exists

code, label = h._claude_get_code_for_question()
check("whole-buffer extraction when no selection",
      code == "line one\nline two\nline three\n", repr(code))
check("label uses basename with no selection", label == "foo.py", label)

start = buf1.get_start_iter()
end = buf1.get_start_iter()
end.forward_chars(4)
buf1.select_range(start, end)
code2, label2 = h._claude_get_code_for_question()
check("selection extraction returns just the selection", code2 == "line", repr(code2))
check("label includes line range for a selection", label2 == "foo.py:1-1", label2)

# --- handle_trigger: no code available --------------------------------
buf_empty = Gtk.TextBuffer()
h.tabs[0] = FakeTab(buf_empty, local_path='/tmp/empty.py')
h._claude_handle_trigger('find_bugs')
check("no code -> status set, nothing dispatched",
      h.status and h.status[-1] == "Open or select code to ask Claude about",
      h.status)

# --- handle_trigger: non-custom preset sends directly, no dialog ---------
h.status.clear()
h.tabs[0] = FakeTab(buf1, local_path='/tmp/foo.py')
buf1.select_range(buf1.get_start_iter(), buf1.get_start_iter())  # clear selection
sent = []
h._claude_send = lambda code, prompt, label, key: sent.append((code, prompt, label, key))
h._claude_handle_trigger('explain')
check("non-custom preset calls _claude_send directly",
      len(sent) == 1 and sent[0][1] == PRESET_PROMPTS['explain'], sent)
check("non-custom preset key passed through", sent[0][3] == 'explain', sent)

# --- handle_trigger: preset_key=None / 'custom' opens the modal ----------
def find_all(widget, cls):
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            found.append(child)
        found.extend(find_all(child, cls))
        child = child.get_next_sibling()
    return found

captured = []
class _CapturingWindow(Gtk.Window):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        captured.append(self)

_RealWindow = Gtk.Window
Gtk.Window = _CapturingWindow
try:
    h._claude_handle_trigger(None)
finally:
    Gtk.Window = _RealWindow

check("preset_key=None opens the Ask Claude window", len(captured) == 1, captured)
win = captured[0]
check("window title is 'Ask Claude'", win.get_title() == "Ask Claude", win.get_title())

# Recompute what the dialog was actually built from (selection was cleared
# above, so this is the whole-buffer path) — used by the assertions below.
code, label = h._claude_get_code_for_question()

combo = find_all(win, Gtk.ComboBoxText)[0]
check("dialog defaults to 'find_bugs' when no preset requested",
      combo.get_active_id() == 'find_bugs', combo.get_active_id())

buttons = find_all(win, Gtk.Button)
labeled_order = [b.get_label() for b in buttons if b.get_label()]
by_label = {b.get_label(): b for b in buttons if b.get_label()}
check("Cancel and Send buttons present", set(by_label) == {"Cancel", "Send"},
      list(by_label))
# Gtk.Dialog.add_button("Cancel") then add_button("Send") rendered as
# [Cancel, Send] left-to-right (add_button preserves call order, unlike
# pack_end) — find_all() walks children in append order, so this pins
# that layout against a future regression that silently swaps the two.
check("button visual order is [Cancel, Send] (matches old add_button order)",
      labeled_order == ["Cancel", "Send"], labeled_order)
check("Send is the default widget (was set_default_response(OK))",
      win.get_default_widget() is by_label["Send"])

textviews = find_all(win, Gtk.TextView)
check("code preview shows the selected code", textviews[0].get_buffer().get_text(
      textviews[0].get_buffer().get_start_iter(),
      textviews[0].get_buffer().get_end_iter(), False) == code, "mismatch")
check("code preview is read-only", textviews[0].get_editable() is False)

# --- Cancel does not send ---------------------------------------------
sent.clear()
by_label["Cancel"].emit('clicked')
check("Cancel sends nothing", sent == [], sent)

# --- Send with a preset + no extra prompt: uses the preset text verbatim -
def reopen(default_preset=None):
    captured.clear()
    global Gtk
    Gtk.Window = _CapturingWindow
    try:
        if default_preset is None:
            h._claude_handle_trigger(None)
        else:
            h._claude_show_dialog(code, label, default_preset=default_preset)
    finally:
        Gtk.Window = _RealWindow
    return captured[0]

win = reopen()
combo = find_all(win, Gtk.ComboBoxText)[0]
combo.set_active_id('explain')
buttons = find_all(win, Gtk.Button)
by_label = {b.get_label(): b for b in buttons if b.get_label()}
sent.clear()
by_label["Send"].emit('clicked')
check("Send with preset + empty extra uses the preset text verbatim",
      sent and sent[0][1] == PRESET_PROMPTS['explain'], sent)
check("Send passes the preset key through", sent[0][3] == 'explain', sent)

# --- Send with a preset + extra prompt: preset + blank line + extra -----
win = reopen()
combo = find_all(win, Gtk.ComboBoxText)[0]
combo.set_active_id('find_bugs')
prompt_view = find_all(win, Gtk.TextView)[1]
prompt_view.get_buffer().set_text("Focus on the loop on line 2.")
buttons = find_all(win, Gtk.Button)
by_label = {b.get_label(): b for b in buttons if b.get_label()}
sent.clear()
by_label["Send"].emit('clicked')
expected = PRESET_PROMPTS['find_bugs'] + '\n\n' + "Focus on the loop on line 2."
check("Send with preset + extra concatenates with a blank line",
      sent and sent[0][1] == expected, sent)

# --- Send with 'custom' + no extra: falls back to 'Review this code.' ---
win = reopen()
combo = find_all(win, Gtk.ComboBoxText)[0]
combo.set_active_id('custom')
buttons = find_all(win, Gtk.Button)
by_label = {b.get_label(): b for b in buttons if b.get_label()}
sent.clear()
by_label["Send"].emit('clicked')
check("custom preset with empty prompt falls back to default text",
      sent and sent[0][1] == 'Review this code.', sent)

# --- Send with 'custom' + prompt text: uses exactly that text -----------
win = reopen()
combo = find_all(win, Gtk.ComboBoxText)[0]
combo.set_active_id('custom')
prompt_view = find_all(win, Gtk.TextView)[1]
prompt_view.get_buffer().set_text("Is this thread-safe?")
buttons = find_all(win, Gtk.Button)
by_label = {b.get_label(): b for b in buttons if b.get_label()}
sent.clear()
by_label["Send"].emit('clicked')
check("custom preset with prompt text uses it verbatim",
      sent and sent[0][1] == "Is this thread-safe?", sent)

# --- Escape cancels the dialog (was Gtk.Dialog's built-in RESPONSE_DELETE_EVENT) --
win = reopen()
key_ctrls = [c for c in win.observe_controllers()
             if isinstance(c, Gtk.EventControllerKey)]
# GtkWindow installs its own EventControllerKey (for mnemonics/
# accelerators) in addition to ours — same "count/verify, don't assume
# which one is yours" gotcha as GtkSourceView's own focus controller.
check("Ask Claude dialog has at least one key controller (ours + GTK's own)",
      len(key_ctrls) >= 1, key_ctrls)

sent.clear()
# Only one of these is ours; emitting on all is harmless (unmatched ones
# just return False) and doesn't require guessing which is ours.
for kc in key_ctrls:
    kc.emit('key-pressed', Gdk.KEY_Escape, 0, 0)
check("Escape sends nothing", sent == [], sent)
check("Escape closes the window (was RESPONSE_DELETE_EVENT)",
      win.get_visible() is False, win.get_visible())

del h._claude_send  # restore the real method for the pipeline test below

# --- real send/stream/finish pipeline (subprocess + threading + idle) ---
class FakeStream:
    def __init__(self, lines=None, text=''):
        self._lines = lines or []
        self._text = text
    def __iter__(self): return iter(self._lines)
    def read(self): return self._text
    def close(self): pass

class FakeProc:
    def __init__(self, *a, **kw):
        self.stdout = FakeStream(lines=["Fake response line 1\n",
                                        "Fake response line 2\n"])
        self.stderr = FakeStream(text='')
    def wait(self): return 0
    def poll(self): return 0
    def terminate(self): pass

claude_tab.subprocess.Popen = FakeProc
claude_tab.shutil.which = lambda name: '/usr/bin/' + name

h._claude_send("print(1)", "Explain this", "foo.py", 'explain')
check("streaming flag set immediately", h._claude_state['streaming'] is True)
check("stop button shown while streaming", stop_btn.get_visible() is True)

ctx = GLib.MainContext.default()
t0 = time.time()
while time.time() - t0 < 5 and h._claude_state['streaming']:
    ctx.iteration(True)

full_text = claude_buf.get_text(claude_buf.get_start_iter(),
                                claude_buf.get_end_iter(), False)
check("streamed stdout lines land in the buffer",
      "Fake response line 1" in full_text and "Fake response line 2" in full_text,
      full_text)
check("status reports success", h.status[-1] == "Claude finished", h.status)
check("stop button hidden again after finish", stop_btn.get_visible() is False)
check("turn recorded for v2 conversation continuity",
      len(h._claude_state['turns']) == 1 and
      h._claude_state['turns'][0]['user_prompt'] == "Explain this",
      h._claude_state['turns'])

print()
if fails: print(f"{len(fails)} FAILED: {fails}"); sys.exit(1)
print("ALL PASS")
