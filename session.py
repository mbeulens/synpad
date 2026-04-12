"""SynPad session save/restore mixin."""

import json
import os

from config import CONFIG_DIR, SESSION_FILE


class SessionMixin:
    """Mixin for SynPadWindow — session persistence."""

    def _save_session(self):
        """Save all open tabs to session file so they can be restored."""
        session_tabs = []
        for page_num in range(self.notebook.get_n_pages()):
            tab = self.tabs.get(page_num)
            if not tab:
                continue
            start = tab.buffer.get_start_iter()
            end = tab.buffer.get_end_iter()
            content = tab.buffer.get_text(start, end, True)

            session_tabs.append({
                'remote_path': tab.remote_path,
                'local_path': tab.local_path,
                'is_local': tab.is_local,
                'modified': tab.modified,
                'content': content,
                'server_guid': tab.server_guid,
                'remote_hash': tab.remote_hash,
                'remote_mtime': tab.remote_mtime,
            })

        session = {
            'tabs': session_tabs,
            'active_tab': self.notebook.get_current_page(),
        }

        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(SESSION_FILE, 'w') as f:
                json.dump(session, f, indent=2)
        except Exception:
            pass

    def _restore_session(self):
        """Restore tabs from the previous session."""
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, 'r') as f:
                session = json.load(f)
        except Exception:
            return

        tabs = session.get('tabs', [])
        if not tabs:
            return

        for tab_data in tabs:
            remote_path = tab_data.get('remote_path', '')
            local_path = tab_data.get('local_path', '')
            is_local = tab_data.get('is_local', False)
            content = tab_data.get('content', '')
            was_modified = tab_data.get('modified', False)
            server_guid = tab_data.get('server_guid', '')

            if not remote_path:
                continue

            # For local files, re-read from disk if not modified
            if is_local and not was_modified and os.path.isfile(local_path):
                try:
                    with open(local_path, 'r', errors='replace') as f:
                        content = f.read()
                except Exception:
                    pass

            # For remote files, save content to temp file
            if not is_local:
                filename = os.path.basename(remote_path)
                local_path = os.path.join(
                    self.tmp_dir,
                    filename.replace('/', '_') + f'_{id(remote_path)}'
                )
                try:
                    with open(local_path, 'w') as f:
                        f.write(content)
                except Exception:
                    continue

            self._create_editor_tab(remote_path, local_path, content,
                                    is_local=is_local, server_guid=server_guid)

            # Restore remote stats and mark as modified if needed
            page_num = self.notebook.get_n_pages() - 1
            tab = self.tabs.get(page_num)
            if tab:
                tab.remote_hash = tab_data.get('remote_hash')
                tab.remote_mtime = tab_data.get('remote_mtime')
                if was_modified:
                    tab.buffer.set_modified(True)

        # Restore active tab
        active = session.get('active_tab', 0)
        if active < self.notebook.get_n_pages():
            self.notebook.set_current_page(active)

        self.item_save.set_sensitive(bool(self.tabs))
