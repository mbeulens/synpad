#!/usr/bin/env python3
"""Generate PHP function signatures from JetBrains phpstorm-stubs.

Downloads the latest stubs tarball from GitHub, extracts function signatures
from Core + common extensions, and writes completion_php_generated.py at the
repo root. Run whenever you want to refresh coverage for a newer PHP version.

Usage:
    python3 tools/gen_php_completions.py
"""

import re
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

STUBS_URL = "https://github.com/JetBrains/phpstorm-stubs/archive/refs/heads/master.tar.gz"

DIRS = [
    "Core",
    "standard",
    "PDO",
    "mbstring",
    "curl",
    "date",
    "json",
    "fileinfo",
    "pcre",
    "mysqli",
]

# Find top-level function declarations. The anchor `(?:^|\n)\s*function\s+`
# excludes class methods, which are preceded by visibility keywords
# (public/private/protected/static/abstract/final) between the newline and
# `function`. After the name, we walk balanced parens manually because
# parameters can span multiple lines and contain attributes like
# `#[PhpStormStubsElementAvailable(from: '8.0')]` with nested parens.
FUNC_START_RE = re.compile(r'(?:^|\n)\s*function\s+(\w+)\s*\(')


def _find_matching_paren(text: str, start: int) -> int:
    """Return the index just past the matching `)` for an opening `(`.
    `start` is the position of the first char inside the parens.
    Returns -1 if unbalanced."""
    depth = 1
    i = start
    while i < len(text):
        c = text[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _strip_attributes(text: str) -> str:
    """Remove `#[...]` attribute blocks with balanced bracket matching.
    Attributes like `#[LanguageLevelTypeAware(['8.0' => 'CurlHandle'], default: 'resource')]`
    contain nested brackets, so a naïve regex stops at the first `]`."""
    out = []
    i = 0
    while i < len(text):
        if text[i:i + 2] == '#[':
            depth = 1
            j = i + 2
            while j < len(text) and depth > 0:
                if text[j] == '[':
                    depth += 1
                elif text[j] == ']':
                    depth -= 1
                j += 1
            if depth == 0:
                i = j
                continue
        out.append(text[i])
        i += 1
    return ''.join(out)


def extract_from_file(path: Path) -> dict:
    text = path.read_text(encoding='utf-8', errors='replace')
    # Strip block comments so commented-out `function` keywords don't match
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Strip PHP attributes #[...] with balanced bracket matching
    text = _strip_attributes(text)

    results: dict = {}
    for m in FUNC_START_RE.finditer(text):
        name = m.group(1)
        inner_start = m.end()
        close = _find_matching_paren(text, inner_start)
        if close < 0:
            continue
        params = re.sub(r'\s+', ' ', text[inner_start:close - 1].strip())

        # Look for optional return-type annotation `: TYPE` before `{` or `;`
        i = close
        while i < len(text) and text[i] in ' \t\n\r':
            i += 1
        ret = None
        if i < len(text) and text[i] == ':':
            i += 1
            ret_start = i
            while i < len(text) and text[i] not in '{;':
                i += 1
            ret = text[ret_start:i].strip()
        sig = f"({params}): {ret}" if ret else f"({params})"
        results.setdefault(name, sig)
    return results


def main() -> int:
    repo_root = Path(__file__).parent.parent
    output = repo_root / "completion_php_generated.py"

    print(f"Downloading {STUBS_URL}...")
    with tempfile.TemporaryDirectory() as td:
        tarball = Path(td) / "stubs.tar.gz"
        urllib.request.urlretrieve(STUBS_URL, tarball)
        print("Extracting...")
        with tarfile.open(tarball) as tf:
            tf.extractall(td)
        root_candidates = list(Path(td).glob("phpstorm-stubs-*"))
        if not root_candidates:
            print("Error: extracted directory not found.", file=sys.stderr)
            return 1
        root = root_candidates[0]
        print(f"Scanning {root}...")

        all_funcs: dict = {}
        for dirname in DIRS:
            d = root / dirname
            if not d.is_dir():
                print(f"  [skip] {dirname} (not present in stubs)")
                continue
            before = len(all_funcs)
            for php_file in d.rglob("*.php"):
                for name, sig in extract_from_file(php_file).items():
                    all_funcs.setdefault(name, sig)
            print(f"  [ok]   {dirname}: +{len(all_funcs) - before} (cumulative {len(all_funcs)})")

    print(f"Total unique functions: {len(all_funcs)}")

    with output.open('w') as f:
        f.write('"""Generated PHP function signatures from JetBrains phpstorm-stubs.\n\n')
        f.write('Do not edit by hand. Regenerate with tools/gen_php_completions.py.\n')
        f.write('"""\n\n')
        f.write('PHP_GENERATED = {\n')
        for name in sorted(all_funcs.keys()):
            sig = all_funcs[name].replace("\\", "\\\\").replace("'", "\\'")
            f.write(f"    '{name}': '{sig}',\n")
        f.write('}\n')
    print(f"Wrote {output}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
