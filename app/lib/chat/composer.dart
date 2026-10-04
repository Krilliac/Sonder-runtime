import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../api.dart';
import '../runtime/model_routing.dart';
import '../safety_colors.dart';
import '../theme.dart';
import '../ui/kit.dart' show QuietAction, RingMeter;
import '../workspace_ui.dart' show conversationWidth;
import 'model_picker.dart';

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

/// The message box and its control strip:
///
/// ```
/// ┌──────────────────────────────────────────────────────────────┐
/// │ Ask Sonder…                                                  │
/// │ ● manual ⌄  sonder ⌄  ◔ 26%     Enter to send · …        (↑) │
/// └──────────────────────────────────────────────────────────────┘
/// ```
///
/// The permission mode chip (built by the shell; its contract and keys are
/// unchanged), the model picker and the context ring sit on the left; a
/// keyboard hint (wide layouts, when it fits whole) and Send, which becomes
/// Stop while a turn runs, on the right. Enter sends and Shift+Enter adds a
/// newline ([onKey]); a leading "/" opens the command palette above the
/// box, whose footer opens the full browser ([onOpenCommands]).
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

  /// The `/v1/models` ids, the selected one and how routes are bound. The
  /// picker shows when [onModelChanged] is set.
  final List<String> models;
  final String model;
  final ModelRouting routing;
  final ValueChanged<String>? onModelChanged;

  /// The status poll, for the context ring; null leaves the ring out.
  final ValueListenable<SystemInfo?>? status;

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
    this.models = const <String>[],
    this.model = '',
    this.routing = const ModelRouting(),
    this.onModelChanged,
    this.status,
  });

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return SafeArea(
      top: false,
      bottom: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(
            SonderSpace.md, SonderSpace.xs, SonderSpace.md, SonderSpace.sm),
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
                    onBrowse: onOpenCommands,
                  ),
                _ComposerSurface(
                  focusNode: focusNode,
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
                          style: text.bodyMedium,
                          decoration: const InputDecoration(
                            hintText: 'Ask Sonder…',
                            filled: false,
                            contentPadding: EdgeInsets.fromLTRB(SonderSpace.lg,
                                SonderSpace.md, SonderSpace.lg, SonderSpace.xs),
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                          ),
                        ),
                      ),
                      // The first pill lines up with the text above; Send
                      // keeps the same visual inset on the right.
                      Padding(
                        padding: const EdgeInsets.only(
                            left: SonderSpace.lg, right: SonderSpace.sm),
                        child: _ControlStrip(composer: this),
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

/// The composer's frame. Its border brightens while the box has focus.
class _ComposerSurface extends StatefulWidget {
  final FocusNode focusNode;
  final Widget child;
  const _ComposerSurface({required this.focusNode, required this.child});

  @override
  State<_ComposerSurface> createState() => _ComposerSurfaceState();
}

class _ComposerSurfaceState extends State<_ComposerSurface> {
  @override
  void initState() {
    super.initState();
    widget.focusNode.addListener(_changed);
  }

  @override
  void didUpdateWidget(covariant _ComposerSurface oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.focusNode, widget.focusNode)) {
      oldWidget.focusNode.removeListener(_changed);
      widget.focusNode.addListener(_changed);
    }
  }

  @override
  void dispose() {
    widget.focusNode.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final focused = widget.focusNode.hasFocus;
    return AnimatedContainer(
      key: const Key('composer-surface'),
      duration: SonderMotion.of(context, SonderMotion.fast),
      curve: SonderMotion.standard,
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.card),
        border: Border.all(
          color: focused
              ? tokens.accentText.withValues(alpha: 0.7)
              : tokens.hairlineStrong,
        ),
      ),
      child: widget.child,
    );
  }
}

/// Mode, model and context on the left; the keyboard hint and Send/Stop on
/// the right ([_StripLayout] gives each its width).
class _ControlStrip extends StatelessWidget {
  final ChatComposer composer;
  const _ControlStrip({required this.composer});

