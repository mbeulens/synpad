"""SynPad symbol parser — extracts functions, classes, etc. from source code."""

import re


# Patterns: (icon, regex) — regex must have named group 'name' and match at line start
SYMBOL_PATTERNS = {
    'php': [
        ('method',  re.compile(
            r'^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*'
            r'function\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*(?:abstract\s+)?class\s+(?P<name>\w+)', re.MULTILINE)),
        ('iface',   re.compile(
            r'^\s*interface\s+(?P<name>\w+)', re.MULTILINE)),
    ],
    'js': [
        ('func',    re.compile(
            r'^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('method',  re.compile(
            r'^\s*(?:(?:static|async|get|set)\s+)*(?P<name>(?!if|else|for|while|switch|catch|return|throw|typeof|instanceof|new|delete|void|do)\w+)\s*\([^)]*\)\s*\{', re.MULTILINE)),
        ('arrow',   re.compile(
            r'^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*=\s*(?:async\s+)?'
            r'(?:\([^)]*\)|[\w]+)\s*=>', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*(?:export\s+)?class\s+(?P<name>\w+)', re.MULTILINE)),
    ],
    'ts': None,  # will copy from js
    'py': [
        ('func',    re.compile(
            r'^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\(', re.MULTILINE)),
        ('class',   re.compile(
            r'^\s*class\s+(?P<name>\w+)', re.MULTILINE)),
    ],
}
SYMBOL_PATTERNS['ts'] = SYMBOL_PATTERNS['js']

SYMBOL_ICONS = {
    'func': 'media-playback-start-symbolic',
    'method': 'media-playback-start-symbolic',
    'arrow': 'go-next-symbolic',
    'class': 'dialog-information-symbolic',
    'iface': 'dialog-question-symbolic',
}

# Extensions that support symbol parsing
SYMBOL_EXTENSIONS = {'php', 'js', 'jsx', 'ts', 'tsx', 'py'}


def parse_symbols(content, ext):
    """Return list of (kind, name, line_number, char_offset) from source content."""
    lang = ext
    if ext in ('jsx',):
        lang = 'js'
    elif ext in ('tsx',):
        lang = 'ts'
    patterns = SYMBOL_PATTERNS.get(lang)
    if not patterns:
        return []

    symbols = []
    seen = set()  # (name, offset) to deduplicate across patterns
    for kind, pattern in patterns:
        for m in pattern.finditer(content):
            name = m.group('name')
            offset = m.start('name')
            line = content[:offset].count('\n') + 1
            key = (name, offset)
            if key not in seen:
                seen.add(key)
                symbols.append((kind, name, line, offset))
    symbols.sort(key=lambda s: s[3])
    return symbols
