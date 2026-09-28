// Model-authored Markdown must not fetch anything on its own: remote images
// are beacons and a Windows `file://host/share` image is an SMB/NTLM leak.
// Only inline `data:` images render; everything else is a placeholder that
// names the URL.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/workspace_ui.dart';

// A 1x1 transparent PNG.
const _pngDataUri = 'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

Future<void> _pump(WidgetTester tester, String markdown) =>
    tester.pumpWidget(MaterialApp(
      theme: SonderTheme.dark,
      home: Scaffold(
          body: SingleChildScrollView(
              child: ConversationContent(content: markdown))),
    ));

Iterable<ImageProvider> _providers(WidgetTester tester) =>
    tester.widgetList<Image>(find.byType(Image)).map((i) => i.image);

void main() {
  for (final url in [
    'https://attacker.example/p.png?d=secret',
    'http://attacker.example/p.png',
    'file://attacker-host/share/a.png',
    'file:///C:/Users/me/secret.png',
    '//attacker-host/share/a.png',
    r'\\attacker-host\share\a.png',
    'relative/a.png',
  ]) {
    testWidgets('does not load $url', (tester) async {
      await _pump(tester, 'before ![beacon]($url) after');
      await tester.pump();
      expect(_providers(tester).whereType<NetworkImage>(), isEmpty);
      expect(_providers(tester).whereType<FileImage>(), isEmpty);
      expect(find.byType(Image), findsNothing);
      expect(find.byKey(const Key('markdown-image-blocked')), findsOneWidget);
      expect(find.textContaining('Image not loaded', findRichText: true),
          findsOneWidget);
      expect(find.textContaining('beacon', findRichText: true), findsWidgets);
    });
  }

  testWidgets('an inline data: image renders from memory', (tester) async {
    await _pump(tester, '![dot]($_pngDataUri)');
    await tester.pump();
    final providers = _providers(tester).toList();
    expect(providers, hasLength(1));
    expect(providers.single, isA<MemoryImage>());
    expect(find.byKey(const Key('markdown-image-blocked')), findsNothing);
  });

  test('the policy allows only bounded data: images', () {
    expect(markdownImageAllowed(Uri.parse(_pngDataUri)), isTrue);
    expect(
        markdownImageAllowed(Uri.parse('data:text/html;base64,PGI+')), isFalse);
    expect(markdownImageAllowed(Uri.parse('https://x.example/a.png')), isFalse);
    expect(markdownImageAllowed(Uri.parse('file://host/share/a.png')), isFalse);
    final huge = 'data:image/png;base64,${'A' * (3 * 1024 * 1024)}';
    expect(markdownImageAllowed(Uri.parse(huge)), isFalse);
  });
}
