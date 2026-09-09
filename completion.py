"""SynPad code completion providers and language dictionaries."""

import re

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GtkSource', '5')
from gi.repository import GObject, GtkSource, Gio

# Completions: {name: signature_or_None}
# None = keyword (no hint), string = function signature hint
PHP_COMPLETIONS = {
    # Keywords (no hints)
    'abstract': None, 'and': None, 'array': None, 'as': None, 'break': None,
    'callable': None, 'case': None, 'catch': None, 'class': None, 'clone': None,
    'const': None, 'continue': None, 'declare': None, 'default': None,
    'do': None, 'echo': None, 'else': None, 'elseif': None, 'empty': None,
    'extends': None, 'final': None, 'finally': None, 'fn': None, 'for': None,
    'foreach': None, 'function': None, 'global': None, 'if': None,
    'implements': None, 'include': None, 'include_once': None,
    'instanceof': None, 'interface': None, 'isset': None, 'list': None,
    'match': None, 'namespace': None, 'new': None, 'null': None, 'or': None,
    'print': None, 'private': None, 'protected': None, 'public': None,
    'readonly': None, 'require': None, 'require_once': None, 'return': None,
    'static': None, 'switch': None, 'throw': None, 'trait': None, 'try': None,
    'unset': None, 'use': None, 'var': None, 'while': None, 'yield': None,
    'true': None, 'false': None, 'self': None, 'parent': None,
    # Functions with signatures
    'array_key_exists': '(mixed $key, array $array): bool',
    'array_keys': '(array $array, mixed $filter_value?, bool $strict?): array',
    'array_map': '(callable $callback, array $array, array ...$arrays): array',
    'array_merge': '(array ...$arrays): array',
    'array_filter': '(array $array, callable $callback?, int $mode?): array',
    'array_pop': '(array &$array): mixed',
    'array_push': '(array &$array, mixed ...$values): int',
    'array_shift': '(array &$array): mixed',
    'array_slice': '(array $array, int $offset, int $length?, bool $preserve_keys?): array',
    'array_splice': '(array &$array, int $offset, int $length?, mixed $replacement?): array',
    'array_unique': '(array $array, int $flags?): array',
    'array_values': '(array $array): array',
    'array_combine': '(array $keys, array $values): array',
    'array_chunk': '(array $array, int $length, bool $preserve_keys?): array',
    'array_column': '(array $array, int|string $column_key, int|string $index_key?): array',
    'array_count_values': '(array $array): array',
    'array_diff': '(array $array, array ...$arrays): array',
    'array_intersect': '(array $array, array ...$arrays): array',
    'array_flip': '(array $array): array',
    'array_reverse': '(array $array, bool $preserve_keys?): array',
    'array_search': '(mixed $needle, array $haystack, bool $strict?): int|string|false',
    'array_sum': '(array $array): int|float',
    'array_rand': '(array $array, int $num?): int|string|array',
    'count': '(array|Countable $value, int $mode?): int',
    'in_array': '(mixed $needle, array $haystack, bool $strict?): bool',
    'sort': '(array &$array, int $flags?): bool',
    'usort': '(array &$array, callable $callback): bool',
    'ksort': '(array &$array, int $flags?): bool',
    'implode': '(string $separator, array $array): string',
    'explode': '(string $separator, string $string, int $limit?): array',
    'compact': '(string|array ...$var_names): array',
    'extract': '(array &$array, int $flags?, string $prefix?): int',
    'range': '(mixed $start, mixed $end, int|float $step?): array',
    'str_contains': '(string $haystack, string $needle): bool',
    'str_replace': '(mixed $search, mixed $replace, mixed $subject, int &$count?): mixed',
    'str_starts_with': '(string $haystack, string $needle): bool',
    'str_ends_with': '(string $haystack, string $needle): bool',
    'strlen': '(string $string): int',
    'strpos': '(string $haystack, string $needle, int $offset?): int|false',
    'strtolower': '(string $string): string',
    'strtoupper': '(string $string): string',
    'substr': '(string $string, int $offset, int $length?): string',
    'trim': '(string $string, string $characters?): string',
    'ltrim': '(string $string, string $characters?): string',
    'rtrim': '(string $string, string $characters?): string',
    'sprintf': '(string $format, mixed ...$values): string',
    'printf': '(string $format, mixed ...$values): int',
    'number_format': '(float $num, int $decimals?, string $dec_point?, string $thousands_sep?): string',
    'preg_match': '(string $pattern, string $subject, array &$matches?, int $flags?, int $offset?): int|false',
    'preg_replace': '(mixed $pattern, mixed $replacement, mixed $subject, int $limit?, int &$count?): mixed',
    'preg_match_all': '(string $pattern, string $subject, array &$matches?, int $flags?, int $offset?): int|false',
    'preg_split': '(string $pattern, string $subject, int $limit?, int $flags?): array|false',
    'file_get_contents': '(string $filename, bool $use_include_path?, resource $context?, int $offset?, int $length?): string|false',
    'file_put_contents': '(string $filename, mixed $data, int $flags?, resource $context?): int|false',
    'file_exists': '(string $filename): bool',
    'fopen': '(string $filename, string $mode, bool $use_include_path?, resource $context?): resource|false',
    'fclose': '(resource $stream): bool',
    'fread': '(resource $stream, int $length): string|false',
    'fwrite': '(resource $stream, string $data, int $length?): int|false',
    'fgets': '(resource $stream, int $length?): string|false',
    'is_file': '(string $filename): bool',
    'is_dir': '(string $filename): bool',
    'mkdir': '(string $directory, int $permissions?, bool $recursive?, resource $context?): bool',
    'rmdir': '(string $directory, resource $context?): bool',
    'unlink': '(string $filename, resource $context?): bool',
    'rename': '(string $from, string $to, resource $context?): bool',
    'copy': '(string $from, string $to, resource $context?): bool',
    'realpath': '(string $path): string|false',
    'dirname': '(string $path, int $levels?): string',
    'basename': '(string $path, string $suffix?): string',
    'json_encode': '(mixed $value, int $flags?, int $depth?): string|false',
    'json_decode': '(string $json, bool $assoc?, int $depth?, int $flags?): mixed',
    'var_dump': '(mixed ...$values): void',
    'print_r': '(mixed $value, bool $return?): string|bool',
    'var_export': '(mixed $value, bool $return?): string|null',
    'intval': '(mixed $value, int $base?): int',
    'floatval': '(mixed $value): float',
    'strval': '(mixed $value): string',
    'is_array': '(mixed $value): bool',
    'is_string': '(mixed $value): bool',
    'is_int': '(mixed $value): bool',
    'is_null': '(mixed $value): bool',
    'is_numeric': '(mixed $value): bool',
    'is_bool': '(mixed $value): bool',
    'is_object': '(mixed $value): bool',
    'date': '(string $format, int $timestamp?): string',
    'time': '(): int',
    'strtotime': '(string $datetime, int $baseTimestamp?): int|false',
    'mktime': '(int $hour, int $minute?, int $second?, int $month?, int $day?, int $year?): int|false',
    'header': '(string $header, bool $replace?, int $response_code?): void',
    'setcookie': '(string $name, string $value?, int $expires?, string $path?, string $domain?, bool $secure?, bool $httponly?): bool',
    'session_start': '(array $options?): bool',
    'session_destroy': '(): bool',
    'htmlspecialchars': '(string $string, int $flags?, string $encoding?, bool $double_encode?): string',
    'htmlentities': '(string $string, int $flags?, string $encoding?, bool $double_encode?): string',
    'urlencode': '(string $string): string',
    'urldecode': '(string $string): string',
    'base64_encode': '(string $string): string',
    'base64_decode': '(string $string, bool $strict?): string|false',
    'md5': '(string $string, bool $binary?): string',
    'sha1': '(string $string, bool $binary?): string',
    'password_hash': '(string $password, string|int $algo, array $options?): string',
    'password_verify': '(string $password, string $hash): bool',
    'rand': '(int $min?, int $max?): int',
    'mt_rand': '(int $min?, int $max?): int',
    'min': '(mixed ...$values): mixed',
    'max': '(mixed ...$values): mixed',
    'abs': '(int|float $num): int|float',
    'ceil': '(int|float $num): float',
    'floor': '(int|float $num): float',
    'round': '(int|float $num, int $precision?, int $mode?): float',
    'pow': '(mixed $base, mixed $exp): int|float',
    'sqrt': '(float $num): float',
    'class_exists': '(string $class, bool $autoload?): bool',
    'method_exists': '(object|string $object_or_class, string $method): bool',
    'property_exists': '(object|string $object_or_class, string $property): bool',
    'mysqli_connect': '(string $host?, string $username?, string $password?, string $database?, int $port?, string $socket?): mysqli|false',
    'mysqli_query': '(mysqli $mysql, string $query, int $result_mode?): mysqli_result|bool',
    'PDO': None, 'PDOStatement': None, 'Exception': None,
    'TypeError': None, 'RuntimeException': None,
    'stdClass': None, 'ArrayObject': None, 'DateTime': None,
    'DateTimeImmutable': None,
}

