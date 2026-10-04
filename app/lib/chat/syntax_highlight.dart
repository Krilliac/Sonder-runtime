/// A small, dependency-free code tokenizer for fenced code blocks.
///
/// It recognises only four kinds of token: keywords, strings, comments and
/// numbers. Everything else is plain text. That keeps highlighting subtle
/// (the transcript stays a quiet instrument) and the scanner simple enough
/// to be obviously safe on model-authored input: one pass, no regular
/// expression backtracking over the whole block, and a size cap past which
/// a block is left plain. Colours are applied by the caller from theme
/// tokens; nothing here knows about colour.
library;

/// What a span of code is.
enum CodeTokenKind { plain, keyword, string, comment, number }

/// One run of code text of a single [kind].
class CodeToken {
  final CodeTokenKind kind;
  final String text;
  const CodeToken(this.kind, this.text);

  @override
  bool operator ==(Object other) =>
      other is CodeToken && other.kind == kind && other.text == text;

  @override
  int get hashCode => Object.hash(kind, text);

  @override
  String toString() => '${kind.name}(${text.replaceAll('\n', r'\n')})';
}

/// Blocks longer than this are shown plain: highlighting is decoration, and
/// a very long block should not cost a frame.
const maxHighlightedCodeChars = 60000;

/// The rules of one language family.
class _Language {
  final Set<String> keywords;
  final bool caseInsensitive;
  final List<String> lineComments;
  final List<(String, String)> blockComments;

  /// Quote characters that start a string.
  final String quotes;

  /// Quotes whose strings may span lines (template literals, here-strings).
  final String multilineQuotes;

  /// Python-style `"""` / `'''` strings.
  final bool tripleQuotes;

  /// `'` starts a string only as a one-character literal (`'a'`, `'\n'`),
  /// so Rust lifetimes (`'a`) stay plain.
  final bool charLiteralsOnly;

  /// `#` starts a comment only at the start of a line or after whitespace,
  /// as in shells (`${#list}` and `$#` are not comments).
  final bool hashNeedsSpace;

  /// A quote starts a string only after whitespace or punctuation, so the
  /// apostrophe in a plain YAML value (`note: don't`) stays plain.
  final bool quoteNeedsBoundary;

  const _Language({
    this.keywords = const {},
    this.caseInsensitive = false,
    this.lineComments = const [],
    this.blockComments = const [],
    this.quotes = '"\'',
    this.multilineQuotes = '',
    this.tripleQuotes = false,
    this.charLiteralsOnly = false,
    this.hashNeedsSpace = false,
    this.quoteNeedsBoundary = false,
  });
}

const _cFamily = {
  'auto',
  'break',
  'case',
  'catch',
  'char',
  'class',
  'const',
  'constexpr',
  'continue',
  'default',
  'delete',
  'do',
  'double',
  'else',
  'enum',
  'explicit',
  'extern',
  'false',
  'float',
  'for',
  'friend',
  'goto',
  'if',
  'inline',
  'int',
  'long',
  'namespace',
  'new',
  'noexcept',
  'nullptr',
  'operator',
  'override',
  'private',
  'protected',
  'public',
  'return',
  'short',
  'signed',
  'sizeof',
  'static',
  'struct',
  'switch',
  'template',
  'this',
  'throw',
  'true',
  'try',
  'typedef',
  'typename',
  'union',
  'unsigned',
  'using',
  'virtual',
  'void',
  'volatile',
  'while',
  'bool',
  'final',
  'import',
  'package',
  'extends',
  'implements',
  'interface',
  'instanceof',
  'super',
  'null',
  'var',
  'val',
  'fun',
  'is',
  'as',
  'in',
  'out',
  'object',
  'when',
  'string',
  'readonly',
  'async',
  'await',
  'base',
  'foreach',
  'lock',
  'yield',
  'abstract',
  'sealed',
  'record',
  'let',
  'func',
  'guard',
  'self',
  'nil',
  'protocol',
  'extension',
};

const _dart = {
  'abstract',
  'as',
  'assert',
  'async',
  'await',
  'base',
  'break',
  'case',
  'catch',
  'class',
  'const',
  'continue',
  'covariant',
  'default',
  'deferred',
  'do',
  'dynamic',
  'else',
  'enum',
  'export',
  'extends',
  'extension',
  'external',
  'factory',
  'false',
  'final',
  'finally',
  'for',
  'Function',
  'get',
  'hide',
  'if',
  'implements',
  'import',
  'in',
  'interface',
  'is',
  'late',
  'library',
  'mixin',
  'new',
  'null',
  'on',
  'operator',
  'part',
  'required',
  'rethrow',
  'return',
  'sealed',
  'set',
  'show',
  'static',
  'super',
  'switch',
  'sync',
  'this',
  'throw',
  'true',
  'try',
  'typedef',
  'var',
  'void',
  'when',
  'while',
  'with',
  'yield',
  'int',
  'double',
  'String',
  'bool',
  'num',
  'Object',
};

