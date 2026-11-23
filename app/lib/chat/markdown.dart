/// The one Markdown presentation for chat answers, agent messages and
/// returned reports (re-exported by `workspace_ui.dart`).
///
/// * Fenced code blocks get a header with the language and a Copy button,
///   comfortable padding, horizontal scroll, and subtle token-coloured
///   highlighting from [tokenizeCode].
/// * Links work: web and mail addresses open outside the app, anything else
///   is shown with Copy, and a link whose text names another site asks
///   first ([decideLink]).
/// * Images never fetch: see [markdownImageAllowed].
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:markdown/markdown.dart' as md;

import '../theme.dart';
import '../ui/kit.dart' show QuietAction, showSonderToast;
import 'links.dart';
import 'syntax_highlight.dart';

/// One Markdown owner for chat answers, agent messages and returned reports.
///
/// With [fullWidthCode] block content takes the full available (reading)
/// width; it needs a bounded width, so it is opt-in for callers that lay
/// the content out inside one. Code blocks always span the width they get.
class ConversationContent extends StatelessWidget {
  final String content;
  final Color? color;
  final bool fullWidthCode;
  const ConversationContent(
      {super.key,
      required this.content,
      this.color,
      this.fullWidthCode = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final theme = Theme.of(context);
    // Reading text: 16/26, the size and leading of the chat apps people
    // already read all day.
    final body =
        theme.textTheme.bodyLarge?.copyWith(color: color ?? tokens.text);
    TextStyle? heading(TextStyle? base) =>
        base?.copyWith(fontWeight: FontWeight.w600, color: tokens.text);
    return MarkdownBody(
      data: content,
      selectable: true,
      softLineBreak: true,
      fitContent: !fullWidthCode,
      // Model-authored Markdown never fetches: see [markdownImageAllowed].
      imageBuilder: (uri, title, alt) =>
          _MarkdownImage(uri: uri, alt: alt ?? title ?? ''),
      builders: <String, MarkdownElementBuilder>{'pre': _CodeBlockBuilder()},
      onTapLink: (text, href, title) =>
          unawaited(openMarkdownLink(context, text, href)),
      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
        p: body,
        strong: body?.copyWith(fontWeight: FontWeight.w600),
        h1: heading(theme.textTheme.headlineSmall),
        h1Padding: const EdgeInsets.only(top: SonderSpace.sm),
        h2: heading(theme.textTheme.titleLarge),
        h2Padding: const EdgeInsets.only(top: SonderSpace.sm),
        h3: heading(theme.textTheme.titleMedium),
        h3Padding: const EdgeInsets.only(top: SonderSpace.xs),
        h4: heading(theme.textTheme.titleSmall),
        em: body?.copyWith(fontStyle: FontStyle.italic),
        listBullet: body?.copyWith(color: tokens.text2),
        a: body?.copyWith(
            color: tokens.accentText,
            decoration: TextDecoration.underline,
            decorationColor: tokens.accentText.withValues(alpha: 0.5)),
        // Inline code only; blocks are drawn by [CodeBlockView].
        code: tokens
            .mono(14, color: tokens.text, height: 26)
            .copyWith(backgroundColor: tokens.bubble),
        // The frame around every fenced block; the header, body and scroll
        // live inside it.
        codeblockDecoration: BoxDecoration(
            color: tokens.panel,
            borderRadius: BorderRadius.circular(SonderRadius.menu),
            border: Border.all(color: tokens.hairline)),
        blockquote: body?.copyWith(color: tokens.text2),
        blockquoteDecoration: BoxDecoration(
            border: Border(
                left: BorderSide(color: tokens.hairlineStrong, width: 3))),
        blockquotePadding: const EdgeInsets.fromLTRB(
            SonderSpace.lg, SonderSpace.xxs, 0, SonderSpace.xxs),
        horizontalRuleDecoration: BoxDecoration(
            border: Border(top: BorderSide(color: tokens.hairline))),
        tableHead: body?.copyWith(fontWeight: FontWeight.w600),
        tableHeadAlign: TextAlign.left,
        tableBody: body,
        tableBorder: TableBorder.all(
          color: tokens.hairline,
          borderRadius: BorderRadius.circular(SonderRadius.row),
        ),
        tableHeadCellsDecoration: BoxDecoration(color: tokens.sunken),
        tableCellsPadding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.md, vertical: SonderSpace.sm),
        blockSpacing: SonderSpace.lg,
        // The marker column holds "10." at any text size.
        listIndent: MediaQuery.textScalerOf(context).scale(SonderSpace.xxl),
      ),
    );
  }
}

