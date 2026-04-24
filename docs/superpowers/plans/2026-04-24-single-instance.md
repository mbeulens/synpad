# Single-Instance Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SynPad a single-instance GTK application — subsequent launches (from desktop, "Open with", or terminal) forward files to the running instance instead of starting a new process.

**Architecture:** Wrap the existing `SynPadWindow` in a `Gtk.Application` with `Gio.ApplicationFlags.HANDLES_OPEN`. GTK handles DBus-based instance deduplication automatically. `do_activate` focuses the existing window on bare re-launch; `do_open` forwards file paths to `open_or_focus_file`, which either switches to the existing tab or opens a new one. If the matched tab has unsaved changes, the user is prompted to reload from disk.

**Tech Stack:** Python 3, GTK3 (`gi.repository.Gtk`, `Gio`, `GLib`), PyGObject. No test framework — verification is manual per task.

**Spec:** `docs/superpowers/specs/2026-04-24-single-instance-design.md`

## File Structure

- **Modify** `synpad.py` — replace `Gtk.main()` flow with `SynPadApplication(Gtk.Application)`.
- **Modify** `window.py` — change `SynPadWindow` base from `Gtk.Window` → `Gtk.ApplicationWindow`, accept `application` arg, add `open_or_focus_file(filepath)`, add `_reload_tab_from_disk(tab)`.
- **Modify** `~/.local/share/applications/synpad.desktop` — `%f` → `%F` (multi-file support).

## Working Notes for the Engineer

- Work on the `dev` branch throughout. Commit after each task.
- The version rule (`feedback_versioning.md`) says: every change to `synpad.py` bumps the patch in `APP_VERSION` (in `config.py`). This feature touches `synpad.py`, so Task 1 bumps to `v1.16.1`. Subsequent tasks in this same plan stay at `v1.16.1` — they're one feature, one version bump at the end if the user decides to release.
- Git config: `user=mbeulens`, `email=m.beulens@syntec-it.nl`. All commits use co-author trailer `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- `SynPadWindow` is a `Gtk.Window` with mixin classes — see `window.py:23`. Existing `_open_local_file` lives in `editor.py:306`. Existing tabs are tracked in `self.tabs: dict[int, OpenTab]` where `OpenTab.local_path` is the filesystem path and `OpenTab.modified: bool` is the dirty flag (`tab.py:4`).
- There is no existing test suite. Verification is manual — run Synpad, exercise the feature, inspect behavior.

---

### Task 1: Introduce `Gtk.Application` skeleton (no behavior change yet)

Wrap current startup in a `Gtk.Application` subclass while keeping behavior identical to today. `SynPadWindow` still subclasses `Gtk.Window` at this point; we only change the process entry flow.

**Files:**
- Modify: `synpad.py` (entire file)
- Modify: `config.py:3` (bump `APP_VERSION`)

- [ ] **Step 1: Bump APP_VERSION to 1.16.1**

Edit `config.py` line 3:

```python
APP_VERSION = "1.16.1"
```

- [ ] **Step 2: Rewrite `synpad.py` to use `Gtk.Application`**

Replace the entire contents of `synpad.py` with:

```python
#!/usr/bin/env python3
"""SynPad - A lightweight PHP IDE with FTP/SFTP integration for Linux."""

import sys
import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '3.0')
from gi.repository import Gtk, Gdk, Gio, GLib

from config import APP_VERSION


class SynPadApplication(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="com.mbeulens.synpad",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)

        # Suppress all GTK/GLib warning and critical messages from stderr
        import ctypes
        try:
            libc = ctypes.CDLL("libglib-2.0.so.0")
            libc.g_log_set_always_fatal(0)
            LOG_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_char_p, ctypes.POINTER(ctypes.c_int))
            _noop_handler = LOG_FUNC(lambda *a: None)
            # Keep reference alive on the app instance
            self._log_handler = _noop_handler
            libc.g_log_set_handler(b"Gtk", 0xFF, _noop_handler, None)
            libc.g_log_set_handler(b"GtkSourceView", 0xFF, _noop_handler, None)
        except Exception:
            pass

        import warnings
        warnings.filterwarnings('ignore')

        GLib.set_prgname("synpad")
        GLib.set_application_name("SynPad")
        Gdk.set_program_class("synpad")

    def do_activate(self):
        from window import SynPadWindow
        if self.window is None:
            self.window = SynPadWindow()
            self.window.show_all()
        else:
            self.window.present_with_time(Gtk.get_current_event_time())

        # Preserve today's behavior: open file from argv[1] on first activation
        if len(sys.argv) > 1:
            filepath = os.path.abspath(sys.argv[1])
            if os.path.isfile(filepath):
                GLib.idle_add(self.window._open_local_file, filepath)