# Merge in generated signatures from JetBrains phpstorm-stubs. Generated set
# is the base; hand-written entries above override any overlap so local
# tweaks are preserved. Regenerate with tools/gen_php_completions.py.
try:
    from completion_php_generated import PHP_GENERATED as _PHP_GENERATED
    _merged = dict(_PHP_GENERATED)
    _merged.update(PHP_COMPLETIONS)
    PHP_COMPLETIONS = _merged
    del _merged, _PHP_GENERATED
except ImportError:
    pass

JS_COMPLETIONS = {
    # Keywords (no hints)
    'abstract': None, 'arguments': None, 'async': None, 'await': None,
    'break': None, 'case': None, 'class': None, 'const': None,
    'continue': None, 'debugger': None, 'default': None, 'delete': None,
    'do': None, 'else': None, 'enum': None, 'export': None, 'extends': None,
    'false': None, 'finally': None, 'for': None, 'from': None,
    'function': None, 'get': None, 'if': None, 'implements': None,
    'import': None, 'in': None, 'instanceof': None, 'interface': None,
    'let': None, 'new': None, 'null': None, 'of': None, 'package': None,
    'private': None, 'protected': None, 'public': None, 'return': None,
    'set': None, 'static': None, 'super': None, 'switch': None,
    'this': None, 'throw': None, 'true': None, 'try': None, 'typeof': None,
    'undefined': None, 'var': None, 'void': None, 'while': None,
    'with': None, 'yield': None,
    # TypeScript extras
    'type': None, 'namespace': None, 'declare': None, 'module': None,
    'readonly': None, 'keyof': None, 'infer': None, 'never': None,
    'unknown': None, 'any': None, 'string': None, 'number': None,
    'boolean': None, 'symbol': None, 'object': None,
    'Record': None, 'Partial': None, 'Required': None, 'Pick': None, 'Omit': None,
    # Functions/methods with signatures
    'console.log': '(...data: any[]): void',
    'console.error': '(...data: any[]): void',
    'console.warn': '(...data: any[]): void',
    'console.info': '(...data: any[]): void',
    'console.table': '(data: any, columns?: string[]): void',
    'console.dir': '(obj: any, options?: object): void',
    'document.getElementById': '(id: string): HTMLElement | null',
    'document.querySelector': '(selectors: string): Element | null',
    'document.querySelectorAll': '(selectors: string): NodeList',
    'document.createElement': '(tagName: string): HTMLElement',
    'document.addEventListener': '(type: string, listener: Function, options?: object): void',
    'window.addEventListener': '(type: string, listener: Function, options?: object): void',
    'JSON.parse': '(text: string, reviver?: Function): any',
    'JSON.stringify': '(value: any, replacer?: Function, space?: number): string',
    'Math.abs': '(x: number): number',
    'Math.ceil': '(x: number): number',
    'Math.floor': '(x: number): number',
    'Math.round': '(x: number): number',
    'Math.max': '(...values: number[]): number',
    'Math.min': '(...values: number[]): number',
    'Math.random': '(): number',
    'Math.pow': '(base: number, exponent: number): number',
    'Math.sqrt': '(x: number): number',
    'Object.keys': '(obj: object): string[]',
    'Object.values': '(obj: object): any[]',
    'Object.entries': '(obj: object): [string, any][]',
    'Object.assign': '(target: object, ...sources: object[]): object',
    'Object.freeze': '(obj: T): Readonly<T>',
    'Object.defineProperty': '(obj: object, prop: string, descriptor: object): object',
    'Array.isArray': '(value: any): boolean',
    'Array.from': '(arrayLike: Iterable, mapFn?: Function): any[]',
    'Array.of': '(...items: any[]): any[]',
    'Promise': None, 'Promise.all': '(values: Promise[]): Promise<any[]>',
    'Promise.resolve': '(value?: any): Promise',
    'Promise.reject': '(reason?: any): Promise',
    'setTimeout': '(callback: Function, delay?: number, ...args: any[]): number',
    'setInterval': '(callback: Function, delay?: number, ...args: any[]): number',
    'clearTimeout': '(id: number): void',
    'clearInterval': '(id: number): void',
    'parseInt': '(string: string, radix?: number): number',
    'parseFloat': '(string: string): number',
    'isNaN': '(value: any): boolean',
    'isFinite': '(value: any): boolean',
    'encodeURIComponent': '(uri: string): string',
    'decodeURIComponent': '(uri: string): string',
    'fetch': '(input: string | Request, init?: RequestInit): Promise<Response>',
    'Map': None, 'Set': None, 'WeakMap': None, 'WeakSet': None,
    'Symbol': None, 'Proxy': None, 'Reflect': None,
    'RegExp': None, 'Date': None, 'Error': None, 'TypeError': None, 'RangeError': None,
    'Request': None, 'Response': None, 'Headers': None,
    'URL': None, 'URLSearchParams': None,
    # Array/String methods
    'addEventListener': '(type: string, listener: Function, options?: object): void',
    'appendChild': '(node: Node): Node',
    'charAt': '(index: number): string',
    'concat': '(...items: any[]): any[] | string',
    'endsWith': '(searchString: string, length?: number): boolean',
    'every': '(callback: (value, index, array) => boolean): boolean',
    'fill': '(value: any, start?: number, end?: number): any[]',
    'filter': '(callback: (value, index, array) => boolean): any[]',
    'find': '(callback: (value, index, array) => boolean): any | undefined',
    'findIndex': '(callback: (value, index, array) => boolean): number',
    'flat': '(depth?: number): any[]',
    'flatMap': '(callback: (value, index, array) => any): any[]',
    'forEach': '(callback: (value, index, array) => void): void',
    'includes': '(searchElement: any, fromIndex?: number): boolean',
    'indexOf': '(searchElement: any, fromIndex?: number): number',
    'join': '(separator?: string): string',
    'lastIndexOf': '(searchElement: any, fromIndex?: number): number',
    'map': '(callback: (value, index, array) => any): any[]',
    'match': '(regexp: RegExp | string): string[] | null',
    'matchAll': '(regexp: RegExp): IterableIterator<RegExpMatchArray>',
    'padEnd': '(targetLength: number, padString?: string): string',
    'padStart': '(targetLength: number, padString?: string): string',
    'pop': '(): any | undefined',
    'push': '(...items: any[]): number',
    'reduce': '(callback: (acc, value, index, array) => any, initialValue?: any): any',
    'repeat': '(count: number): string',
    'replace': '(search: string | RegExp, replacement: string | Function): string',
    'replaceAll': '(search: string | RegExp, replacement: string): string',
    'reverse': '(): any[]',
    'shift': '(): any | undefined',
    'slice': '(start?: number, end?: number): any[] | string',
    'some': '(callback: (value, index, array) => boolean): boolean',
    'sort': '(compareFn?: (a, b) => number): any[]',
    'splice': '(start: number, deleteCount?: number, ...items: any[]): any[]',
    'split': '(separator: string | RegExp, limit?: number): string[]',
    'startsWith': '(searchString: string, position?: number): boolean',
    'substring': '(start: number, end?: number): string',
    'then': '(onFulfilled?: Function, onRejected?: Function): Promise',
    'catch': '(onRejected?: Function): Promise',
    'toFixed': '(digits?: number): string',
    'toLowerCase': '(): string',
    'toUpperCase': '(): string',
    'trim': '(): string',
    'trimEnd': '(): string',
    'trimStart': '(): string',
    'unshift': '(...items: any[]): number',
    'length': None,
    # React
    'useState': '<T>(initialState: T | () => T): [T, (value: T) => void]',
    'useEffect': '(effect: () => void | (() => void), deps?: any[]): void',
    'useCallback': '<T>(callback: T, deps: any[]): T',
    'useMemo': '<T>(factory: () => T, deps: any[]): T',
    'useRef': '<T>(initialValue: T): { current: T }',
    'useContext': '<T>(context: Context<T>): T',
    'useReducer': '(reducer: Function, initialState: any): [state, dispatch]',
    'createElement': '(type: string, props?: object, ...children: any[]): ReactElement',
    'Component': None, 'Fragment': None, 'StrictMode': None,
    'module.exports': None, 'require': '(id: string): any', 'exports': None,
}