/// Replaces flutter_markdown's plain `pre` block with [CodeBlockView].
class _CodeBlockBuilder extends MarkdownElementBuilder {
  @override
  bool isBlockElement() => true;

  // The text is drawn by [CodeBlockView]; the default text widget is not
  // wanted inside the block.
  @override
  Widget? visitText(md.Text text, TextStyle? preferredStyle) => null;

  @override
  Widget? visitElementAfterWithContext(BuildContext context, md.Element element,
      TextStyle? preferredStyle, TextStyle? parentStyle) {
    var language = '';
    final children = element.children;
    if (children != null &&
        children.isNotEmpty &&
        children.first is md.Element) {
      language = codeLanguageOf(
          (children.first as md.Element).attributes['class'] ?? '');
    }
    // Markdown keeps the fence's final newline; the block does not show it.
    final code = element.textContent.replaceAll(RegExp(r'\n$'), '');
    return CodeBlockView(code: code, language: language);
  }
}

/// A fenced code block: a header with the language and Copy, then the code
/// in mono with subtle highlighting, scrolling sideways instead of
/// wrapping.
class CodeBlockView extends StatefulWidget {
  final String code;

  /// The fence's language (`cpp`, `python`), or empty.
  final String language;

  const CodeBlockView({super.key, required this.code, this.language = ''});

  @override
  State<CodeBlockView> createState() => _CodeBlockViewState();
}

class _CodeBlockViewState extends State<CodeBlockView> {
  final _scroll = ScrollController();
  late List<CodeToken> _tokens = tokenizeCode(widget.code, widget.language);
  bool _copied = false;
  Timer? _reset;

  @override
  void didUpdateWidget(covariant CodeBlockView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.code != widget.code ||
        oldWidget.language != widget.language) {
      _tokens = tokenizeCode(widget.code, widget.language);
    }
  }

  @override
  void dispose() {
    _reset?.cancel();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.code));
    if (!mounted) return;
    setState(() => _copied = true);
    showSonderToast(context, 'Code copied');
    _reset?.cancel();
    _reset = Timer(const Duration(milliseconds: 1600), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  static Color _colorOf(CodeTokenKind kind, SonderTokens tokens) =>
      switch (kind) {
        CodeTokenKind.keyword => tokens.auto,
        CodeTokenKind.string => tokens.ok,
        CodeTokenKind.number => tokens.mutation,
        CodeTokenKind.comment => tokens.muted,
        CodeTokenKind.plain => tokens.text,
      };

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final base = tokens.mono(13, color: tokens.text, height: 20);
    final spans = <TextSpan>[
      for (final token in _tokens)
        TextSpan(
          text: token.text,
          style: token.kind == CodeTokenKind.plain
              ? null
              : TextStyle(color: _colorOf(token.kind, tokens)),
        ),
    ];
    final label = widget.language;
    return Semantics(
      container: true,
      label: label.isEmpty ? 'Code' : 'Code, $label',
      child: Stack(
        children: [
          Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Container(
                height: SonderSpace.x4,
                // Room on the right for the copy control: two 48px targets.
                padding: const EdgeInsets.only(
                    left: SonderSpace.lg, right: SonderSpace.x5 + SonderSpace.x5),
                alignment: Alignment.centerLeft,
                // The header bar: one tone below the code, inside the
                // frame's top corners.
                decoration: BoxDecoration(
                  color: tokens.sunken,
                  borderRadius: const BorderRadius.vertical(
                      top: Radius.circular(SonderRadius.menu - 1)),
                ),
                child: ExcludeSemantics(
                  child: Text(label,
                      key: const Key('code-block-language'),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: tokens.mono(12, color: tokens.text2)),
                ),
              ),
              Scrollbar(
                controller: _scroll,
                child: SingleChildScrollView(
                  controller: _scroll,
                  scrollDirection: Axis.horizontal,
                  padding: const EdgeInsets.fromLTRB(SonderSpace.lg,
                      SonderSpace.md, SonderSpace.lg, SonderSpace.lg),
                  child: SelectableText.rich(
                    TextSpan(style: base, children: spans),
                    key: const Key('code-block-text'),
                  ),
                ),
              ),
            ],
          ),
          Positioned(
            top: 0,
            right: SonderSpace.xs,
            child: QuietAction(
              key: const Key('code-block-copy'),
              icon: _copied ? Icons.check : Icons.copy_outlined,
              label: _copied ? 'Copied' : 'Copy',
              semanticLabel: _copied ? 'Code copied' : 'Copy code',
              selected: _copied,
              // The 48 dp target hangs below the 40 dp header; the pill
              // sits on the header's centre line.
              alignment: const Alignment(0, -0.5),
              onPressed: _copy,
            ),
          ),
        ],
      ),
    );
  }
}

