import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart' as launcher;

import '../theme.dart';
import '../ui/kit.dart' show showSonderToast;
import '../ui/sheet.dart' show showSonderSheet;
import '../ui/status_vocab.dart';

/// Opens [uri] outside the app; true when something handled it. Tests (and
/// a platform without a browser) replace it.
typedef ExternalLinkOpener = Future<bool> Function(Uri uri);

Future<bool> _launchExternal(Uri uri) =>
    launcher.launchUrl(uri, mode: launcher.LaunchMode.externalApplication);

/// The process-wide link opener. Tests swap in a recorder.
ExternalLinkOpener externalLinkOpener = _launchExternal;

/// What tapping a Markdown link does.
enum LinkDecision {
  /// A web or mail address: open it in the browser or mail app.
  open,

  /// A web address whose visible text names a different site, or whose host
  /// hides behind credentials or look-alike characters: show the real
  /// address first and let the person decide.
  confirm,

  /// Anything else (a file path, a custom app scheme, a relative link):
  /// never opened from model-written text. The address is shown with Copy.
  show,
}

const _openableSchemes = {'http', 'https', 'mailto'};

/// The domain [text] names, when the visible text of a link looks like an
/// address (`docs.python.org`, `https://example.com/a`); otherwise null.
String? _namedHost(String text) {
  final t = text.trim().toLowerCase();
  if (t.isEmpty || t.contains(' ') || !t.contains('.')) return null;
  final uri = Uri.tryParse(t.contains('://') ? t : 'https://$t');
  final host = uri?.host ?? '';
  if (!RegExp(r'^[a-z0-9.-]+\.[a-z]{2,}$').hasMatch(host)) return null;
  return host.startsWith('www.') ? host.substring(4) : host;
}

bool _sameSite(String a, String b) =>
    a == b || a.endsWith('.$b') || b.endsWith('.$a');

/// Decide what a tap on a link with visible [text] and target [href] does.
///
/// Markdown here is written by a model, and whatever a tool fed that model
/// can steer what it writes, so a link is treated as a suggestion: only
/// web and mail addresses open directly, and only when the visible text
/// does not claim to be somewhere else.
LinkDecision decideLink(String text, String? href) {
  final uri = Uri.tryParse((href ?? '').trim());
  if (uri == null || !_openableSchemes.contains(uri.scheme.toLowerCase())) {
    return LinkDecision.show;
  }
  if (uri.scheme.toLowerCase() == 'mailto') {
    return uri.path.trim().isEmpty ? LinkDecision.show : LinkDecision.open;
  }
  final host = uri.host.toLowerCase();
  if (host.isEmpty) return LinkDecision.show;
  // `https://bank.example@evil.example` and look-alike (IDN) hosts.
  final lookAlike = host.contains('xn--') || host.runes.any((r) => r > 0x7F);
  if (uri.userInfo.isNotEmpty || lookAlike) return LinkDecision.confirm;
  final named = _namedHost(text);
  final bare = host.startsWith('www.') ? host.substring(4) : host;
  if (named != null && !_sameSite(named, bare)) return LinkDecision.confirm;
  return LinkDecision.open;
}

/// Handle a tap on a Markdown link: open it, or show the address first (see
/// [decideLink]). Failures to open fall back to showing the address.
Future<void> openMarkdownLink(
    BuildContext context, String text, String? href) async {
  final raw = (href ?? '').trim();
  final decision = decideLink(text, href);
  if (decision == LinkDecision.open) {
    final uri = Uri.parse(raw);
    var opened = false;
    try {
      opened = await externalLinkOpener(uri);
    } catch (_) {
      opened = false;
    }
    if (opened || !context.mounted) return;
    await _showLinkSheet(context, raw, reason: _LinkReason.failed);
    return;
  }
  await _showLinkSheet(context, raw,
      reason: decision == LinkDecision.confirm
          ? _LinkReason.confirm
          : _LinkReason.notWeb);
}

enum _LinkReason { confirm, notWeb, failed }

/// The address of a link, selectable, with Copy link and (for a web
/// address) Open link.
Future<void> _showLinkSheet(BuildContext context, String href,
    {_LinkReason reason = _LinkReason.confirm}) {
  final outer = context;
  return showSonderSheet<void>(
    context,
    builder: (sheetContext) => _LinkSheet(
      href: href,
      reason: reason,
      onCopy: () async {
        await Clipboard.setData(ClipboardData(text: href));
        if (sheetContext.mounted) Navigator.of(sheetContext).pop();
        if (outer.mounted) showSonderToast(outer, 'Link copied');
      },
      onOpen: reason == _LinkReason.confirm
          ? () async {
              Navigator.of(sheetContext).pop();
              var opened = false;
              try {
                opened = await externalLinkOpener(Uri.parse(href));
              } catch (_) {
                opened = false;
              }
              if (!opened && outer.mounted) {
                showSonderToast(outer, "Couldn't open the link",
                    kind: StatusKind.fail);
              }
            }
          : null,
    ),
  );
}

class _LinkSheet extends StatelessWidget {
  final String href;
  final _LinkReason reason;
  final Future<void> Function() onCopy;
  final VoidCallback? onOpen;

  const _LinkSheet({
    required this.href,
    required this.reason,
    required this.onCopy,
    this.onOpen,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final (title, body) = switch (reason) {
      _LinkReason.confirm => (
          'Open this link?',
          'The link text names a different address. Check where it goes '
              'before opening it.',
        ),
      _LinkReason.notWeb => (
          'Link not opened',
          "This isn't a web address, so Sonder won't open it from a reply. "
              'Copy it if you want to use it.',
        ),
      _LinkReason.failed => (
          "Couldn't open the link",
          'No app on this device opened it. Copy the address instead.',
        ),
    };
    return Semantics(
      key: const Key('link-sheet'),
      scopesRoute: true,
      namesRoute: true,
      explicitChildNodes: true,
      label: title,
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.xl, SonderSpace.lg, SonderSpace.xl, SonderSpace.lg),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(title, style: text.titleMedium),
            const SizedBox(height: SonderSpace.sm),
            Text(body, style: text.bodyMedium?.copyWith(color: tokens.text2)),
            const SizedBox(height: SonderSpace.lg),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.symmetric(
                  horizontal: SonderSpace.md, vertical: SonderSpace.sm),
              decoration: BoxDecoration(
                color: tokens.canvas,
                borderRadius: BorderRadius.circular(SonderRadius.row),
                border: Border.all(color: tokens.hairline),
              ),
              child: SelectableText(
                href.isEmpty ? '(empty link)' : href,
                key: const Key('link-sheet-address'),
                style: tokens.mono(12.5, color: tokens.text),
              ),
            ),
            const SizedBox(height: SonderSpace.xl),
            Align(
              alignment: Alignment.centerRight,
              child: Wrap(
                alignment: WrapAlignment.end,
                spacing: SonderSpace.sm,
                runSpacing: SonderSpace.sm,
                children: [
                  TextButton(
                    onPressed: () => Navigator.of(context).pop(),
                    child: Text(onOpen == null ? 'Close' : 'Cancel'),
                  ),
                  OutlinedButton.icon(
                    key: const Key('link-sheet-copy'),
                    onPressed: href.isEmpty ? null : onCopy,
                    icon: const Icon(Icons.copy_outlined, size: 16),
                    label: const Text('Copy link'),
                  ),
                  if (onOpen != null)
                    FilledButton.icon(
                      key: const Key('link-sheet-open'),
                      onPressed: onOpen,
                      icon: const Icon(Icons.open_in_new, size: 16),
                      label: const Text('Open link'),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
