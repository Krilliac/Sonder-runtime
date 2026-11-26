/// Execution-mode routing selector shared by Settings and the chat composer:
/// Auto (null, omit `execution_mode` so the server difficulty router decides)
/// or an explicit override (`single`, `ensemble`, `fanout`).
library;

import 'package:flutter/material.dart';

import '../theme.dart';

/// Canonical non-default execution modes offered in the UI.
const List<String> executionModes = <String>[
  'single',
  'ensemble',
  'fanout',
];

/// All selectable execution-mode values including `null` (Auto / default).
const List<String?> executionModeOptions = <String?>[
  null,
  'single',
  'ensemble',
  'fanout',
];

const Map<String, String> _executionModeAliases = <String, String>{
  'fan-out': 'fanout',
  'fan_out': 'fanout',
};

/// Canonical non-default execution mode (`single`, `ensemble`, `fanout`), or
/// null for Auto (default). Unknown values normalize to null.
String? normalizeExecutionMode(String? value) {
  if (value == null) return null;
  final text = value.trim().toLowerCase();
  if (text.isEmpty || text == 'auto' || text == 'default' || text == 'reset') {
    return null;
  }
  final canonical = _executionModeAliases[text] ?? text;
  return executionModes.contains(canonical) ? canonical : null;
}

/// Parses a typed `/exec_mode <arg>` value.
/// Returns `(recognized, value)` where `value == null` means Auto.
(bool, String?) parseExecutionModeInput(String? raw) {
  if (raw == null) return (false, null);
  final text = raw.trim().toLowerCase();
  if (text.isEmpty) return (false, null);
  if (text == 'auto' || text == 'default' || text == 'reset') {
    return (true, null);
  }
  final canonical = _executionModeAliases[text] ?? text;
  if (executionModes.contains(canonical)) return (true, canonical);
  return (false, null);
}

/// Stored preference string -> execution mode or null (Auto).
String? parseExecutionMode(String? text) => normalizeExecutionMode(text);

/// Wire/display lowercase label (`auto`, `single`, `ensemble`, `fanout`).
String executionModeLabel(String? value) =>
    normalizeExecutionMode(value) ?? 'auto';

/// Compact title-case label for pills, menus, and chips.
String executionModeShortLabel(String? value) =>
    switch (normalizeExecutionMode(value)) {
      null => 'Auto',
      'single' => 'Single',
      'ensemble' => 'Ensemble',
      'fanout' => 'Fanout',
      _ => 'Auto',
    };

/// Human-readable subtitle for menus and Settings.
String executionModeDescription(String? value) =>
    switch (normalizeExecutionMode(value)) {
      null => 'Difficulty router chooses Single, Ensemble, or Fanout',
      'single' => 'Force a single model tier for this turn',
      'ensemble' => 'Run multiple tiers in parallel and synthesize',
      'fanout' => 'Bounded multi-model local fan-out',
      _ => 'Difficulty router chooses Single, Ensemble, or Fanout',
    };

/// Compact composer pill for switching execution mode (`Auto`, `Single`,
/// `Ensemble`, `Fanout`).
class ExecutionModePickerButton extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onSelected;
  final bool dense;

  const ExecutionModePickerButton({
    super.key,
    required this.value,
    required this.onSelected,
    this.dense = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final normalized = normalizeExecutionMode(value);
    final active = normalized != null;
    final label = dense
        ? executionModeShortLabel(normalized)
        : 'Exec: ${executionModeShortLabel(normalized)}';
    final icon = switch (normalized) {
      'ensemble' => Icons.hub_outlined,
      'fanout' => Icons.call_split_rounded,
      'single' => Icons.bolt_outlined,
      _ => Icons.alt_route_rounded,
    };
    return Tooltip(
      message: 'Execution mode: ${executionModeShortLabel(normalized)} — '
          '${executionModeDescription(normalized)}',
      child: PopupMenuButton<String>(
        key: const Key('composer-execution-mode-picker'),
        tooltip: '',
        popUpAnimationStyle: SonderMotion.menu(context),
        initialValue: normalized ?? 'auto',
        onSelected: (picked) => onSelected(
          picked == 'auto' ? null : normalizeExecutionMode(picked),
        ),
        itemBuilder: (context) => [
          for (final opt in executionModeOptions)
            PopupMenuItem<String>(
              key: Key('execution-mode-option-${opt ?? 'auto'}'),
              value: opt ?? 'auto',
              child: Row(
                children: [
                  SizedBox(
                    width: 20,
                    child: opt == normalized
                        ? Icon(Icons.check, size: 15, color: tokens.accentText)
                        : null,
                  ),
                  const SizedBox(width: SonderSpace.xs),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          executionModeShortLabel(opt),
                          style: text.labelLarge?.copyWith(
                            color: opt == normalized
                                ? tokens.accentText
                                : tokens.text,
                          ),
                        ),
                        Text(
                          executionModeDescription(opt),
                          style: text.bodySmall?.copyWith(color: tokens.muted),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
        ],
        child: Container(
          height: 32,
          padding: EdgeInsets.symmetric(
            horizontal: dense ? SonderSpace.xs : SonderSpace.sm,
          ),
          decoration: BoxDecoration(
            color: active
                ? tokens.accent.withValues(alpha: 0.10)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(SonderRadius.pill),
            border: Border.all(
              color: active
                  ? tokens.accent.withValues(alpha: 0.35)
                  : tokens.hairline,
            ),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                icon,
                size: 14,
                color: active ? tokens.accentText : tokens.muted,
              ),
              const SizedBox(width: SonderSpace.xs),
              Flexible(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: text.labelMedium?.copyWith(
                    color: active ? tokens.accentText : tokens.text2,
                  ),
                ),
              ),
              Icon(Icons.expand_more, size: 15, color: tokens.muted),
            ],
          ),
        ),
      ),
    );
  }
}

/// Settings page selector for execution mode.
class ExecutionModeSelector extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onChanged;

  const ExecutionModeSelector({
    super.key,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final normalized = normalizeExecutionMode(value);
    return Wrap(
      key: const Key('settings-execution-mode-selector'),
      spacing: SonderSpace.xs,
      runSpacing: SonderSpace.xs,
      children: [
        for (final opt in executionModeOptions)
          ChoiceChip(
            key: Key('settings-execution-mode-${opt ?? 'auto'}'),
            tooltip: executionModeDescription(opt),
            label: Text(executionModeShortLabel(opt)),
            selected: opt == normalized,
            onSelected: (_) => onChanged(opt),
            selectedColor: tokens.accent.withValues(alpha: 0.18),
            labelStyle: Theme.of(context).textTheme.labelMedium?.copyWith(
                  color: opt == normalized ? tokens.accentText : tokens.text2,
                ),
          ),
      ],
    );
  }
}
