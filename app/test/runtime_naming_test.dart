import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Plan P2-9: the workspace is "Runtime" everywhere a person reads it.
///
/// Scoped to the Runtime/Settings/Agents/App-control files; `chat_screen.dart`
/// (Chat lane) and the transport's `SonderException.transport` copy
/// (`api.dart`, transport lane) are checked by their owners.
void main() {
  test('no user-visible "System" name for the Runtime workspace', () {
    final files = <File>[
      ...Directory('lib/runtime')
          .listSync(recursive: true)
          .whereType<File>()
          .where((file) => file.path.endsWith('.dart')),
      for (final name in const [
        'settings_screen.dart',
        'agent_screen.dart',
        'app_control_screen.dart',
        'app_work_screen.dart',
      ])
        File('lib/$name'),
    ];
    final banned = RegExp(
        r"""['"](System|System page|System screen|System sections)['"]|the System (page|screen)""");
    final hits = <String>[];
    for (final file in files) {
      final lines = file.readAsLinesSync();
      for (var i = 0; i < lines.length; i++) {
        final line = lines[i].trim();
        if (line.startsWith('//')) continue;
        if (banned.hasMatch(line)) hits.add('${file.path}:${i + 1}: $line');
      }
    }
    expect(hits, isEmpty);
  });
}
