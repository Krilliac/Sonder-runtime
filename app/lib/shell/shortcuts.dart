import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme.dart';
import '../workspace_ui.dart';

class NewChatIntent extends Intent {
  const NewChatIntent();
}

class OpenCommandsIntent extends Intent {
  const OpenCommandsIntent();
}

class SearchChatsIntent extends Intent {
  const SearchChatsIntent();
}

class GoToDestinationIntent extends Intent {
  final WorkspaceDestination destination;
  const GoToDestinationIntent(this.destination);
}

class ToggleSidebarIntent extends Intent {
  const ToggleSidebarIntent();
}

class ShowShortcutsIntent extends Intent {
  const ShowShortcutsIntent();
}

/// Whether the platform's primary shortcut modifier is ⌘ rather than Ctrl.
bool usesCommandKey(TargetPlatform platform) =>
    platform == TargetPlatform.macOS || platform == TargetPlatform.iOS;

/// Ctrl+[key] on Windows, Linux and Android; ⌘+[key] on Apple platforms.
SingleActivator primaryShortcut(LogicalKeyboardKey key, TargetPlatform platform,
    {bool shift = false}) {
  final mac = usesCommandKey(platform);
  return SingleActivator(key, control: !mac, meta: mac, shift: shift);
}

/// The app-wide shortcuts, installed by the shell around every page. Mode
/// cycling (Shift+Tab) is not here: it belongs to the composer, so reverse
/// focus traversal keeps working everywhere else.
Map<ShortcutActivator, Intent> shellShortcuts(TargetPlatform platform) {
  SingleActivator primary(LogicalKeyboardKey key, {bool shift = false}) =>
      primaryShortcut(key, platform, shift: shift);
  return <ShortcutActivator, Intent>{
    primary(LogicalKeyboardKey.keyN): const NewChatIntent(),
    primary(LogicalKeyboardKey.keyK): const OpenCommandsIntent(),
    primary(LogicalKeyboardKey.keyP): const SearchChatsIntent(),
    primary(LogicalKeyboardKey.comma):
        const GoToDestinationIntent(WorkspaceDestination.settings),
    primary(LogicalKeyboardKey.digit1):
        const GoToDestinationIntent(WorkspaceDestination.chat),
    primary(LogicalKeyboardKey.digit2):
        const GoToDestinationIntent(WorkspaceDestination.agents),
    primary(LogicalKeyboardKey.digit3):
        const GoToDestinationIntent(WorkspaceDestination.runtime),
    primary(LogicalKeyboardKey.digit4):
        const GoToDestinationIntent(WorkspaceDestination.settings),
    // The older Runtime shortcut, kept as an alias and listed in the guide.
    primary(LogicalKeyboardKey.keyD):
        const GoToDestinationIntent(WorkspaceDestination.runtime),
    primary(LogicalKeyboardKey.keyB): const ToggleSidebarIntent(),
    primary(LogicalKeyboardKey.slash): const ShowShortcutsIntent(),
    // Ctrl+? where "/" needs Shift.
    primary(LogicalKeyboardKey.slash, shift: true): const ShowShortcutsIntent(),
  };
}

/// The number shortcut for [destination] (Ctrl+1 … Ctrl+4).
int destinationNumber(WorkspaceDestination destination) =>
    WorkspaceDestination.values.indexOf(destination) + 1;

/// Key names as the platform spells them.
class ShortcutKeys {
  final TargetPlatform platform;
  const ShortcutKeys(this.platform);

  bool get _mac => usesCommandKey(platform);
  String get primary => _mac ? '⌘' : 'Ctrl';
  String get shift => _mac ? '⇧' : 'Shift';
  String get alt => _mac ? '⌥' : 'Alt';
  String get enter => _mac ? 'Return' : 'Enter';

  /// "Ctrl+N" / "⌘N", for tooltips.
  String combo(String key) => _mac ? '$primary$key' : '$primary+$key';
}

/// One line of the guide: the keys, then what they do.
class _ShortcutEntry {
  final List<List<String>> keys;
  final String action;
  final String? note;

  /// The combos are the ends of a range (Ctrl+1 to Ctrl+4), not
  /// alternatives.
  final bool range;
  const _ShortcutEntry(this.keys, this.action, {this.note, this.range = false});
}