  @override
  Widget build(BuildContext context) {
    final c = composer;
    final onModelChanged = c.onModelChanged;
    return LayoutBuilder(builder: (context, constraints) {
      // A very narrow strip leaves the ring out; the status line under the
      // composer still states the context.
      final status = constraints.maxWidth >= 320 ? c.status : null;
      return SizedBox(
        height: 48,
        child: CustomMultiChildLayout(
          delegate: _StripLayout(),
          children: [
            if (c.modeChip != null)
              LayoutId(id: _StripSlot.mode, child: c.modeChip!),
            if (onModelChanged != null && c.models.isNotEmpty)
              LayoutId(
                id: _StripSlot.model,
                child: ModelPickerButton(
                  models: c.models,
                  current: c.model,
                  routing: c.routing,
                  onSelected: onModelChanged,
                  dense: !c.desktop,
                ),
              ),
            if (status != null)
              LayoutId(
                id: _StripSlot.ring,
                child: _ContextRing(status: status, showPercent: c.desktop),
              ),
            if (c.desktop)
              LayoutId(id: _StripSlot.hint, child: const _KeyboardHint()),
            LayoutId(
              id: _StripSlot.send,
              child: _SendButton(
                controller: c.controller,
                sending: c.sending,
                onSend: c.onSend,
                onStop: c.onCancel,
              ),
            ),
          ],
        ),
      );
    });
  }
}

enum _StripSlot { mode, model, ring, hint, send }

/// Lays the strip out in priority order: Send at the end; the mode chip at
/// its natural width (it is the contract, and its label ellipsizes only
/// when it must); the model picker in what the chip leaves; the ring when
/// there is still room; the hint in whatever is left between them.
class _StripLayout extends MultiChildLayoutDelegate {
  static const _gap = SonderSpace.xs;
  static const _pickerMin = 96.0;
  static const _chipMin = 72.0;

  @override
  void performLayout(Size size) {
    final height = size.height;
    Size layout(_StripSlot slot, double maxWidth) => layoutChild(
          slot,
          BoxConstraints(
              maxWidth: maxWidth < 0 ? 0 : maxWidth, maxHeight: height),
        );
    double centre(Size child) => (height - child.height) / 2;

    final send = layout(_StripSlot.send, size.width);
    var left = 0.0;
    var room = size.width - send.width - _gap;

    final hasRing = hasChild(_StripSlot.ring);
    final hasModel = hasChild(_StripSlot.model);
    final ring = hasRing ? layout(_StripSlot.ring, room) : Size.zero;
    final ringWidth = ring.width;

    if (hasChild(_StripSlot.mode)) {
      final reserve = hasModel ? _pickerMin + _gap : 0.0;
      final max = (room - ringWidth - reserve).clamp(_chipMin, 260.0);
      final chip = layout(_StripSlot.mode, max);
      positionChild(_StripSlot.mode, Offset(left, centre(chip)));
      left += chip.width + _gap;
      room -= chip.width + _gap;
    }
    if (hasModel) {
      final max = (room - ringWidth).clamp(0.0, 300.0);
      final picker = layout(_StripSlot.model, max);
      positionChild(_StripSlot.model, Offset(left, centre(picker)));
      left += picker.width;
      room -= picker.width;
    }
    if (hasRing) {
      positionChild(_StripSlot.ring, Offset(left, centre(ring)));
      left += ringWidth;
      room -= ringWidth;
    }
    if (hasChild(_StripSlot.hint)) {
      final hint = layout(_StripSlot.hint, room - SonderSpace.sm);
      positionChild(_StripSlot.hint,
          Offset(size.width - send.width - _gap - hint.width, centre(hint)));
    }
    positionChild(
        _StripSlot.send, Offset(size.width - send.width, centre(send)));
  }

  @override
  bool shouldRelayout(_StripLayout oldDelegate) => false;
}

/// "Enter to send · Shift+Enter for a new line", shown only when it fits
/// whole: a cut hint is noise.
class _KeyboardHint extends StatelessWidget {
  const _KeyboardHint();

  static const text = 'Enter to send · Shift+Enter for a new line';

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final style =
        Theme.of(context).textTheme.bodySmall?.copyWith(color: tokens.muted);
    return LayoutBuilder(builder: (context, constraints) {
      final painter = TextPainter(
        text: TextSpan(text: text, style: style),
        textDirection: TextDirection.ltr,
        textScaler: MediaQuery.textScalerOf(context),
        maxLines: 1,
      )..layout();
      final fits = painter.width <= constraints.maxWidth;
      final width = painter.width;
      painter.dispose();
      if (!fits) return const SizedBox.shrink();
      return SizedBox(
        width: width,
        child: Text(text,
            key: const Key('composer-hint'), maxLines: 1, style: style),
      );
    });
  }
}

