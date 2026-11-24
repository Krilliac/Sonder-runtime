/// Reasoning effort control shared by Settings and the chat composer:
/// Auto (null, server/model default) or one of the canonical effort levels
/// (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`).
library;

import 'package:flutter/material.dart';

import '../theme.dart';

/// Canonical non-auto effort levels, in ascending order.
const List<String> effortLevels = <String>[
  'off',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
];

/// All selectable values including `null` (Auto).
const List<String?> effortOptions = <String?>[
  null,
  'off',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
];

const Map<String, String> _effortAliases = <String, String>{
  'none': 'off',
  'disable': 'off',
  'disabled': 'off',
  'false': 'off',
  'min': 'minimal',
  'med': 'medium',
  'max': 'xhigh',
  'ultra': 'xhigh',
};

/// Canonical effort level or null (Auto). Unknown values normalize to null.
String? normalizeEffort(String? value) {
  if (value == null) return null;
  final text = value.trim().toLowerCase();
  if (text.isEmpty || text == 'auto' || text == 'default' || text == 'reset') {
    return null;
  }
  final canonical = _effortAliases[text] ?? text;
  return effortLevels.contains(canonical) ? canonical : null;
}

/// Parses a typed `/effort <arg>` value.
/// Returns `(recognized, value)` where `value == null` means Auto.
(bool, String?) parseEffortInput(String? raw) {
  if (raw == null) return (false, null);
  final text = raw.trim().toLowerCase();
  if (text.isEmpty) return (false, null);
  if (text == 'auto' || text == 'default' || text == 'reset') {
    return (true, null);
  }
  final canonical = _effortAliases[text] ?? text;
  if (effortLevels.contains(canonical)) return (true, canonical);
  return (false, null);
}

/// Stored preference string -> effort level or null (Auto).
String? parseEffort(String? text) => normalizeEffort(text);

/// Wire/display lowercase label (`auto`, `off`, `minimal`, `low`, `medium`, `high`, `xhigh`).
String effortLabel(String? value) => normalizeEffort(value) ?? 'auto';

/// Compact title-case label for pills and dropdowns.
String effortShortLabel(String? value) => switch (normalizeEffort(value)) {
      null => 'Auto',
      'off' => 'Off',
      'minimal' => 'Min',
      'low' => 'Low',
      'medium' => 'Med',
      'high' => 'High',
      'xhigh' => 'XHigh',
      _ => 'Auto',
    };

/// Human-readable subtitle for menu items and Settings.
String effortDescription(String? value) => switch (normalizeEffort(value)) {
      null => 'Follow server or model default',
      'off' => 'No reasoning — fastest responses and tool loops',
      'minimal' => 'Briefest thinking before answering',
      'low' => 'Light reasoning for quick verification',
      'medium' => 'Balanced reasoning depth and latency',
      'high' => 'Deep reasoning for harder debugging and design',
      'xhigh' => 'Maximum reasoning effort',
      _ => 'Follow server or model default',
    };

/// Compact composer pill for switching reasoning effort per session.
class EffortPickerButton extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onSelected;
  final bool dense;

  const EffortPickerButton({
    super.key,
    required this.value,
    required this.onSelected,
    this.dense = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final normalized = normalizeEffort(value);
    final active = normalized != null;
    final label = dense
        ? effortShortLabel(normalized)
        : 'Effort: ${effortShortLabel(normalized)}';
    return Tooltip(
      message: 'Reasoning effort: ${effortLabel(normalized)} — '
          '${effortDescription(normalized)}',
      child: PopupMenuButton<String>(
        key: const Key('composer-effort-picker'),
        tooltip: '',
        popUpAnimationStyle: SonderMotion.menu(context),
        initialValue: normalized ?? 'auto',
        onSelected: (picked) =>
            onSelected(picked == 'auto' ? null : normalizeEffort(picked)),
        itemBuilder: (context) => [
          for (final opt in effortOptions)
            PopupMenuItem<String>(
              key: Key('effort-option-${opt ?? 'auto'}'),
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
                          effortShortLabel(opt),
                          style: text.labelLarge?.copyWith(
                            color: opt == normalized
                                ? tokens.accentText
                                : tokens.text,
                          ),
                        ),
                        Text(
                          effortDescription(opt),
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
            color: active ? tokens.accent.withValues(alpha: 0.10) : Colors.transparent,
            borderRadius: BorderRadius.circular(SonderRadius.pill),
            border: Border.all(
              color: active ? tokens.accent.withValues(alpha: 0.35) : tokens.hairline,
            ),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                normalized == 'off'
                    ? Icons.bolt_outlined
                    : Icons.psychology_outlined,
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

/// Settings page selector for reasoning effort.
class EffortSelector extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onChanged;

  const EffortSelector({
    super.key,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final normalized = normalizeEffort(value);
    return Wrap(
      key: const Key('settings-effort-selector'),
      spacing: SonderSpace.xs,
      runSpacing: SonderSpace.xs,
      children: [
        for (final opt in effortOptions)
          ChoiceChip(
            key: Key('settings-effort-${opt ?? 'auto'}'),
            label: Text(effortShortLabel(opt)),
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
