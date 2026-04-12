"""SynPad open tab tracker."""


class OpenTab:
    """Represents one open file tab."""

    def __init__(self, remote_path, local_path, source_view, buffer,
                 is_local=False, server_guid=''):
        self.remote_path = remote_path
        self.local_path = local_path
        self.source_view = source_view
        self.buffer = buffer
        self.modified = False
        self.is_local = is_local  # True for local files, False for remote
        self.server_guid = server_guid  # GUID of the server this file belongs to
        self.remote_mtime = None  # remote file mtime when opened/last saved
        self.remote_size = None   # remote file size when opened/last saved
        self.remote_hash = None   # SHA256 of remote content when opened/last saved