const _js = {
  'async',
  'await',
  'break',
  'case',
  'catch',
  'class',
  'const',
  'continue',
  'debugger',
  'default',
  'delete',
  'do',
  'else',
  'enum',
  'export',
  'extends',
  'false',
  'finally',
  'for',
  'from',
  'function',
  'get',
  'if',
  'implements',
  'import',
  'in',
  'instanceof',
  'interface',
  'let',
  'new',
  'null',
  'of',
  'private',
  'protected',
  'public',
  'readonly',
  'return',
  'set',
  'static',
  'super',
  'switch',
  'this',
  'throw',
  'true',
  'try',
  'type',
  'typeof',
  'undefined',
  'var',
  'void',
  'while',
  'with',
  'yield',
  'as',
  'any',
  'number',
  'string',
  'boolean',
  'keyof',
  'namespace',
};

const _python = {
  'False',
  'None',
  'True',
  'and',
  'as',
  'assert',
  'async',
  'await',
  'break',
  'class',
  'continue',
  'def',
  'del',
  'elif',
  'else',
  'except',
  'finally',
  'for',
  'from',
  'global',
  'if',
  'import',
  'in',
  'is',
  'lambda',
  'match',
  'case',
  'nonlocal',
  'not',
  'or',
  'pass',
  'raise',
  'return',
  'try',
  'while',
  'with',
  'yield',
  'self',
};

const _go = {
  'break',
  'case',
  'chan',
  'const',
  'continue',
  'default',
  'defer',
  'else',
  'fallthrough',
  'false',
  'for',
  'func',
  'go',
  'goto',
  'if',
  'import',
  'interface',
  'iota',
  'map',
  'nil',
  'package',
  'range',
  'return',
  'select',
  'struct',
  'switch',
  'true',
  'type',
  'var',
  'string',
  'int',
  'error',
  'bool',
  'byte',
};

const _rust = {
  'as',
  'async',
  'await',
  'break',
  'const',
  'continue',
  'crate',
  'dyn',
  'else',
  'enum',
  'extern',
  'false',
  'fn',
  'for',
  'if',
  'impl',
  'in',
  'let',
  'loop',
  'match',
  'mod',
  'move',
  'mut',
  'pub',
  'ref',
  'return',
  'self',
  'Self',
  'static',
  'struct',
  'super',
  'trait',
  'true',
  'type',
  'unsafe',
  'use',
  'where',
  'while',
  'i32',
  'i64',
  'u8',
  'u32',
  'u64',
  'usize',
  'f32',
  'f64',
  'bool',
  'str',
  'String',
  'Vec',
  'Option',
  'Result',
  'Some',
  'None',
  'Ok',
  'Err',
};

const _shell = {
  'if',
  'then',
  'else',
  'elif',
  'fi',
  'for',
  'while',
  'until',
  'do',
  'done',
  'case',
  'esac',
  'in',
  'function',
  'return',
  'local',
  'export',
  'readonly',
  'set',
  'unset',
  'echo',
  'exit',
  'source',
  'cd',
  'sudo',
};

const _powershell = {
  'begin',
  'break',
  'catch',
  'class',
  'continue',
  'data',
  'do',
  'dynamicparam',
  'else',
  'elseif',
  'end',
  'enum',
  'exit',
  'filter',
  'finally',
  'for',
  'foreach',
  'from',
  'function',
  'if',
  'in',
  'param',
  'process',
  'return',
  'switch',
  'throw',
  'trap',
  'try',
  'until',
  'using',
  'while',
};

const _sql = {
  'select',
  'from',
  'where',
  'and',
  'or',
  'not',
  'insert',
  'into',
  'values',
  'update',
  'set',
  'delete',
  'create',
  'table',
  'index',
  'view',
  'drop',
  'alter',
  'add',
  'join',
  'left',
  'right',
  'inner',
  'outer',
  'on',
  'as',
  'group',
  'by',
  'order',
  'having',
  'limit',
  'offset',
  'distinct',
  'null',
  'is',
  'in',
  'like',
  'between',
  'case',
  'when',
  'then',
  'else',
  'end',
  'primary',
  'key',
  'foreign',
  'references',
  'default',
  'union',
  'all',
  'exists',
  'with',
  'returning',
  'true',
  'false',
  'integer',
  'text',
};