/// Send: an accent circle with an arrow, quiet while the box is empty.
/// While a turn runs it is Stop.
class _SendButton extends StatelessWidget {
  final TextEditingController controller;
  final bool sending;
  final VoidCallback onSend;
  final VoidCallback onStop;

  const _SendButton({
    required this.controller,
    required this.sending,
    required this.onSend,
    required this.onStop,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<TextEditingValue>(
      valueListenable: controller,
      builder: (context, value, _) {
        final empty = value.text.trim().isEmpty;
        return IconButton.filled(
          key: const Key('composer-send'),
          tooltip: sending ? 'Stop' : 'Send',
          onPressed: sending ? onStop : (empty ? null : onSend),
          style: IconButton.styleFrom(
            fixedSize: const Size(32, 32),
            minimumSize: const Size(32, 32),
            padding: EdgeInsets.zero,
            tapTargetSize: MaterialTapTargetSize.padded,
            shape: const CircleBorder(),
            backgroundColor: tokens.accent,
            foregroundColor: tokens.onAccent,
            disabledBackgroundColor: tokens.raised,
            disabledForegroundColor: tokens.muted,
          ),
          icon: AnimatedSwitcher(
            duration: SonderMotion.of(context, SonderMotion.fast),
            child: Icon(
              sending ? Icons.stop_rounded : Icons.arrow_upward_rounded,
              key: ValueKey<bool>(sending),
              size: 18,
            ),
          ),
        );
      },
    );
  }
}

/// `2100` → `2,100`.
String _grouped(int n) {
  final digits = n.abs().toString();
  final out = StringBuffer(n < 0 ? '-' : '');
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) out.write(',');
    out.write(digits[i]);
  }
  return out.toString();
}

/// The words of the context ring: `Context: 2,100 of 8,192 tokens used
/// (26%)`. Null when the server does not report context.
String? contextUsageText(ContextHealth? ctx) {
  if (ctx == null) return null;
  final limit =
      ctx.contextLimit > 0 ? ctx.contextLimit : ctx.nativeContextLimit;
  if (limit <= 0) return null;
  final used = ctx.estimatedTokens < 0 ? 0 : ctx.estimatedTokens;
  final percent = (used * 100 / limit).round();
  return 'Context: ${_grouped(used)} of ${_grouped(limit)} tokens used '
      '($percent%)';
}