def main():
    app = SynPadApplication()
    sys.exit(app.run(sys.argv))


if __name__ == '__main__':
    main()
```

Note: `HANDLES_OPEN` is set but `do_open` is not implemented yet — Task 4 adds it. For now, argv handling in `do_activate` preserves current behavior. This is intentional: each task leaves the app working.

- [ ] **Step 3: Manual verification — app still launches**

Run:

```bash
cd /home/beuner/Development/Local/Synpad/repo
python3 synpad.py
```

Expected: SynPad window opens, session restores, title bar shows `SynPad v1.16.1`. Close the window; the process should exit cleanly.

Then:

```bash
python3 synpad.py /tmp/test.txt   # (create /tmp/test.txt first with any content)
```

Expected: SynPad opens with `/tmp/test.txt` as an open tab.

- [ ] **Step 4: Commit**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add synpad.py config.py
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Wrap startup in Gtk.Application (scaffold for single-instance mode)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

---

### Task 2: Make `SynPadWindow` a `Gtk.ApplicationWindow`

Change the window's base class so it can join the application's lifecycle. The app now controls the main loop; the window belongs to the app.

**Files:**
- Modify: `window.py:23-28` (class declaration + `__init__`)
- Modify: `synpad.py` (pass `application=self` to constructor, remove `show_all` redundancy if needed)

- [ ] **Step 1: Update `SynPadWindow` base class and `__init__`**

Edit `window.py`:

Change line 23-24 from:

```python
class SynPadWindow(Gtk.Window, EditorMixin, RemoteMixin, LocalFilesMixin,
                   CompareMixin, DialogsMixin, SessionMixin):
```

to:

```python
class SynPadWindow(Gtk.ApplicationWindow, EditorMixin, RemoteMixin, LocalFilesMixin,
                   CompareMixin, DialogsMixin, SessionMixin):
```

Change line 27-28 from:

```python
    def __init__(self):
        super().__init__(title="SynPad - PHP IDE")
```

to:

```python
    def __init__(self, application=None):
        super().__init__(application=application, title="SynPad - PHP IDE")
```

- [ ] **Step 2: Pass app instance into the window in `synpad.py`**

In `synpad.py` `do_activate`, change:

```python
            self.window = SynPadWindow()
```

to:

```python
            self.window = SynPadWindow(application=self)
```

- [ ] **Step 3: Manual verification — app still works identically**

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py
```

Expected: window opens, looks identical, session restores. Close the window; process exits.

Known pitfall: `Gtk.ApplicationWindow` reserves some menu accelerators for its internal action system. If you see `Gtk-CRITICAL` messages about duplicate accelerators, they will be suppressed by the log handler already installed in `do_startup` — they can be safely ignored at runtime. If the app won't start at all, double-check that you didn't accidentally leave a stray `Gtk.main()` call anywhere.

- [ ] **Step 4: Commit**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add window.py synpad.py
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Make SynPadWindow a Gtk.ApplicationWindow

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

---

### Task 3: Implement window focus on bare re-launch

With `Gtk.Application` registered, a second launch without args already forwards to `do_activate` on the primary. This task verifies that behavior end-to-end and removes the legacy `sys.argv` handling from `do_activate` (Task 4 moves file-opening into `do_open`).

**Files:**
- Modify: `synpad.py` — trim argv handling out of `do_activate` (kept only because `HANDLES_OPEN` is not yet fully wired).

- [ ] **Step 1: Remove argv fallback from `do_activate`**

In `synpad.py`, change `do_activate` to:

```python
    def do_activate(self):
        from window import SynPadWindow
        if self.window is None:
            self.window = SynPadWindow(application=self)
            self.window.show_all()
        else:
            self.window.present_with_time(Gtk.get_current_event_time())
```

Rationale: with `HANDLES_OPEN`, GTK routes argv containing files to `do_open` automatically. We no longer need to inspect `sys.argv` here.

- [ ] **Step 2: Manual verification — second launch raises the existing window**

Terminal A:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py
```

Let it open. Minimize it or click away from it.

Terminal B:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py
```