const _json = {'true', 'false', 'null'};

const _yaml = {'true', 'false', 'null', 'yes', 'no', 'on', 'off'};

final Map<String, _Language> _languages = () {
  const c = _Language(
    keywords: _cFamily,
    lineComments: ['//'],
    blockComments: [('/*', '*/')],
  );
  const dart = _Language(
    keywords: _dart,
    lineComments: ['//'],
    blockComments: [('/*', '*/')],
    tripleQuotes: true,
  );
  const js = _Language(
    keywords: _js,
    lineComments: ['//'],
    blockComments: [('/*', '*/')],
    quotes: '"\'`',
    multilineQuotes: '`',
  );
  const python = _Language(
    keywords: _python,
    lineComments: ['#'],
    tripleQuotes: true,
  );
  const go = _Language(
    keywords: _go,
    lineComments: ['//'],
    blockComments: [('/*', '*/')],
    quotes: '"\'`',
    multilineQuotes: '`',
  );
  const rust = _Language(
    keywords: _rust,
    lineComments: ['//'],
    blockComments: [('/*', '*/')],
    charLiteralsOnly: true,
  );
  const shell = _Language(
    keywords: _shell,
    lineComments: ['#'],
    hashNeedsSpace: true,
  );
  const powershell = _Language(
    keywords: _powershell,
    caseInsensitive: true,
    lineComments: ['#'],
    blockComments: [('<#', '#>')],
  );
  const sql = _Language(
    keywords: _sql,
    caseInsensitive: true,
    lineComments: ['--'],
    blockComments: [('/*', '*/')],
    quotes: '\'"',
  );
  const json = _Language(keywords: _json, quotes: '"');
  const yaml =
      _Language(keywords: _yaml, lineComments: ['#'], quoteNeedsBoundary: true);
  const toml =
      _Language(keywords: _json, lineComments: ['#'], quoteNeedsBoundary: true);
  return {
    for (final name in [
      'c',
      'h',
      'cpp',
      'c++',
      'cc',
      'hpp',
      'cxx',
      'cs',
      'csharp',
      'c#',
      'java',
      'kotlin',
      'kt',
      'swift',
      'objc',
      'hlsl',
      'glsl',
      'cuda',
      'scala'
    ])
      name: c,
    'dart': dart,
    for (final name in [
      'js',
      'javascript',
      'jsx',
      'ts',
      'typescript',
      'tsx',
      'mjs',
      'cjs'
    ])
      name: js,
    for (final name in ['py', 'python', 'python3']) name: python,
    'go': go,
    'golang': go,
    for (final name in ['rs', 'rust']) name: rust,
    for (final name in ['sh', 'bash', 'shell', 'zsh', 'console']) name: shell,
    for (final name in ['ps1', 'powershell', 'pwsh', 'ps']) name: powershell,
    'sql': sql,
    for (final name in ['json', 'jsonc', 'json5']) name: json,
    for (final name in ['yaml', 'yml']) name: yaml,
    for (final name in ['toml', 'ini', 'cfg', 'conf']) name: toml,
  };
}();

/// The canonical name a fence's info string selects, lower-case, or empty.
/// `language-cpp` (the Markdown class) and `cpp {.numberLines}` both give
/// `cpp`.
String codeLanguageOf(String? info) {
  var text = (info ?? '').trim().toLowerCase();
  if (text.startsWith('language-')) text = text.substring(9);
  final cut = text.indexOf(RegExp(r'[\s{,]'));
  if (cut >= 0) text = text.substring(0, cut);
  return text;
}

/// Whether [language] has highlighting rules.
bool codeLanguageKnown(String language) =>
    _languages.containsKey(codeLanguageOf(language));

bool _isIdentStart(int c) =>
    (c >= 0x41 && c <= 0x5A) || // A-Z
    (c >= 0x61 && c <= 0x7A) || // a-z
    c == 0x5F || // _
    c == 0x24 || // $
    c >= 0x80;

bool _isIdentPart(int c) => _isIdentStart(c) || _isDigit(c);

bool _isDigit(int c) => c >= 0x30 && c <= 0x39;

bool _isSpace(int c) => c == 0x20 || c == 0x09 || c == 0x0A || c == 0x0D;

/// Whether the character at [index] lets a quote open a string after it.
bool _isBoundary(String code, int index) {
  final c = code.codeUnitAt(index);
  return _isSpace(c) || '([{,:=-'.codeUnits.contains(c);
}

