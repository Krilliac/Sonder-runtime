import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// The design system's source rules (DESIGN.md, "Components" and "Motion and
/// content"): a colour value is written only in `lib/theme.dart`, padding and
/// gaps come from `SonderSpace`, and a popup menu opens on the shared motion.
/// Each is checked against the source, so a screen cannot drift from the
/// tokens without this failing.
void main() {
  List<File> sources() => Directory('lib')
      .listSync(recursive: true)
      .whereType<File>()
      .where((file) => file.path.endsWith('.dart'))
      .toList();

  String pathOf(File file) => file.path.replaceAll('\\', '/');

  // A literal colour: a hex or channel constructor, or a Material swatch.
  // `Colors.transparent` is not a colour choice.
  final colourValue = RegExp(r'\bColor\(0x|\bColor\.from(?:ARGB|RGBO)\('
      r'|\bColors\.(?!transparent\b)\w+|\bCupertinoColors\.');

  List<String> colourHits(String name, List<String> lines) => [
        for (var i = 0; i < lines.length; i++)
          if (!lines[i].trimLeft().startsWith('//') &&
              colourValue.hasMatch(lines[i]))
            '$name:${i + 1}: ${lines[i].trim()}',
      ];

  // A raw number where a spacing token belongs: inside an EdgeInsets
  // constructor, as a SizedBox that is only a gap, or as a spacing argument.
  // Zero is not a spacing choice.
  final insets = RegExp(
      r'\bEdgeInsets(?:Directional)?\.(?:all|only|symmetric|fromLTRB|fromSTEB)\(');
  final number = RegExp(r'(?<![\w.$])(\d+(?:\.\d+)?)(?![\w.])');
  final gap = RegExp(r'\bSizedBox\((?:width|height): (\d+(?:\.\d+)?)\)');
  final spacing = RegExp(
      r'\b(?:spacing|runSpacing|mainAxisSpacing|crossAxisSpacing): (\d+(?:\.\d+)?)\b');

  int closingParen(String source, int open) {
    var depth = 0;
    for (var i = open; i < source.length; i++) {
      final ch = source[i];
      if (ch == '(') depth++;
      if (ch == ')' && --depth == 0) return i;
    }
    return source.length;
  }

  List<String> spacingHits(String name, String source) {
    int lineOf(int offset) =>
        '\n'.allMatches(source.substring(0, offset)).length + 1;
    final hits = <String>[];
    for (final match in insets.allMatches(source)) {
      final args =
          source.substring(match.end, closingParen(source, match.end - 1));
      for (final value in number.allMatches(args)) {
        if (double.parse(value.group(1)!) != 0) {
          hits.add('$name:${lineOf(match.start)}: ${value.group(1)} in '
              'EdgeInsets(${args.replaceAll(RegExp(r'\s+'), ' ')})');
        }
      }
    }
    for (final pattern in [gap, spacing]) {
      for (final match in pattern.allMatches(source)) {
        if (double.parse(match.group(1)!) != 0) {
          hits.add('$name:${lineOf(match.start)}: ${match.group(0)}');
        }
      }
    }
    return hits;
  }

  // A popup menu that does not name its motion opens on Material's own
  // 300 ms; each one takes the shared style instead.
  final popupMenu = RegExp(r'\b(?:PopupMenuButton|showMenu)\b(?:<[^(]*>)?\(');
  const menuMotion = 'popUpAnimationStyle: SonderMotion.menu(context)';

  List<String> menuHits(String name, String source) => [
        for (final match in popupMenu.allMatches(source))
          if (!source
              .substring(match.end, closingParen(source, match.end - 1))
              .contains(menuMotion))
            '$name:${'\n'.allMatches(source.substring(0, match.start)).length + 1}: '
                '${match.group(0)}',
      ];

  test('the scanners see the app and catch what they are for', () {
    // A scan that reads nothing, or matches nothing, would pass for the
    // wrong reason: prove both have teeth before trusting an empty result.
    final files = sources();
    expect(files.length, greaterThan(60));
    expect(files.map(pathOf), contains('lib/theme.dart'));
    expect(
        colourHits('x.dart', [
          '  color: const Color(0xFF112233),',
          '  color: Colors.teal,',
          '  color: Color.fromARGB(255, 1, 2, 3),',
          '  // color: Color(0xFF000000) in a comment',
          '  color: Colors.transparent,',
          '  gradient: SonderMarkColors.gradient,',
        ]),
        hasLength(3));
    expect(
        spacingHits(
            'x.dart',
            'padding: const EdgeInsets.symmetric(\n'
                '    horizontal: 6, vertical: wide ? 0 : SonderSpace.sm),\n'
                'const SizedBox(height: 12),\n'
                'Wrap(spacing: 8, runSpacing: SonderSpace.xs),\n'
                'const SizedBox(width: 20, height: 20),\n'
                'padding: EdgeInsets.zero,\n'
                'padding: const EdgeInsets.all(SonderSpace.x5 + SonderSpace.x5),\n'),
        hasLength(3));
    expect(
        menuHits(
            'x.dart',
            'PopupMenuButton<String>(\n'
                '  tooltip: "a",\n'
                '  itemBuilder: (context) => []),\n'
                'PopupMenuButton<Map<String, int>>(\n'
                '  popUpAnimationStyle: SonderMotion.menu(context),\n'
                '  itemBuilder: (context) => []),\n'
                'showMenu<int>(context: context, items: const []),\n'
                'final PopupMenuButton<String> held;\n'),
        ['x.dart:1: PopupMenuButton<String>(', 'x.dart:7: showMenu<int>(']);
  });

  test('a colour value is written only in theme.dart', () {
    final hits = <String>[
      for (final file in sources())
        if (pathOf(file) != 'lib/theme.dart')
          ...colourHits(pathOf(file), file.readAsLinesSync()),
    ];
    expect(hits, isEmpty,
        reason: 'read the colour from SonderTokens or the colour scheme');
  });

  test('padding and gaps come from SonderSpace', () {
    final hits = <String>[
      for (final file in sources())
        ...spacingHits(pathOf(file), file.readAsStringSync()),
    ];
    expect(hits, isEmpty,
        reason: 'use a SonderSpace token (extend the scale in theme.dart '
            'if a step is missing)');
  });

  test('every popup menu opens on the shared motion', () {
    var menus = 0;
    final hits = <String>[];
    for (final file in sources()) {
      final source = file.readAsStringSync();
      menus += popupMenu.allMatches(source).length;
      hits.addAll(menuHits(pathOf(file), source));
    }
    // The composer's + menu, the sidebar's project filter, the workspace
    // menu: a scan that finds none of them is not looking.
    expect(menus, greaterThanOrEqualTo(3));
    expect(hits, isEmpty, reason: 'pass $menuMotion');
  });
}