Expected:
- Terminal B's process exits almost immediately (no persistent process).
- The existing SynPad window is raised and focused.
- `ps aux | grep synpad | grep -v grep` shows exactly one `python3 .../synpad.py` line.

- [ ] **Step 3: Commit**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add synpad.py
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Focus existing window on bare re-launch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

---

### Task 4: Implement `do_open` and `open_or_focus_file`

Hook file-path arguments through `do_open` in the application, and add `open_or_focus_file` on the window. This task covers the clean-tab case (switch to tab) and the no-match case (open new tab). Dirty-tab handling with the reload prompt is Task 5.

**Files:**
- Modify: `window.py` — add `open_or_focus_file(filepath)` method on `SynPadWindow`.
- Modify: `synpad.py` — implement `do_open`.

- [ ] **Step 1: Add `open_or_focus_file` to `SynPadWindow`**

Add this method to `SynPadWindow` in `window.py` (place it near `_restore_session`, e.g. right after `__init__`):

```python
    def open_or_focus_file(self, filepath):
        """Open filepath as a tab. If already open, switch to that tab.
        If already open and dirty, prompt the user (see Task 5)."""
        import os
        target = os.path.realpath(os.path.abspath(filepath))

        for page_num, tab in self.tabs.items():
            if not tab.is_local or not tab.local_path:
                continue
            existing = os.path.realpath(os.path.abspath(tab.local_path))
            if existing == target:
                self.notebook.set_current_page(page_num)
                return

        # Not open yet — delegate to the existing opener
        self._open_local_file(target)
```

Note: `_open_local_file` (in `editor.py:306`) has its own duplicate check, but it uses unnormalized string comparison. By doing the normalized check first here, we guarantee correct duplicate detection when the path comes in with symlinks or relative segments.

- [ ] **Step 2: Add `do_open` to `SynPadApplication`**

Add this method to `SynPadApplication` in `synpad.py` (right after `do_activate`):

```python
    def do_open(self, files, n_files, hint):
        from window import SynPadWindow
        if self.window is None:
            self.window = SynPadWindow(application=self)
            self.window.show_all()
        else:
            self.window.present_with_time(Gtk.get_current_event_time())

        for gio_file in files:
            path = gio_file.get_path()
            if path and os.path.isfile(path):
                GLib.idle_add(self.window.open_or_focus_file, path)
```

- [ ] **Step 3: Manual verification — warm launch opens new tab, repeat focuses existing tab**

Setup:

```bash
echo "hello" > /tmp/synpad-a.txt
echo "world" > /tmp/synpad-b.txt
```

Launch primary:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py
```

From another terminal:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py /tmp/synpad-a.txt
```

Expected: existing window is focused, `synpad-a.txt` opens as a new tab.

Repeat the same command:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py /tmp/synpad-a.txt
```

Expected: existing window focused, `synpad-a.txt` tab is already open → that tab becomes the current one. **No duplicate tab is created.**

Try with a symlink:

```bash
ln -sf /tmp/synpad-a.txt /tmp/synpad-a-link.txt
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py /tmp/synpad-a-link.txt
```

Expected: still no duplicate — the same tab is focused (realpath normalization handles the symlink).

Try with multiple files on one command:

```bash
python3 /home/beuner/Development/Local/Synpad/repo/synpad.py /tmp/synpad-a.txt /tmp/synpad-b.txt
```

Expected: both files are tabs, focused tab ends on `synpad-b.txt` (last in list).

- [ ] **Step 4: Commit**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add synpad.py window.py
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Route file opens through do_open, dedupe by realpath

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

---

### Task 5: Prompt to reload when opening a dirty tab

If the file being opened is already open as a tab **and** that tab has unsaved changes, switch to the tab then show a modal dialog with `Reload (discard my changes)` / `Cancel`.

**Files:**
- Modify: `window.py` — extend `open_or_focus_file` to handle dirty case; add `_reload_tab_from_disk(tab)`.

- [ ] **Step 1: Extend `open_or_focus_file` to handle dirty tabs**

Replace the method you added in Task 4 with this version:

```python
    def open_or_focus_file(self, filepath):
        """Open filepath as a tab. If already open:
        - clean tab: switch to it.
        - dirty tab: switch to it, then prompt to reload from disk."""
        import os
        target = os.path.realpath(os.path.abspath(filepath))

        for page_num, tab in self.tabs.items():
            if not tab.is_local or not tab.local_path:
                continue
            existing = os.path.realpath(os.path.abspath(tab.local_path))
            if existing == target:
                self.notebook.set_current_page(page_num)
                if tab.modified:
                    self._prompt_reload_dirty_tab(tab, target)
                return

        # Not open yet — delegate to the existing opener
        self._open_local_file(target)