/// Largest inline `data:` image rendered from Markdown, in bytes.
const markdownDataImageMaxBytes = 2 * 1024 * 1024;

/// Whether a Markdown image at [uri] may be rendered.
///
/// Markdown in this app is written by a model (and by whatever a tool fed
/// it), so an image must not cause a request: an `http(s)` image is a
/// beacon that leaks conversation data, and on Windows a `file://host/…` or
/// UNC path opens an SMB connection that sends the user's credentials. Only
/// a bounded inline `data:image/…` URI is rendered, from memory; everything
/// else is shown as a placeholder naming the URL.
bool markdownImageAllowed(Uri uri) {
  if (uri.scheme != 'data') return false;
  final data = uri.data;
  if (data == null || !data.mimeType.startsWith('image/')) return false;
  // Base64 expands 3 bytes to 4 characters; bound before decoding.
  return uri.toString().length <= markdownDataImageMaxBytes * 4 ~/ 3 + 256;
}

/// A Markdown image: a `data:` image from memory, or a placeholder.
class _MarkdownImage extends StatefulWidget {
  final Uri uri;
  final String alt;
  const _MarkdownImage({required this.uri, required this.alt});

  @override
  State<_MarkdownImage> createState() => _MarkdownImageState();
}

class _MarkdownImageState extends State<_MarkdownImage> {
  bool _copied = false;
  Timer? _reset;

  @override
  void dispose() {
    _reset?.cancel();
    super.dispose();
  }