# Map file extensions to completion dicts
COMPLETION_LANGS = {
    'php': PHP_COMPLETIONS,
    'js': JS_COMPLETIONS,
    'jsx': JS_COMPLETIONS,
    'ts': JS_COMPLETIONS,
    'tsx': JS_COMPLETIONS,
}


# ---------------------------------------------------------------------------
# Completion providers
#
# GtkSourceView 5's GtkSourceCompletionProvider cannot be implemented from
# Python on this stack. Its only population entry point is the async
# populate_async/populate_finish vfunc pair, and a provider that implements
# them segfaults GtkSourceView on the first keystroke -- reproduced with a
# minimal do-nothing provider, so it is not specific to our logic:
#
#     GLib-GIO-CRITICAL: g_task_get_source_object: assertion 'G_IS_TASK' failed
#     Segmentation fault
#
# The built-in C providers are unaffected (verified: no providers, and
# CompletionWords, and CompletionSnippets all survive typing). So word
# completion now goes through GtkSource.CompletionWords, which accepts extra
# buffers via register() -- we seed one with the language's keyword and
# function names.
#
# What changes for the user: the completion list shows bare names rather than
# "name  (signature)". The signatures themselves are unaffected -- they come
# from signature_help.py's popover, which reads the same tables below.


def make_completion_providers(lang_dict, doc_buffer):
    """Build the completion providers for one editor tab.

    `lang_dict` is a COMPLETION_LANGS entry (or None for languages we have no
    word list for); `doc_buffer` is the tab's own buffer, so words already in
    the document are completed too.

    Returns (providers, keep_alive). The caller must retain `keep_alive` for
    as long as the tab lives: it holds the hidden GtkSource.Buffer seeded with
    the language words, which CompletionWords does not own.
    """
    providers = []
    keep_alive = []

    if lang_dict:
        seed = GtkSource.Buffer()
        seed.set_text(" ".join(sorted(lang_dict.keys())))
        lang_words = GtkSource.CompletionWords.new('SynPad')
        lang_words.set_property('priority', 1)
        lang_words.set_property('minimum-word-size', 2)
        lang_words.register(seed)
        providers.append(lang_words)
        keep_alive.append(seed)

    doc_words = GtkSource.CompletionWords.new('Document')
    doc_words.set_property('priority', 0)
    doc_words.set_property('minimum-word-size', 3)
    doc_words.register(doc_buffer)
    providers.append(doc_words)

    return providers, keep_alive