```

- [ ] **Step 2: Add `_prompt_reload_dirty_tab` and `_reload_tab_from_disk`**

Add these two methods right after `open_or_focus_file` in `window.py`:

```python
    def _prompt_reload_dirty_tab(self, tab, filepath):
        """Ask whether to reload from disk, discarding the buffer's unsaved edits."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="File has unsaved changes",
        )
        dialog.format_secondary_text(
            f"{filepath}\n\nReload from disk and discard your unsaved changes?"
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Reload (discard my changes)", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)

        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.ACCEPT:
            self._reload_tab_from_disk(tab)

    def _reload_tab_from_disk(self, tab):
        """Replace the tab's buffer contents with the on-disk file, clearing dirty flag."""
        try:
            with open(tab.local_path, 'r', errors='replace') as f:
                content = f.read()
        except Exception as e:
            self._show_error("Reload Failed", str(e))
            return

        # Replace buffer content without re-triggering modified handling
        buf = tab.buffer
        buf.handler_block_by_func(self._on_buffer_changed) if hasattr(self, '_on_buffer_changed') else None
        try:
            buf.set_text(content)
        finally:
            if hasattr(self, '_on_buffer_changed'):
                buf.handler_unblock_by_func(self._on_buffer_changed)

        tab.modified = False
        # Refresh the tab label (strips the `* ` bold marker)
        page_widget = tab.source_view.get_parent()
        page_num = self.notebook.page_num(page_widget)
        name = os.path.basename(tab.local_path)
        self._update_tab_label(tab, name)
```

Note on `handler_block_by_func`: if `_on_buffer_changed` does not exist as a bound method on the window (check via `hasattr`), the block is skipped. If editor.py uses a different handler name, replace the reference. To find the correct handler, run:

```bash
grep -n "buf.connect\|buffer.connect" /home/beuner/Development/Local/Synpad/repo/editor.py
```

Use whatever handler name the `'changed'` signal is connected to. If the handler cannot be located, the fallback is to accept a spurious `modified=True` after reload and immediately overwrite it with `tab.modified = False` and a label refresh — which is what the code above does.

- [ ] **Step 3: Manual verification — reload prompt**

With SynPad running:

1. Open `/tmp/synpad-a.txt` (new tab).
2. Edit the tab to add some text — tab label should show `* synpad-a.txt` indicating dirty state.
3. From a terminal: `python3 /home/beuner/Development/Local/Synpad/repo/synpad.py /tmp/synpad-a.txt`
4. Expected: existing tab gets focus **and** a dialog appears: "File has unsaved changes — reload from disk and discard your unsaved changes?" with `Cancel` / `Reload (discard my changes)` buttons.
5. Click `Cancel` → dialog closes, buffer still shows your edits, label still shows `*`.
6. Repeat steps 3-4. This time click `Reload (discard my changes)` → buffer reverts to original disk content, label no longer has `*`, tab is clean.

Also verify clean-tab case is unaffected:

7. Open a fresh file with no edits. Re-open it from another terminal.
8. Expected: **no dialog** appears, tab is simply focused.

- [ ] **Step 4: Commit**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add window.py
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Prompt to reload when re-opening a dirty tab

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

---

### Task 6: Update desktop launcher for multi-file support

Change the `Exec` line from `%f` (single file) to `%F` (list of files) so file managers can pass multiple selections.

**Files:**
- Modify: `~/.local/share/applications/synpad.desktop`

- [ ] **Step 1: Edit the desktop file**

Change the `Exec` line from:

```
Exec=python3 /home/beuner/Development/Local/Synpad/repo/synpad.py %f
```

to:

```
Exec=python3 /home/beuner/Development/Local/Synpad/repo/synpad.py %F
```

- [ ] **Step 2: Manual verification — file manager selections**

1. In a file manager (Nautilus, Nemo, etc.), select **two or more** text files.
2. Right-click → "Open with SynPad" (or drag onto the launcher).
3. Expected: all selected files open as tabs in the running SynPad instance (or a new one if none was running). No duplicate processes.
4. `ps aux | grep synpad | grep -v grep` should still show exactly one process.

Also verify the single-file case still works:

5. Right-click a single file → "Open with SynPad". It should open as a tab in the existing instance.

- [ ] **Step 3: Commit**

The desktop launcher file lives outside the repo, so there's nothing to commit. However, if you have a copy of it inside the repo (e.g. `install.sh` references the path or a template), update that template too. Check:

```bash
grep -rn "%f" /home/beuner/Development/Local/Synpad/repo/install.sh 2>/dev/null || echo "no install.sh reference to %f"
```

If `install.sh` writes the desktop file, edit the `%f` → `%F` in `install.sh` and commit:

```bash
git -C /home/beuner/Development/Local/Synpad/repo add install.sh
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Desktop launcher uses %F for multi-file selection

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

