"""SynPad configuration — paths, defaults, load/save helpers."""

APP_VERSION = "1.15.0"
DEBUG_MODE = False

import json
import os
import uuid
from pathlib import Path


CONFIG_DIR = os.path.join(str(Path.home()), '.config', 'synpad')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
SESSION_FILE = os.path.join(CONFIG_DIR, 'session.json')

DEFAULT_CONFIG = {
    'host': '',
    'port': 22,
    'username': '',
    'password': '',
    'max_upload_size_mb': 5,
    'last_directory': '/',
    'protocol': 'sftp',       # 'ftp' or 'sftp'
    'ssh_key_path': '',        # optional path to private key
    'home_directory': '',      # starting directory on connect (blank = /)
    'servers': [],             # saved server profiles
    'last_server': '',         # GUID of last used server
    'pane_order': ['symbols', 'editor', 'files'],  # left to right
    'dark_theme': True,        # editor color scheme
    'color_scheme': 'oblivion', # GtkSourceView style scheme id
    'custom_colors_dark': {},   # dark mode overrides: style_id -> {fg, bg, bold, italic}
    'custom_colors_light': {},  # light mode overrides
    'saved_color_schemes': {},  # name -> {base, colors_dark, colors_light}
    'active_custom_scheme': '', # name of currently active saved custom scheme
    'editor_extensions': [
        'php', 'js', 'jsx', 'ts', 'tsx', 'py', 'html', 'htm', 'css',
        'json', 'xml', 'sql', 'sh', 'bash', 'yml', 'yaml', 'md', 'txt',
        'ini', 'conf', 'env', 'htaccess', 'gitignore', 'log', 'csv',
        'tpl', 'twig', 'blade', 'vue', 'svelte', 'rb', 'java', 'c',
        'h', 'cpp', 'hpp', 'rs', 'go', 'swift', 'kt', 'r', 'lua',
        'pl', 'pm', 'toml', 'cfg', 'properties', 'less', 'scss', 'sass',
        'svg', 'dockerfile', 'makefile', 'cmake',
    ],
}


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        # Migrate: add GUIDs to any server profiles that don't have one
        migrated = False
        for srv in cfg.get('servers', []):
            if 'guid' not in srv:
                srv['guid'] = str(uuid.uuid4())
                migrated = True
        # Migrate: old custom_colors -> custom_colors_dark
        if 'custom_colors' in cfg and cfg['custom_colors']:
            cfg.setdefault('custom_colors_dark', cfg.pop('custom_colors'))
            migrated = True
        elif 'custom_colors' in cfg:
            del cfg['custom_colors']
        # Migrate: old saved_color_schemes with 'colors' -> 'colors_dark'
        for name, scheme in cfg.get('saved_color_schemes', {}).items():
            if 'colors' in scheme and 'colors_dark' not in scheme:
                scheme['colors_dark'] = scheme.pop('colors')
                scheme.setdefault('colors_light', {})
                migrated = True
        if migrated:
            save_config(cfg)
        return cfg
    return dict(DEFAULT_CONFIG)


def find_server_by_guid(cfg, guid):
    """Find a server profile by its GUID. Returns the dict or None."""
    for srv in cfg.get('servers', []):
        if srv.get('guid') == guid:
            return srv
    return None


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)