/// How full the session's context is: a small ring (and, on wide layouts,
/// the percentage) whose tooltip states the numbers. It turns warn at 75%
/// and danger at 90%. Hidden while the server does not report context (an
/// account without host-wide status, offline).
class _ContextRing extends StatelessWidget {
  final ValueListenable<SystemInfo?> status;
  final bool showPercent;
  const _ContextRing({required this.status, this.showPercent = false});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return ValueListenableBuilder<SystemInfo?>(
      valueListenable: status,
      builder: (context, info, _) {
        final ctx = info?.context;
        final words = contextUsageText(ctx);
        if (ctx == null || words == null) return const SizedBox.shrink();
        final limit =
            ctx.contextLimit > 0 ? ctx.contextLimit : ctx.nativeContextLimit;
        final used = ctx.estimatedTokens < 0 ? 0 : ctx.estimatedTokens;
        final fraction = used / limit;
        return Tooltip(
          key: const Key('context-ring'),
          message: words,
          triggerMode: TooltipTriggerMode.tap,
          // The ring's own label already says it.
          excludeFromSemantics: true,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: SonderSpace.sm),
            child: SizedBox(
              height: 40,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  RingMeter(value: fraction, semanticLabel: words),
                  if (showPercent) ...[
                    const SizedBox(width: SonderSpace.xs + SonderSpace.xxs),
                    ExcludeSemantics(
                      child: Text('${(fraction * 100).round()}%',
                          key: const Key('context-ring-percent'),
                          style: tokens.mono(11.5,
                              color: RingMeter.colorFor(tokens, fraction) ==
                                      tokens.accentText
                                  ? tokens.muted
                                  : RingMeter.colorFor(tokens, fraction))),
                    ),
                  ],
                ],
              ),
            ),
          ),
        );
      },
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
              width: SonderSpace.sm,
              height: SonderSpace.sm,
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
    final tokens = SonderTokens.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: SonderSpace.sm),
      decoration: BoxDecoration(
        color: tokens.raised,
        borderRadius: BorderRadius.circular(SonderRadius.control),
        border: Border.all(color: tokens.hairline),
      ),
      child: Text(category,
          style: Theme.of(context)
              .textTheme
              .labelMedium
              ?.copyWith(color: tokens.text2)),
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
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
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
    final meta = tokens.mono(11, color: tokens.muted);
    return Semantics(
      button: true,
      selected: selected,
      label: semanticParts.join('. '),
      child: InkWell(
        onTap: onTap,
        child: Container(
          color: selected ? tokens.accentDim : null,
          padding: const EdgeInsets.symmetric(
              horizontal: SonderSpace.md, vertical: SonderSpace.sm),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  _RiskDot(risk: command.risk),
                  const SizedBox(width: SonderSpace.sm),
                  SizedBox(
                    width: 150,
                    child: Text(
                      command.displayName,
                      style: tokens.mono(13,
                          color: tokens.accentText,
                          weight: selected ? FontWeight.w600 : FontWeight.w500),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                  if (command.category.isNotEmpty) ...[
                    const SizedBox(width: SonderSpace.sm),
                    _CategoryTag(category: command.category),
                  ],
                  const SizedBox(width: SonderSpace.md),
                  Expanded(
                    child: Text(command.summary,
                        style: text.bodyMedium?.copyWith(color: tokens.text2),
                        overflow: TextOverflow.ellipsis),
                  ),
                  if (opens != null) ...[
                    const SizedBox(width: SonderSpace.sm),
                    Text(opens, style: meta),
                  ],
                ],
              ),
              if (usage != command.displayName || aliases.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(
                      left: 24 + SonderSpace.sm, top: SonderSpace.xxs),
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

  /// Opens the full command browser; null leaves the footer out.
  final VoidCallback? onBrowse;

  const CommandPalette({
    super.key,
    required this.matches,
    required this.selected,
    required this.grouped,
    required this.categories,
    required this.onPick,
    this.onBrowse,
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
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final rows = _rows;
    return Container(
      margin: const EdgeInsets.only(bottom: SonderSpace.sm),
      constraints: const BoxConstraints(maxHeight: 320),
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: BorderRadius.circular(SonderRadius.card),
        border: Border.all(color: tokens.hairlineStrong),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Flexible(child: _list(rows, text)),
          if (onBrowse != null) ...[
            Divider(height: 1, color: tokens.hairline),
            Row(children: [
              QuietAction(
                key: const Key('command-palette-browse'),
                icon: Icons.manage_search,
                label: 'Browse all commands',
                onPressed: onBrowse,
              ),
              const Spacer(),
              Padding(
                padding: const EdgeInsets.only(right: SonderSpace.md),
                child:
                    Text('Ctrl+K', style: tokens.mono(11, color: tokens.muted)),
              ),
            ]),
          ],
        ],
      ),
    );
  }

  Widget _list(List<_PaletteRow> rows, TextTheme text) {
    return ListView.builder(
      key: const Key('command-palette'),
      shrinkWrap: true,
      padding: const EdgeInsets.symmetric(vertical: SonderSpace.xs),
      itemCount: rows.length,
      itemBuilder: (context, i) {
        final row = rows[i];
        final command = row.command;
        if (command == null) {
          final key = row.heading ?? '';
          final blurb = categories[key] ?? '';
          return Padding(
            padding: const EdgeInsets.fromLTRB(SonderSpace.md, SonderSpace.sm,
                SonderSpace.md, SonderSpace.xxs),
            child: Text(
              blurb.isEmpty
                  ? key.toUpperCase()
                  : '${key.toUpperCase()} — $blurb',
              style: text.labelSmall,
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
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final grouped = widget.catalog.byCategory;
    final showingCategories = _query.isEmpty && _category == null;
    final results = _results;
    final total = widget.catalog.commands.length;

    return Dialog(
      key: const Key('command-browser'),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 760, maxHeight: 620),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.lg, SonderSpace.md, SonderSpace.lg, SonderSpace.md),
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
                      style: text.titleMedium,
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
              const SizedBox(height: SonderSpace.md),
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
                              trailing: Text('${entry.value.length}',
                                  style: tokens.mono(12, color: tokens.muted)),
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
              const SizedBox(height: SonderSpace.sm),
              Text(
                widget.fromServer
                    ? '$total commands published by this server.'
                    : 'Server catalog unavailable — showing $total built-in '
                        'commands.',
                style: text.bodySmall,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