If no install script references the desktop file, no commit is needed for this task — the change only lives in `~/.local/share/applications/synpad.desktop`.

---

### Task 7: Full-flow manual verification pass

End-to-end walkthrough covering every requirement from the spec.

**Files:** none (verification only).

- [ ] **Step 1: Clean slate**

```bash
pkill -f "synpad.py" ; sleep 1
ps aux | grep synpad | grep -v grep   # should be empty
```

- [ ] **Step 2: Run the spec's test plan in order**

For each item below, confirm the observed behavior matches. Any miss = reopen the task that covers it.

1. Launch from desktop (or `python3 synpad.py`) → fresh window, session restored. ✓
2. While running, re-launch with no file → same window raises to front, no new process. ✓
3. While running, "Open with" a file not currently open → new tab opens in existing window, window focused. ✓
4. File already open and unmodified → existing tab is focused, no duplicate. ✓
5. File already open and dirty → prompt appears; `Reload` replaces buffer and clears dirty; `Cancel` keeps buffer. Either way the tab is focused. ✓
6. Select 3 files in file manager, "Open with Synpad" → all 3 opened/focused as tabs. ✓
7. From terminal `python3 synpad.py foo.txt` while running → opens in existing instance. ✓
8. `ps aux | grep synpad | grep -v grep` after all of the above → only one python process. ✓

- [ ] **Step 3: Update project memory**

Update `project_synpad.md` in `.memory/` to note the new version and feature. Add a line under the features list:

> Single-instance mode — second launches forward files to the running window (Gtk.Application + HANDLES_OPEN)

And bump `**Current version:** v1.16.0` to `v1.16.1`.

Per the memory-sync rule, copy the updated file to `~/.claude/projects/-home-beuner-Development-Local-Synpad/memory/project_synpad.md` as well, and to `.remember/`.

- [ ] **Step 4: Commit memory update**

```bash
git -C /home/beuner/Development/Local/Synpad/repo add .memory/project_synpad.md
git -C /home/beuner/Development/Local/Synpad/repo commit -m "$(cat <<'EOF'
v1.16.1: Update project memory for single-instance feature

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git -C /home/beuner/Development/Local/Synpad/repo push origin dev
```

- [ ] **Step 5: Hand off to the user**

Report which tasks succeeded, note any deviations from the plan, and ask whether they want a release (minor/major merge to `main` + tag). Do **not** merge to `main` autonomously — per `feedback_versioning.md`, releases are user-initiated.

---

## Self-Review Notes

Reviewed against `docs/superpowers/specs/2026-04-24-single-instance-design.md`:

- ✅ Architecture section → Tasks 1, 2, 3
- ✅ `synpad.py` component changes → Tasks 1, 3, 4
- ✅ `window.py` component changes → Tasks 2, 4, 5
- ✅ `open_or_focus_file` with normalize → clean match → delegate → Task 4, dirty prompt → Task 5
- ✅ Desktop launcher `%f` → `%F` → Task 6
- ✅ Data flow (cold/warm/no-file/multi-file) → exercised in Tasks 3, 4, 6, 7
- ✅ Error handling (DBus unavailable, missing files, stale registration, session restore) → preserved automatically by `Gtk.Application` semantics
- ✅ Manual test plan (8 items) → Task 7 Step 2 walks all 8

No placeholders. Type names and method names match across tasks: `open_or_focus_file`, `_reload_tab_from_disk`, `_prompt_reload_dirty_tab`, `_open_local_file` (existing), `OpenTab.local_path`, `OpenTab.modified`.