  Future<void> _copyMarkdown() async {
    final label = widget.alt.isEmpty ? 'Image' : widget.alt;
    final snippet = '![$label](${widget.uri})';
    try {
      await Clipboard.setData(ClipboardData(text: snippet));
    } catch (_) {
      return;
    }
    if (!mounted) return;
    setState(() => _copied = true);
    showSonderToast(context, 'Image copied for chat paste');
    _reset?.cancel();
    _reset = Timer(const Duration(milliseconds: 1600), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  void _openLightbox(BuildContext context, Uint8List bytes) {
    final tokens = SonderTokens.of(context);
    showDialog<void>(
      context: context,
      builder: (dialogContext) => Dialog(
        backgroundColor: tokens.panel,
        insetPadding: const EdgeInsets.all(SonderSpace.lg),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(SonderRadius.menu),
          side: BorderSide(color: tokens.hairline),
        ),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 960, maxHeight: 760),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(
                    SonderSpace.md, SonderSpace.xs, SonderSpace.xs, SonderSpace.xs),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        widget.alt.isEmpty ? 'Generated image' : widget.alt,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: Theme.of(dialogContext).textTheme.titleSmall,
                      ),
                    ),
                    IconButton(
                      tooltip: 'Close',
                      icon: const Icon(Icons.close, size: 18),
                      onPressed: () => Navigator.of(dialogContext).pop(),
                    ),
                  ],
                ),
              ),
              Divider(height: 1, color: tokens.hairline),
              Flexible(
                child: InteractiveViewer(
                  minScale: 0.5,
                  maxScale: 5.0,
                  child: Center(
                    child: Padding(
                      padding: const EdgeInsets.all(SonderSpace.md),
                      child: Image.memory(
                        bytes,
                        fit: BoxFit.contain,
                        semanticLabel: widget.alt.isEmpty ? null : widget.alt,
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (markdownImageAllowed(widget.uri)) {
      try {
        final bytes = widget.uri.data!.contentAsBytes();
        if (bytes.length <= markdownDataImageMaxBytes) {
          final tokens = SonderTokens.of(context);
          return Container(
            key: const Key('markdown-image-card'),
            margin: const EdgeInsets.symmetric(vertical: SonderSpace.xs),
            decoration: BoxDecoration(
              color: tokens.panel,
              borderRadius: BorderRadius.circular(SonderRadius.menu),
              border: Border.all(color: tokens.hairline),
            ),
            clipBehavior: Clip.antiAlias,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                GestureDetector(
                  onTap: () => _openLightbox(context, bytes),
                  child: MouseRegion(
                    cursor: SystemMouseCursors.click,
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(
                        maxWidth: 520,
                        maxHeight: 460,
                      ),
                      child: Image.memory(
                        bytes,
                        fit: BoxFit.contain,
                        semanticLabel: widget.alt.isEmpty ? null : widget.alt,
                        errorBuilder: (context, _, __) => _placeholder(context),
                      ),
                    ),
                  ),
                ),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: SonderSpace.sm,
                    vertical: SonderSpace.xxs,
                  ),
                  decoration: BoxDecoration(
                    color: tokens.sunken,
                    border: Border(top: BorderSide(color: tokens.hairline)),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (widget.alt.isNotEmpty)
                        Flexible(
                          child: Padding(
                            padding: const EdgeInsets.only(right: SonderSpace.sm),
                            child: Text(
                              widget.alt,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: Theme.of(context)
                                  .textTheme
                                  .labelSmall
                                  ?.copyWith(color: tokens.muted),
                            ),
                          ),
                        ),
                      QuietAction(
                        key: const Key('markdown-image-zoom'),
                        icon: Icons.open_in_full,
                        label: 'Zoom',
                        semanticLabel: 'Zoom image',
                        onPressed: () => _openLightbox(context, bytes),
                      ),
                      QuietAction(
                        key: const Key('markdown-image-copy'),
                        icon: _copied ? Icons.check : Icons.copy_outlined,
                        label: _copied ? 'Copied' : 'Copy',
                        semanticLabel:
                            _copied ? 'Image copied' : 'Copy image for chat paste',
                        selected: _copied,
                        onPressed: () => unawaited(_copyMarkdown()),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          );
        }
      } on FormatException {
        // Malformed data URI: fall through to the placeholder.
      }
    }
    return _placeholder(context);
  }

  Widget _placeholder(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final shown = widget.uri.scheme == 'data'
        ? 'inline data'
        : (widget.uri.toString().length > 200
            ? '${widget.uri.toString().substring(0, 200)}…'
            : widget.uri.toString());
    final label =
        widget.alt.isEmpty ? 'Image not loaded' : 'Image not loaded: ${widget.alt}';
    return Container(
      key: const Key('markdown-image-blocked'),
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.sm, vertical: SonderSpace.xs),
      decoration: BoxDecoration(
          color: tokens.raised,
          borderRadius: BorderRadius.circular(SonderRadius.row),
          border: Border.all(color: tokens.hairline)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(Icons.image_not_supported_outlined, size: 16, color: tokens.muted),
        const SizedBox(width: SonderSpace.sm),
        Flexible(
          child: Text.rich(
            TextSpan(children: [
              TextSpan(text: label),
              TextSpan(
                  text: '  $shown',
                  style: tokens.mono(12, color: tokens.muted)),
            ]),
            style: Theme.of(context).textTheme.bodySmall,
            semanticsLabel: '$label. $shown',
          ),
        ),
      ]),
    );
  }
}
