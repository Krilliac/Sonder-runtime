import 'package:flutter/material.dart';

import '../api.dart';
import '../safety_colors.dart';
import '../theme.dart';
import '../workspace_ui.dart' show conversationWidth;

// ---------------------------------------------------------------------------
// Slash intercepts (P0-5)
// ---------------------------------------------------------------------------

/// A composer line that must not be sent as a chat message.
sealed class ComposerIntercept {
  const ComposerIntercept();
}

/// `/login`, `/register`, `/admin_login`: a password must never enter the
/// transcript (it would be stored and re-sent as history on every turn), so
/// these open Settings > Account instead.
class AccountIntercept extends ComposerIntercept {
  final String command;

  /// The first argument, when it looks like a user name. Never the password.
  final String username;
  const AccountIntercept(this.command, {this.username = ''});
}

/// `/mode`, `/permission_mode`, `/permissions <mode>`, `/elevate`: the chat
/// route is unattended and refuses a raise, so these go to the chip flow
/// (P0-4), where the app itself is the attended surface.
class ModeIntercept extends ComposerIntercept {
  /// The mode asked for, or null to open the picker.
  final String? target;
  const ModeIntercept(this.target);
}

const _accountCommands = {'/login', '/register', '/admin_login'};
const _modeCommands = {'/mode', '/permission_mode'};

/// Canonical mode name for a typed one, or null.
String? canonicalMode(String typed) => switch (typed.toLowerCase()) {
      'plan' => 'plan',
      'manual' || 'default' => 'manual',
      'acceptedits' ||
      'accept-edits' ||
      'accept_edits' ||
      'edits' =>
        'acceptEdits',
      'auto' => 'auto',
      _ => null,
    };

/// Classify a composer line; null means "send it".
ComposerIntercept? classifyIntercept(String text) {
  final trimmed = text.trim();
  if (!trimmed.startsWith('/')) return null;
  final parts = trimmed.split(RegExp(r'\s+'));
  final command = parts.first.toLowerCase();
  final args = parts.skip(1).toList();
  if (_accountCommands.contains(command)) {
    final user = args.isEmpty ? '' : args.first;
    return AccountIntercept(command,
        username:
            RegExp(r'^[A-Za-z0-9_.@-]{1,64}$').hasMatch(user) ? user : '');
  }
  if (_modeCommands.contains(command)) {
    return ModeIntercept(args.isEmpty ? null : canonicalMode(args.first));
  }
  if ((command == '/permissions' || command == '/perms') && args.isNotEmpty) {
    final mode = canonicalMode(args.first);
    if (mode != null) return ModeIntercept(mode);
  }
  if (command == '/elevate') return const ModeIntercept(null);
  return null;
}

/// The palette's trailing label for commands the composer intercepts.
String? interceptLabel(String commandName) {
  final name = commandName.split(' ').first.toLowerCase();
  if (_accountCommands.contains(name)) return 'opens Settings';
  if (_modeCommands.contains(name) || name == '/elevate') return 'opens mode';
  return null;
}

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------

class ChatComposer extends StatelessWidget {
  final TextEditingController controller;
  final FocusNode focusNode;
  final bool sending;
  final VoidCallback onSend;
  final VoidCallback onCancel;
  final List<SonderCommand> paletteMatches;
  final int paletteSelected;
  final bool paletteGrouped;
  final Map<String, String> paletteCategories;
  final ValueChanged<String> onPalettePick;
  final KeyEventResult Function(KeyEvent) onKey;
  final VoidCallback onOpenCommands;
  final bool desktop;

  /// The mode chip (or its offline stand-in), built by the shell; null when
  /// the server publishes no mode.
  final Widget? modeChip;

