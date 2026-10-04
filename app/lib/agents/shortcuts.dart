/// The Agents keyboard guide. Shortcuts are documented here rather than
/// left to discoverability by accident (UX-CONTRACT.md).
library;

import 'package:flutter/material.dart';

import '../theme.dart';

class _Keys extends StatelessWidget {
  final String keys;
  const _Keys(this.keys);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.sm, vertical: SonderSpace.xxs),
      decoration: BoxDecoration(
        color: tokens.raised,
        borderRadius: BorderRadius.circular(SonderRadius.control),
        border: Border.all(color: tokens.hairline),
      ),
      child: Text(keys, style: tokens.mono(12, color: tokens.text)),
    );
  }
}

class _ShortcutRow extends StatelessWidget {
  final String keys;
  final String action;
  const _ShortcutRow(this.keys, this.action);

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    // AlertDialog measures its content intrinsically, so the stacking
    // decision reads the window width rather than a LayoutBuilder.
    final stacked = MediaQuery.sizeOf(context).width < 560;
    final keys = _Keys(this.keys);
    final label = Text(action, style: text.bodyMedium);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: SonderSpace.sm),
      child: stacked
          ? Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [keys, const SizedBox(height: SonderSpace.xs), label],
            )
          : Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              SizedBox(
                width: 224,
                child: Align(alignment: Alignment.centerLeft, child: keys),
              ),
              const SizedBox(width: SonderSpace.md),
              Expanded(child: label),
            ]),
    );
  }
}

Future<void> showAgentShortcuts(BuildContext context) => showDialog<void>(
      context: context,
      builder: (context) {
        final tokens = SonderTokens.of(context);
        final text = Theme.of(context).textTheme;
        return AlertDialog(
          title: const Text('Agent conversation shortcuts'),
          content: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 520),
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const _ShortcutRow('Ctrl+Enter / Cmd+Enter',
                      'Send the follow-up in the composer'),
                  const _ShortcutRow('Ctrl+Shift+F / Cmd+Shift+F',
                      'Focus conversation search'),
                  const _ShortcutRow('Alt+↑ / Alt+↓',
                      'Move to the previous or next loaded conversation'),
                  const _ShortcutRow('Escape',
                      'Clear focused search, or return to the list on narrow screens'),
                  const SizedBox(height: SonderSpace.md),
                  Text(
                    'Enter adds a new line. A request is sent only after the '
                    'server confirms the same command.',
                    style: text.bodySmall?.copyWith(color: tokens.text2),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              autofocus: true,
              onPressed: () => Navigator.pop(context),
              child: const Text('Close'),
            ),
          ],
        );
      },
    );