List<(String, List<_ShortcutEntry>)> _guide(ShortcutKeys k) => [
      (
        'Anywhere',
        [
          _ShortcutEntry([
            [k.primary, 'N']
          ], 'New chat'),
          _ShortcutEntry([
            [k.primary, 'P']
          ], 'Search chats'),
          _ShortcutEntry([
            [k.primary, 'K']
          ], 'Browse commands'),
          _ShortcutEntry([
            [k.primary, '1'],
            [k.primary, '4'],
          ], 'Chat, Agents, Runtime, Settings',
              note: 'in sidebar order', range: true),
          _ShortcutEntry([
            [k.primary, ',']
          ], 'Settings'),
          _ShortcutEntry([
            [k.primary, 'D']
          ], 'Runtime', note: 'same as ${k.combo('3')}'),
          _ShortcutEntry([
            [k.primary, 'B']
          ], 'Show or hide the sidebar'),
          _ShortcutEntry([
            [k.primary, '/']
          ], 'Show keyboard shortcuts'),
        ],
      ),
      (
        'Composer',
        [
          _ShortcutEntry([
            [k.enter]
          ], 'Send'),
          _ShortcutEntry([
            [k.shift, k.enter]
          ], 'New line'),
          _ShortcutEntry([
            [k.shift, 'Tab']
          ], 'Next permission mode', note: 'raising it asks first'),
          const _ShortcutEntry([
            ['↑'],
            ['↓'],
          ], 'Move in the command list'),
          const _ShortcutEntry([
            ['Esc']
          ], 'Close the command list'),
        ],
      ),
      (
        'Agents',
        [
          _ShortcutEntry([
            [k.primary, k.enter]
          ], 'Send a follow-up'),
          _ShortcutEntry([
            [k.primary, k.shift, 'F']
          ], 'Find a conversation'),
          _ShortcutEntry([
            [k.alt, '↑'],
            [k.alt, '↓'],
          ], 'Previous or next conversation'),
        ],
      ),
    ];

/// Shows every keyboard shortcut, grouped by where it works.
Future<void> showShortcutGuide(BuildContext context) {
  final keys = ShortcutKeys(Theme.of(context).platform);
  return showDialog<void>(
    context: context,
    builder: (context) => _ShortcutGuide(keys: keys),
  );
}

class _ShortcutGuide extends StatelessWidget {
  final ShortcutKeys keys;
  const _ShortcutGuide({required this.keys});

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final tokens = SonderTokens.of(context);
    final groups = _guide(keys);
    return Dialog(
      key: const Key('shortcut-guide'),
      insetPadding: const EdgeInsets.all(SonderSpace.xxl),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 560, maxHeight: 640),
        child: Semantics(
          scopesRoute: true,
          namesRoute: true,
          explicitChildNodes: true,
          label: 'Keyboard shortcuts',
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(SonderSpace.xxl,
                    SonderSpace.lg, SonderSpace.sm, SonderSpace.xs),
                child: Row(children: [
                  Expanded(
                    child: Text('Keyboard shortcuts',
                        style: text.titleMedium
                            ?.copyWith(fontWeight: FontWeight.w600)),
                  ),
                  IconButton(
                    tooltip: 'Close',
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ]),
              ),
              Flexible(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(
                      SonderSpace.xxl, 0, SonderSpace.xxl, SonderSpace.xxl),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      for (final (title, entries) in groups) ...[
                        Padding(
                          padding: const EdgeInsets.only(
                              top: SonderSpace.lg, bottom: SonderSpace.xs),
                          child:
                              Text(title.toUpperCase(), style: text.labelSmall),
                        ),
                        for (var i = 0; i < entries.length; i++) ...[
                          if (i > 0) Divider(height: 1, color: tokens.hairline),
                          _ShortcutLine(entry: entries[i]),
                        ],
                      ],
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ShortcutLine extends StatelessWidget {
  final _ShortcutEntry entry;
  const _ShortcutLine({required this.entry});

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final tokens = SonderTokens.of(context);
    final spoken = [
      for (final combo in entry.keys) combo.join(' '),
    ].join(entry.range ? ' to ' : ' or ');
    return Semantics(
      label: '${entry.action}: $spoken',
      excludeSemantics: true,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 44),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: SonderSpace.sm),
          child: Row(children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(entry.action, style: text.bodyMedium),
                  if (entry.note != null)
                    Text(entry.note!,
                        style: text.bodySmall?.copyWith(color: tokens.muted)),
                ],
              ),
            ),
            const SizedBox(width: SonderSpace.lg),
            Wrap(
              spacing: SonderSpace.xs,
              runSpacing: SonderSpace.xs,
              crossAxisAlignment: WrapCrossAlignment.center,
              children: [
                for (var c = 0; c < entry.keys.length; c++) ...[
                  if (c > 0)
                    Padding(
                      padding: const EdgeInsets.symmetric(
                          horizontal: SonderSpace.xxs),
                      child:
                          Text(entry.range ? '–' : '/', style: text.bodySmall),
                    ),
                  for (final key in entry.keys[c]) KeyCap(key),
                ],
              ],
            ),
          ]),
        ),
      ),
    );
  }
}

/// One key drawn as a small cap ("Ctrl", "N", "⌘").
class KeyCap extends StatelessWidget {
  final String label;
  const KeyCap(this.label, {super.key});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Container(
      constraints: const BoxConstraints(minWidth: 24),
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.sm, vertical: SonderSpace.xxs),
      decoration: BoxDecoration(
        color: tokens.raised,
        borderRadius: BorderRadius.circular(SonderRadius.control),
        border: Border.all(color: tokens.hairlineStrong),
      ),
      child: Text(label,
          textAlign: TextAlign.center,
          style: tokens.mono(12, color: tokens.text, weight: FontWeight.w500)),
    );
  }
}