final _number = RegExp(
    r'0[xX][0-9a-fA-F_]+|0[bB][01_]+|(?:\d[\d_]*)?\.?\d[\d_]*(?:[eE][+-]?\d+)?');

/// Split [code] into tokens for [language]. An unknown or empty language,
/// or a block over [maxHighlightedCodeChars], is one plain token. The
/// concatenated token texts always equal [code].
List<CodeToken> tokenizeCode(String code, String? language) {
  final rules = _languages[codeLanguageOf(language)];
  if (rules == null || code.isEmpty || code.length > maxHighlightedCodeChars) {
    return code.isEmpty ? const [] : [CodeToken(CodeTokenKind.plain, code)];
  }
  final out = <CodeToken>[];
  final plain = StringBuffer();
  void flushPlain() {
    if (plain.isEmpty) return;
    out.add(CodeToken(CodeTokenKind.plain, plain.toString()));
    plain.clear();
  }

  void emit(CodeTokenKind kind, String text) {
    flushPlain();
    out.add(CodeToken(kind, text));
  }

  final n = code.length;
  var i = 0;
  scan:
  while (i < n) {
    final c = code.codeUnitAt(i);

    // Block comments.
    for (final (open, close) in rules.blockComments) {
      if (code.startsWith(open, i)) {
        final end = code.indexOf(close, i + open.length);
        final stop = end < 0 ? n : end + close.length;
        emit(CodeTokenKind.comment, code.substring(i, stop));
        i = stop;
        continue scan;
      }
    }

    // Line comments.
    for (final marker in rules.lineComments) {
      if (!code.startsWith(marker, i)) continue;
      if (marker == '#' &&
          rules.hashNeedsSpace &&
          i > 0 &&
          !_isSpace(code.codeUnitAt(i - 1))) {
        continue;
      }
      final end = code.indexOf('\n', i);
      final stop = end < 0 ? n : end;
      emit(CodeTokenKind.comment, code.substring(i, stop));
      i = stop;
      continue scan;
    }

    // Strings.
    if (rules.quotes.contains(String.fromCharCode(c)) &&
        (!rules.quoteNeedsBoundary || i == 0 || _isBoundary(code, i - 1))) {
      final quote = String.fromCharCode(c);
      if (rules.tripleQuotes && code.startsWith(quote * 3, i)) {
        final end = code.indexOf(quote * 3, i + 3);
        final stop = end < 0 ? n : end + 3;
        emit(CodeTokenKind.string, code.substring(i, stop));
        i = stop;
        continue scan;
      }
      if (quote == "'" && rules.charLiteralsOnly) {
        final m = RegExp(r"'(?:\\.[^']{0,8}|[^'\\])'").matchAsPrefix(code, i);
        if (m == null) {
          plain.write(quote);
          i++;
          continue scan;
        }
        emit(CodeTokenKind.string, m.group(0)!);
        i = m.end;
        continue scan;
      }
      final multiline = rules.multilineQuotes.contains(quote);
      var j = i + 1;
      while (j < n) {
        final d = code.codeUnitAt(j);
        if (d == 0x5C) {
          // Backslash escape: skip the next character.
          j += 2;
          continue;
        }
        if (d == c) {
          j++;
          break;
        }
        if (d == 0x0A && !multiline) break;
        j++;
      }
      final stop = j > n ? n : j;
      emit(CodeTokenKind.string, code.substring(i, stop));
      i = stop;
      continue scan;
    }

    // Identifiers and keywords.
    if (_isIdentStart(c)) {
      var j = i + 1;
      while (j < n && _isIdentPart(code.codeUnitAt(j))) {
        j++;
      }
      final word = code.substring(i, j);
      final probe = rules.caseInsensitive ? word.toLowerCase() : word;
      if (rules.keywords.contains(probe)) {
        emit(CodeTokenKind.keyword, word);
      } else {
        plain.write(word);
      }
      i = j;
      continue scan;
    }

    // Numbers (never the digits inside an identifier, which was consumed
    // whole above).
    if (_isDigit(c) ||
        (c == 0x2E && i + 1 < n && _isDigit(code.codeUnitAt(i + 1)))) {
      final m = _number.matchAsPrefix(code, i);
      if (m != null && m.end > i) {
        emit(CodeTokenKind.number, m.group(0)!);
        i = m.end;
        continue scan;
      }
    }

    plain.writeCharCode(c);
    i++;
  }
  flushPlain();
  return out;
}