  const ChatComposer({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.sending,
    required this.onSend,
    required this.onCancel,
    required this.paletteMatches,
    required this.paletteSelected,
    required this.paletteGrouped,
    required this.paletteCategories,
    required this.onPalettePick,
    required this.onKey,
    required this.onOpenCommands,
    this.modeChip,
    this.desktop = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return SafeArea(
      top: false,
      bottom: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: conversationWidth),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (paletteMatches.isNotEmpty)
                  CommandPalette(
                    matches: paletteMatches,
                    selected: paletteSelected,
                    grouped: paletteGrouped,
                    categories: paletteCategories,
                    onPick: onPalettePick,
                  ),
                Container(
                  decoration: BoxDecoration(
                    color: tokens.panel,
                    borderRadius: BorderRadius.circular(SonderRadius.sheet),
                    border: Border.all(color: tokens.hairlineStrong),
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Focus(
                        onKeyEvent: (node, event) => onKey(event),
                        child: TextField(
                          controller: controller,
                          focusNode: focusNode,
                          minLines: 1,
                          maxLines: 6,
                          textInputAction: TextInputAction.send,
                          onSubmitted: (_) => onSend(),
                          style: Theme.of(context).textTheme.bodyMedium,
                          decoration: const InputDecoration(
                            hintText: 'Ask Sonder…',
                            filled: false,
                            contentPadding: EdgeInsets.fromLTRB(14, 12, 14, 6),
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                          ),
                        ),
                      ),
                      Padding(
                        padding: const EdgeInsets.fromLTRB(8, 0, 8, 4),
                        child: LayoutBuilder(builder: (context, row) {
                          // The chip may take what the "/" and Send targets
                          // leave, and truncates its label past that.
                          final chipMax = (row.maxWidth - 48 - 48 - 12)
                              .clamp(48.0, double.infinity);
                          return Row(
                            children: [
                              if (modeChip != null) ...[
                                ConstrainedBox(
                                  constraints:
                                      BoxConstraints(maxWidth: chipMax),
                                  child: modeChip!,
                                ),
                                const SizedBox(width: 6),
                              ],
                              _CommandsButton(
                                  onTap: onOpenCommands, desktop: desktop),
                              Expanded(
                                child: desktop
                                    ? Padding(
                                        padding: const EdgeInsets.only(
                                            left: 8, right: 10),
                                        child: Text(
                                          'Enter send · Shift Enter newline',
                                          textAlign: TextAlign.right,
                                          maxLines: 1,
                                          overflow: TextOverflow.ellipsis,
                                          style: tokens.mono(11,
                                              color: tokens.muted),
                                        ),
                                      )
                                    : const SizedBox.shrink(),
                              ),
                              SizedBox(
                                width: 48,
                                height: 48,
                                child: Center(
                                  child: SizedBox(
                                    width: 36,
                                    height: 36,
                                    child: FloatingActionButton.small(
                                      key: const Key('composer-send'),
                                      heroTag: null,
                                      onPressed: sending ? onCancel : onSend,
                                      tooltip: sending ? 'Stop' : 'Send',
                                      child: sending
                                          ? const Icon(Icons.stop, size: 18)
                                          : const Icon(Icons.arrow_upward,
                                              size: 18),
                                    ),
                                  ),
                                ),
                              ),
                            ],
                          );
                        }),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _CommandsButton extends StatelessWidget {
  final VoidCallback onTap;
  final bool desktop;
  const _CommandsButton({required this.onTap, required this.desktop});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Tooltip(
      message: 'Commands (Ctrl+K)',
      child: Semantics(
        button: true,
        label: 'Commands',
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(SonderRadius.pill),
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 48, minWidth: 48),
            child: Center(
              widthFactor: 1,
              child: Container(
                height: 28,
                padding: const EdgeInsets.symmetric(horizontal: 10),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(SonderRadius.pill),
                  border: Border.all(color: tokens.hairlineStrong),
                ),
                child: ExcludeSemantics(
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text('/', style: tokens.mono(12, color: tokens.text2)),
                      if (desktop) ...[
                        const SizedBox(width: 6),
                        Text('commands',
                            style: tokens.mono(11, color: tokens.muted)),
                      ],
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Palette and browser
// ---------------------------------------------------------------------------

class _RiskDot extends StatelessWidget {
  final String risk;
  const _RiskDot({required this.risk});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final label = riskLabel(risk);
    return Tooltip(
      message: label,
      child: Semantics(
        label: 'Risk: $label',
        container: true,
        child: SizedBox(
          width: 24,
          height: 24,
          child: Center(
            child: Container(
              width: 9,
              height: 9,
              decoration: BoxDecoration(
                  color: riskColor(cs, risk), shape: BoxShape.circle),
            ),
          ),
        ),
      ),
    );
  }
}

class _CategoryTag extends StatelessWidget {
  final String category;
  const _CategoryTag({required this.category});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: Text(category,
          style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant)),
    );
  }
}

/// One command in the palette and the browser: risk dot, name, category,
/// summary, usage line, and — for intercepted commands — where it goes.
class CommandRow extends StatelessWidget {
  final SonderCommand command;
  final bool selected;
  final VoidCallback onTap;

  const CommandRow({
    super.key,
    required this.command,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final usage = command.usageLine;
    final aliases = command.aliases.where((a) => a.isNotEmpty).join(', ');
    final opens = interceptLabel(command.name);
    final semanticParts = <String>[
      command.displayName,
      if (command.summary.isNotEmpty) command.summary,
      if (opens != null) opens,
      if (command.category.isNotEmpty) 'category ${command.category}',
      if (command.risk.isNotEmpty) 'risk ${command.risk}',
      if (aliases.isNotEmpty) 'aliases $aliases',
      'usage $usage',
    ];
    final meta = TextStyle(
      fontFamily: SonderTheme.mono,
      fontSize: 11,
      color: cs.onSurfaceVariant.withValues(alpha: 0.75),
    );
    return Semantics(
      button: true,
      selected: selected,
      label: semanticParts.join('. '),
      child: InkWell(
        onTap: onTap,
        child: Container(
          color: selected ? cs.primary.withValues(alpha: 0.16) : null,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  _RiskDot(risk: command.risk),
                  const SizedBox(width: 8),
                  SizedBox(
                    width: 150,
                    child: Text(
                      command.displayName,
                      style: TextStyle(
                        fontFamily: SonderTheme.mono,
                        fontWeight:
                            selected ? FontWeight.w700 : FontWeight.w500,
                        color: cs.primary,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  if (command.category.isNotEmpty) ...[
                    const SizedBox(width: 8),
                    _CategoryTag(category: command.category),
                  ],
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(command.summary,
                        style: TextStyle(color: cs.onSurfaceVariant),
                        overflow: TextOverflow.ellipsis),
                  ),
                  if (opens != null) ...[
                    const SizedBox(width: 8),
                    Text(opens, style: meta),
                  ],
                ],
              ),
              if (usage != command.displayName || aliases.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(left: 17, top: 2),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (usage != command.displayName)
                        Text(usage,
                            style: meta, overflow: TextOverflow.ellipsis),
                      if (aliases.isNotEmpty)
                        Text('aliases: $aliases',
                            style: meta, overflow: TextOverflow.ellipsis),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _PaletteRow {
  final String? heading;
  final SonderCommand? command;
  final int matchIndex;

  const _PaletteRow.heading(this.heading)
      : command = null,
        matchIndex = -1;
  const _PaletteRow.command(this.command, this.matchIndex) : heading = null;
}

/// Command palette that opens when the composer starts with "/". A bare "/"
/// browses the popular shortlist under category headings; any further
/// character narrows to a flat ranked list.
class CommandPalette extends StatelessWidget {
  final List<SonderCommand> matches;
  final int selected;
  final bool grouped;
  final Map<String, String> categories;
  final ValueChanged<String> onPick;

  const CommandPalette({
    super.key,
    required this.matches,
    required this.selected,
    required this.grouped,
    required this.categories,
    required this.onPick,
  });

  List<_PaletteRow> get _rows {
    final rows = <_PaletteRow>[];
    String? lastCategory;
    for (var i = 0; i < matches.length; i++) {
      final command = matches[i];
      if (grouped && command.category != lastCategory) {
        lastCategory = command.category;
        rows.add(_PaletteRow.heading(command.category));
      }
      rows.add(_PaletteRow.command(command, i));
    }
    return rows;
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final rows = _rows;
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      constraints: const BoxConstraints(maxHeight: 320),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: cs.outlineVariant),
      ),
      child: ListView.builder(
        key: const Key('command-palette'),
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 4),
        itemCount: rows.length,
        itemBuilder: (context, i) {
          final row = rows[i];
          final command = row.command;
          if (command == null) {
            final key = row.heading ?? '';
            final blurb = categories[key] ?? '';
            return Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 2),
              child: Text(
                blurb.isEmpty
                    ? key.toUpperCase()
                    : '${key.toUpperCase()} — $blurb',
                style: TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                  letterSpacing: 0.6,
                  color: cs.onSurfaceVariant,
                ),
                overflow: TextOverflow.ellipsis,
              ),
            );
          }
          return CommandRow(
            command: command,
            selected: row.matchIndex == selected,
            onTap: () => onPick(command.name),
          );
        },
      ),
    );
  }
}

/// Two-level browser for the whole command surface: categories, then the
/// commands in one, with a search across all of them. Pops the picked name.
class CommandBrowser extends StatefulWidget {
  final CommandCatalog catalog;
  final bool fromServer;

  const CommandBrowser(
      {super.key, required this.catalog, required this.fromServer});

  @override
  State<CommandBrowser> createState() => _CommandBrowserState();
}

class _CommandBrowserState extends State<CommandBrowser> {
  final _search = TextEditingController();
  String? _category;
  String _query = '';

  @override
  void initState() {
    super.initState();
    _search.addListener(() {
      final next = _search.text.trim().toLowerCase();
      if (next == _query) return;
      setState(() => _query = next);
    });
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  List<SonderCommand> get _results {
    if (_query.isNotEmpty) {
      final needle = _query.startsWith('/') ? _query.substring(1) : _query;
      return widget.catalog.commands
          .where((c) => c.matchesLoose(needle))
          .toList(growable: false);
    }
    final category = _category;
    if (category == null) return const [];
    return widget.catalog.byCategory[category] ?? const [];
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final grouped = widget.catalog.byCategory;
    final showingCategories = _query.isEmpty && _category == null;
    final results = _results;
    final total = widget.catalog.commands.length;

    return Dialog(
      key: const Key('command-browser'),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760, maxHeight: 620),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  if (!showingCategories)
                    IconButton(
                      tooltip: 'All categories',
                      icon: const Icon(Icons.arrow_back),
                      onPressed: () {
                        _search.clear();
                        setState(() {
                          _category = null;
                          _query = '';
                        });
                      },
                    ),
                  Expanded(
                    child: Text(
                      showingCategories
                          ? 'Commands'
                          : (_query.isNotEmpty
                              ? 'Search results'
                              : _category ?? 'Commands'),
                      style: const TextStyle(
                          fontSize: 18, fontWeight: FontWeight.w700),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  IconButton(
                    tooltip: 'Close',
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
              TextField(
                key: const Key('command-browser-search'),
                controller: _search,
                decoration: const InputDecoration(
                  prefixIcon: Icon(Icons.search),
                  hintText: 'Search all commands…',
                  isDense: true,
                ),
              ),
              const SizedBox(height: 10),
              Flexible(
                child: showingCategories
                    ? ListView(
                        key: const Key('command-browser-categories'),
                        shrinkWrap: true,
                        children: [
                          for (final entry in grouped.entries)
                            ListTile(
                              key: Key('command-category-${entry.key}'),
                              dense: true,
                              leading: const Icon(Icons.folder_outlined),
                              title: Text(entry.key),
                              subtitle: Text(
                                widget.catalog.categories[entry.key] ?? '',
                                overflow: TextOverflow.ellipsis,
                              ),
                              trailing: Text('${entry.value.length}'),
                              onTap: () =>
                                  setState(() => _category = entry.key),
                            ),
                        ],
                      )
                    : ListView.builder(
                        key: const Key('command-browser-commands'),
                        shrinkWrap: true,
                        itemCount: results.length,
                        itemBuilder: (context, i) => CommandRow(
                          command: results[i],
                          selected: false,
                          onTap: () =>
                              Navigator.of(context).pop(results[i].name),
                        ),
                      ),
              ),
              const SizedBox(height: 8),
              Text(
                widget.fromServer
                    ? '$total commands published by this server.'
                    : 'Server catalog unavailable — showing $total built-in '
                        'commands.',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
