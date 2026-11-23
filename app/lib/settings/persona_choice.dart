/// Persona control shared by Settings and the chat composer:
/// Auto (null, auto-route by tier and prompt), Off (no persona prompt), or one
/// of the 9 canonical personas (`architect`, `coder`, `concise`, `creative`,
/// `explainer`, `mathematician`, `reviewer`, `security`, `teacher`).
library;

import 'package:flutter/material.dart';

import '../theme.dart';

/// Canonical named personas, in alphabetical order.
const List<String> personaNames = <String>[
  'architect',
  'coder',
  'concise',
  'creative',
  'explainer',
  'mathematician',
  'reviewer',
  'security',
  'teacher',
];

/// All selectable values including `null` (Auto) and `'off'`.
const List<String?> personaOptions = <String?>[
  null,
  'off',
  ...personaNames,
];

const Map<String, String> _personaAliases = <String, String>{
  'none': 'off',
  'disable': 'off',
  'disabled': 'off',
  'false': 'off',
  'math': 'mathematician',
  'sec': 'security',
  'arch': 'architect',
  'review': 'reviewer',
  'explain': 'explainer',
  'teach': 'teacher',
  'brief': 'concise',
  'short': 'concise',
  'terse': 'concise',
};

/// Canonical persona (`'off'` or one of [personaNames]), or null (Auto).
/// Unknown values normalize to null.
String? normalizePersona(String? value) {
  if (value == null) return null;
  final text = value.trim().toLowerCase();
  if (text.isEmpty || text == 'auto' || text == 'default' || text == 'reset') {
    return null;
  }
  final canonical = _personaAliases[text] ?? text;
  if (canonical == 'off' || personaNames.contains(canonical)) {
    return canonical;
  }
  return null;
}

/// Parses a typed `/persona <arg>` value.
/// Returns `(recognized, value)` where `value == null` means Auto.
(bool, String?) parsePersonaInput(String? raw) {
  if (raw == null) return (false, null);
  final text = raw.trim().toLowerCase();
  if (text.isEmpty) return (false, null);
  if (text == 'auto' || text == 'default' || text == 'reset') {
    return (true, null);
  }
  final canonical = _personaAliases[text] ?? text;
  if (canonical == 'off' || personaNames.contains(canonical)) {
    return (true, canonical);
  }
  return (false, null);
}

/// Stored preference string -> persona (`'off'` or canonical name) or null (Auto).
String? parsePersona(String? text) => normalizePersona(text);

/// Wire/display lowercase label (`auto`, `off`, or canonical persona name).
String personaLabel(String? value) => normalizePersona(value) ?? 'auto';

/// Compact title-case label for pills and dropdowns.
String personaShortLabel(String? value) => switch (normalizePersona(value)) {
      null => 'Auto',
      'off' => 'Off',
      'architect' => 'Architect',
      'coder' => 'Coder',
      'concise' => 'Concise',
      'creative' => 'Creative',
      'explainer' => 'Explainer',
      'mathematician' => 'Math',
      'reviewer' => 'Reviewer',
      'security' => 'Security',
      'teacher' => 'Teacher',
      _ => 'Auto',
    };

/// Human-readable subtitle for menu items and Settings.
String personaDescription(String? value) => switch (normalizePersona(value)) {
      null => 'Auto-route by tier and prompt',
      'off' => 'No persona prompt — raw model voice',
      'architect' => 'System design, trade-offs, and structural clarity',
      'coder' => 'Pragmatic, clean, runnable implementation focus',
      'concise' => 'Short, direct answers without filler',
      'creative' => 'Expressive, imaginative brainstorming and prose',
      'explainer' => 'Clear step-by-step mental models and analogies',
      'mathematician' => 'Rigorous derivations, proofs, and notation',
      'reviewer' => 'Critical code review for bugs, risks, and edge cases',
      'security' => 'Threat modeling, hardening, and defensive analysis',
      'teacher' => 'Guided learning with checks and worked examples',
      _ => 'Auto-route by tier and prompt',
    };

/// Compact composer pill for switching persona per session.
class PersonaPickerButton extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onSelected;
  final bool dense;

  const PersonaPickerButton({
    super.key,
    required this.value,
    required this.onSelected,
    this.dense = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final normalized = normalizePersona(value);
    final active = normalized != null;
    final label = dense
        ? personaShortLabel(normalized)
        : 'Persona: ${personaShortLabel(normalized)}';
    return Tooltip(
      message: 'Persona: ${personaLabel(normalized)} — '
          '${personaDescription(normalized)}',
      child: PopupMenuButton<String>(
        key: const Key('composer-persona-picker'),
        tooltip: '',
        popUpAnimationStyle: SonderMotion.menu(context),
        initialValue: normalized ?? 'auto',
        onSelected: (picked) =>
            onSelected(picked == 'auto' ? null : normalizePersona(picked)),
        itemBuilder: (context) => [
          for (final opt in personaOptions)
            PopupMenuItem<String>(
              key: Key('persona-option-${opt ?? 'auto'}'),
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
                          personaShortLabel(opt),
                          style: text.labelLarge?.copyWith(
                            color: opt == normalized
                                ? tokens.accentText
                                : tokens.text,
                          ),
                        ),
                        Text(
                          personaDescription(opt),
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
                normalized == null
                    ? Icons.auto_awesome_outlined
                    : Icons.badge_outlined,
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

/// Settings page selector for persona.
class PersonaSelector extends StatelessWidget {
  final String? value;
  final ValueChanged<String?> onChanged;

  const PersonaSelector({
    super.key,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final normalized = normalizePersona(value);
    return Wrap(
      key: const Key('settings-persona-selector'),
      spacing: SonderSpace.xs,
      runSpacing: SonderSpace.xs,
      children: [
        for (final opt in personaOptions)
          ChoiceChip(
            key: Key('settings-persona-${opt ?? 'auto'}'),
            tooltip: personaDescription(opt),
            label: Text(personaShortLabel(opt)),
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
