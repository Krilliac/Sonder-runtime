import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/chat/links.dart';
import 'package:sonder_runtime/chat/markdown.dart' show CodeBlockView;
import 'package:sonder_runtime/chat/syntax_highlight.dart';
import 'package:sonder_runtime/theme.dart';
import 'package:sonder_runtime/workspace_ui.dart';

import 'goldens/golden_fonts.dart';

Widget _app(Widget child) => MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: SonderTheme.dark,
      home: Scaffold(
        body: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: child,
        ),
      ),
    );

/// Records clipboard writes; returns the list it fills.
List<String> _recordClipboard(WidgetTester tester) {
  final copied = <String>[];
  tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
    SystemChannels.platform,
    (call) async {
      if (call.method == 'Clipboard.setData') {
        copied.add((call.arguments as Map)['text'] as String);
      }
      return null;
    },
  );
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(SystemChannels.platform, null));
  return copied;
}

String _joined(List<CodeToken> tokens) => tokens.map((t) => t.text).join();

List<String> _of(List<CodeToken> tokens, CodeTokenKind kind) => [
      for (final t in tokens)
        if (t.kind == kind) t.text
    ];

void main() {
  setUpAll(loadGoldenFonts);

  group('tokenizeCode', () {
    test('cpp: keywords, comments and numbers; the text is unchanged', () {
      const code = '// warm at load\n'
          'for (const auto& d : descs) {\n'
          '  cache->Store(d, /*ttl=*/3600, "pso");\n'
          '}';
      final tokens = tokenizeCode(code, 'cpp');
      expect(_joined(tokens), code);
      expect(_of(tokens, CodeTokenKind.keyword), ['for', 'const', 'auto']);
      expect(
          _of(tokens, CodeTokenKind.comment), ['// warm at load', '/*ttl=*/']);
      expect(_of(tokens, CodeTokenKind.number), ['3600']);
      expect(_of(tokens, CodeTokenKind.string), ['"pso"']);
    });

    test('python: triple-quoted strings and # comments', () {
      const code = 'def warm(c):\n    """Once."""\n    x = 42  # sample\n';
      final tokens = tokenizeCode(code, 'python');
      expect(_joined(tokens), code);
      expect(_of(tokens, CodeTokenKind.keyword), ['def']);
      expect(_of(tokens, CodeTokenKind.string), ['"""Once."""']);
      expect(_of(tokens, CodeTokenKind.comment), ['# sample']);
      expect(_of(tokens, CodeTokenKind.number), ['42']);
    });

    test('digits inside an identifier are not a number', () {
      final tokens = tokenizeCode('var1 = x2 + 3;', 'js');
      expect(_of(tokens, CodeTokenKind.number), ['3']);
    });

    test('shell: # opens a comment only after a space', () {
      final tokens = tokenizeCode('echo \$# # count', 'bash');
      expect(_of(tokens, CodeTokenKind.comment), ['# count']);
    });

    test("rust: a lifetime is not a string, a char literal is", () {
      final tokens = tokenizeCode("fn f<'a>(x: &'a str) { 'z' }", 'rust');
      expect(_of(tokens, CodeTokenKind.string), ["'z'"]);
    });

    test("yaml: an apostrophe in a plain value is not a string", () {
      final tokens = tokenizeCode("note: don't\nname: 'sonder'", 'yaml');
      expect(_of(tokens, CodeTokenKind.string), ["'sonder'"]);
    });

    test('an unknown language or a huge block stays plain', () {
      expect(tokenizeCode('for x', ''),
          [const CodeToken(CodeTokenKind.plain, 'for x')]);
      expect(tokenizeCode('for x', 'brainfuck'),
          [const CodeToken(CodeTokenKind.plain, 'for x')]);
      final huge = 'for ' * (maxHighlightedCodeChars ~/ 4 + 1);
      expect(tokenizeCode(huge, 'cpp'), hasLength(1));
    });

    test('fence info strings resolve to a language', () {
      expect(codeLanguageOf('language-cpp'), 'cpp');
      expect(codeLanguageOf('Python {.numberLines}'), 'python');
      expect(codeLanguageKnown('TS'), isTrue);
      expect(codeLanguageKnown('cobol'), isFalse);
    });
  });

  group('decideLink', () {
    test('web and mail addresses open', () {
      expect(decideLink('the docs', 'https://learn.microsoft.com/a'),
          LinkDecision.open);
      expect(decideLink('docs.python.org', 'https://docs.python.org/3/'),
          LinkDecision.open);
      expect(decideLink('www.example.com', 'https://example.com'),
          LinkDecision.open);
      expect(
          decideLink('mail me', 'mailto:ops@example.com'), LinkDecision.open);
    });

    test('text naming another site, credentials or look-alike hosts ask', () {
      expect(decideLink('paypal.com', 'https://evil.example/login'),
          LinkDecision.confirm);
      expect(decideLink('the bank', 'https://bank.example@evil.example/'),
          LinkDecision.confirm);
      expect(decideLink('docs', 'https://xn--pple-43d.com/'),
          LinkDecision.confirm);
    });

    test('anything that is not a web address is shown, never opened', () {
      for (final href in [
        'file:///C:/Windows/System32',
        r'\\host\share\x.png',
        'javascript:alert(1)',
        'ms-settings:privacy',
        'docs/readme.md',
        '',
        null,
      ]) {
        expect(decideLink('link', href), LinkDecision.show, reason: '$href');
      }
    });
  });

  group('code blocks', () {
    testWidgets('a header names the language; Copy copies and confirms',
        (tester) async {
      final copied = _recordClipboard(tester);
      await tester.pumpWidget(_app(const ConversationContent(
        content: 'Try:\n\n```cpp\nint x = 1;\n```',
        fullWidthCode: true,
      )));
      expect(find.byKey(const Key('code-block-language')), findsOneWidget);
      expect(find.text('cpp'), findsOneWidget);
      expect(find.text('Copy'), findsOneWidget);

      await tester.tap(find.byKey(const Key('code-block-copy')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 500));
      expect(copied, ['int x = 1;']);
      // Visible confirmation: the button says so, and a toast.
      expect(find.text('Copied'), findsOneWidget);
      expect(find.text('Code copied'), findsOneWidget);
      await tester.pump(const Duration(seconds: 3));
      expect(find.text('Copy'), findsOneWidget);
    });

    testWidgets('long lines scroll sideways instead of wrapping',
        (tester) async {
      final line = 'x' * 400;
      await tester.pumpWidget(_app(ConversationContent(
        content: '```\n$line\n```',
        fullWidthCode: true,
      )));
      final scrolls = find.descendant(
          of: find.byType(CodeBlockView),
          matching: find.byType(SingleChildScrollView));
      expect(tester.widget<SingleChildScrollView>(scrolls).scrollDirection,
          Axis.horizontal);
      expect(tester.takeException(), isNull);
    });
  });

  group('links', () {
    late List<Uri> opened;
    late ExternalLinkOpener original;

    setUp(() {
      opened = [];
      original = externalLinkOpener;
      externalLinkOpener = (uri) async {
        opened.add(uri);
        return true;
      };
    });
    tearDown(() => externalLinkOpener = original);

    testWidgets('a web link opens outside the app', (tester) async {
      await tester.pumpWidget(_app(const ConversationContent(
          content: '[the pipeline docs](https://learn.microsoft.com/d3d12)')));
      await tester
          .tap(find.textContaining('the pipeline docs', findRichText: true));
      await tester.pump();
      expect(opened, [Uri.parse('https://learn.microsoft.com/d3d12')]);
    });

    testWidgets('a file link is shown with Copy link, not opened',
        (tester) async {
      final copied = _recordClipboard(tester);
      await tester.pumpWidget(_app(
          const ConversationContent(content: '[notes](file:///C:/x.txt)')));
      await tester.tap(find.textContaining('notes', findRichText: true));
      await tester.pumpAndSettle();
      expect(opened, isEmpty);
      expect(find.text('Link not opened'), findsOneWidget);
      expect(find.byKey(const Key('link-sheet-open')), findsNothing);
      await tester.tap(find.byKey(const Key('link-sheet-copy')));
      await tester.pumpAndSettle();
      expect(copied, ['file:///C:/x.txt']);
      expect(find.text('Link copied'), findsOneWidget);
    });

    testWidgets('a link whose text names another site asks first',
        (tester) async {
      await tester.pumpWidget(_app(const ConversationContent(
          content: '[paypal.com](https://evil.example/login)')));
      await tester.tap(find.textContaining('paypal.com', findRichText: true));
      await tester.pumpAndSettle();
      expect(opened, isEmpty);
      expect(find.text('Open this link?'), findsOneWidget);
      expect(find.text('https://evil.example/login'), findsOneWidget);
      await tester.tap(find.byKey(const Key('link-sheet-open')));
      await tester.pumpAndSettle();
      expect(opened, [Uri.parse('https://evil.example/login')]);
    });
  });
}
