# SynPad single-instance mode — design

## Problem

Launching SynPad from the desktop launcher, "Open with" from a file manager, or the terminal always starts a new process. Users get multiple SynPad windows instead of having the file open as a tab in the existing one.

## Goal

One SynPad instance at a time. Any subsequent launch — with or without a file — forwards to the running instance, opens the file(s) as tabs (or focuses an existing matching tab), and brings the window to the foreground.

## Approach

Use `Gtk.Application` with `Gio.ApplicationFlags.HANDLES_OPEN`. GTK manages single-instance behavior over DBus under the application ID `com.mbeulens.synpad`:

- **First launch** — registration succeeds, becomes the primary instance.
- **Subsequent launch** — arguments are forwarded to the primary over DBus, the secondary process exits immediately.

The primary receives `do_activate` (no files) or `do_open(files, n_files, hint)` (with files) and responds accordingly.

## Component changes

### `synpad.py`

Introduce `SynPadApplication(Gtk.Application)`:

- `application_id = "com.mbeulens.synpad"`
- `flags = Gio.ApplicationFlags.HANDLES_OPEN`
- `do_startup()` — one-time init: current GTK/GLib warning suppression, `GLib.set_prgname`, `GLib.set_application_name`, `Gdk.set_program_class`.
- `do_activate()` — if no window exists, create `SynPadWindow(self)`, show, restore session. If it exists, call `present_with_time()` to raise and focus it.
- `do_open(files, n_files, hint)` — ensure the window exists (same as activate), then for each `Gio.File` call `window.open_or_focus_file(file.get_path())`.

`main()` becomes:

```python
app = SynPadApplication()
sys.exit(app.run(sys.argv))
```

### `window.py`

`SynPadWindow`:

- Base class changes from `Gtk.Window` to `Gtk.ApplicationWindow`.
- `__init__` accepts an `application` argument and passes it to `super().__init__(application=application, title=...)`.

New method `open_or_focus_file(filepath)`:

1. Normalize `filepath` with `os.path.realpath(os.path.abspath(filepath))`.
2. Iterate open tabs, comparing each tab's backing path (normalized the same way) to the target.
3. **No match** → delegate to the existing `_open_local_file(filepath)`.
4. **Match, tab is clean** → `notebook.set_current_page(index)` on the match.
5. **Match, tab is dirty** → set it as current, then show a modal `Gtk.MessageDialog`:
   - Title/text: "File has unsaved changes — reload from disk?"
   - Buttons: `Reload (discard my changes)` / `Cancel`
   - On Reload: replace the buffer contents from disk, clear the dirty flag.
   - On Cancel: no-op (tab remains focused with its current buffer).

### Desktop launcher

`~/.local/share/applications/synpad.desktop`:

- `Exec=python3 /home/beuner/Development/Local/Synpad/repo/synpad.py %f` → `%F` to accept multiple files.

## Data flow

**Cold launch with file:**
```
synpad.py /path/to/file.txt
  → SynPadApplication.run()
  → DBus registration: becomes primary
  → do_startup()  [one-time init]
  → do_open([file], 1, "")
      → create SynPadWindow, restore session
      → open_or_focus_file("/path/to/file.txt")
  → Gtk main loop
```

**Warm launch with file:**
```
synpad.py /path/to/file.txt
  → DBus registration finds primary, forwards args, this process exits
  → (in primary) do_open fires
      → window.present_with_time()
      → open_or_focus_file("/path/to/file.txt")
          → not open yet → _open_local_file(...)  [new tab]
          → already open, clean → switch tab
          → already open, dirty → switch tab + Reload/Cancel prompt
```

**Warm launch, no file:**
```
→ primary receives do_activate()
  → window.present_with_time()
```

**Warm launch, multiple files (`%F`):**
```
→ do_open([f1, f2, f3], 3, "") in primary
  → present window, then open_or_focus_file for each in order
  → focused tab ends on the last file
```

## Error handling

- **DBus unavailable** (rare) — `app.run()` falls back to non-unique behavior. Same as today, acceptable.
- **File path missing or unreadable** — existing `_open_local_file` already handles this (checks `os.path.isfile`). Bad paths are silently skipped.
- **Stale DBus registration** — GTK releases the name automatically on process death; no manual cleanup needed.
- **Session restore in warm open** — `do_startup` runs once, so session restores only once. Subsequent `do_open` calls only open files on top, no conflict.

## Testing (manual)

No test framework in the repo. Manual test plan:

1. Kill all synpad instances. Launch from desktop → fresh window, session restored.
2. While running, click launcher again (no file) → same window raises to front, no new process.
3. While running, "Open with" a file not currently open → new tab in existing window, window focused.
4. File already open and unmodified → existing tab focused, no duplicate tab.
5. File already open and dirty → prompt appears; `Reload` replaces buffer, `Cancel` keeps buffer. Either way the tab is focused.
6. Select 3 files in file manager, "Open with Synpad" → all 3 opened/focused as tabs.
7. Launch from terminal `python3 synpad.py foo.txt` while running → opens in existing instance.
8. `ps aux | grep synpad` after all of the above → only one python process.

## Out of scope

- Multiple top-level windows within one app process (GTK supports it, not required here).
- Cross-user / cross-session single-instance (DBus is session-scoped, which is what we want).
- Custom IPC beyond DBus.
