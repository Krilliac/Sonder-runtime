import 'package:flutter/material.dart';

import '../api.dart';
import '../safety_colors.dart';
import '../theme.dart';
import 'permission_rules.dart';

export '../api.dart' show PermissionMode, PermissionModeOption;
export '../ui/raise_mode_sheet.dart'
    show RaiseModeSheet, confirmModeChange, showRaiseModeSheet;
export 'permission_rules.dart';

/// The always-visible autonomy indicator in the composer: what the agent
/// will do without asking, and — separately — whether it is elevated.
///
/// Three states: live (tap to change), read-only after a 403 (tooltip says
/// only an administrator can change it) and offline (disabled, showing the
/// word "offline" rather than a possibly stale mode).
class PermissionModeChip extends StatelessWidget {
  final PermissionMode state;
  final bool busy;
  final bool readOnly;
  final VoidCallback onTap;

  const PermissionModeChip({
    super.key,
    required this.state,
    required this.busy,
    required this.onTap,
    this.readOnly = false,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final tone = permissionModeColor(Theme.of(context).colorScheme, state.mode);
    final modeLabel = state.displayLabel;
    final modeDescription = state.blurb.trim().isEmpty
        ? 'Autonomy mode ${state.displayLabel}'
        : '${state.displayLabel}: ${state.blurb}';
    final tooltip = readOnly
        ? modeReadOnlyText
        : (state.blurb.trim().isEmpty
            ? 'Autonomy mode — tap to change'
            : '${state.displayLabel} — ${state.blurb}');
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Flexible(
          child: Tooltip(
            message: tooltip,
            child: Semantics(
              button: true,
              enabled: !readOnly,
              label: modeDescription,
              hint: readOnly
                  ? modeReadOnlyText
                  : (busy ? 'Changing mode' : 'Double tap to change mode'),
              child: InkWell(
                key: const Key('permission-mode-chip'),
                onTap: busy || readOnly ? null : onTap,
                borderRadius: BorderRadius.circular(SonderRadius.pill),
                child: ConstrainedBox(
                  // A 48 dp hit area around a 28 dp pill (P2-5).
                  constraints: const BoxConstraints(minHeight: 48),
                  child: Center(
                    widthFactor: 1,
                    child: Container(
                      height: 30,
                      padding: const EdgeInsets.fromLTRB(
                          SonderSpace.md, 0, SonderSpace.sm, 0),
                      decoration: BoxDecoration(
                        color: tokens.sunken,
                        borderRadius: BorderRadius.circular(SonderRadius.pill),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          // The icon and the word name the mode; the tone
                          // only repeats what they say.
                          Icon(permissionModeIcon(state.mode),
                              size: 14, color: tone),
                          const SizedBox(width: SonderSpace.sm),
                          Flexible(
                            child: Text(modeLabel,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: tokens.mono(12,
                                    weight: FontWeight.w500,
                                    color:
                                        readOnly ? tokens.text2 : tokens.text)),
                          ),
                          if (busy)
                            Padding(
                              padding: const EdgeInsets.only(
                                  left: SonderSpace.sm, right: SonderSpace.xxs),
                              child: SizedBox(
                                width: SonderSpace.md,
                                height: SonderSpace.md,
                                child: CircularProgressIndicator(
                                    strokeWidth: 2, color: tokens.text2),
                              ),
                            )
                          else if (readOnly)
                            Padding(
                              padding: const EdgeInsets.only(
                                  left: SonderSpace.xs, right: SonderSpace.xxs),
                              child: Icon(Icons.lock_outline,
                                  size: 13, color: tokens.muted),
                            )
                          else
                            Icon(Icons.expand_more,
                                size: 16, color: tokens.muted),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
        if (state.elevated) ...[
          const SizedBox(width: SonderSpace.tight),
          ElevatedBadge(reason: state.elevationReason),
        ],
      ],
    );
  }
}

/// The chip while the server cannot be reached (P2-13): still in place so
/// the composer does not jump, disabled, and saying "offline" instead of a
/// mode that may no longer be true.
class OfflineModeChip extends StatelessWidget {
  const OfflineModeChip({super.key});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Tooltip(
      message: "The mode can't be read while the server is unreachable",
      child: Semantics(
        button: true,
        enabled: false,
        label: 'Autonomy mode unknown: server unreachable',
        child: Container(
          key: const Key('permission-mode-chip-offline'),
          height: 30,
          padding: const EdgeInsets.symmetric(horizontal: SonderSpace.md),
          decoration: BoxDecoration(
            color: tokens.sunken,
            borderRadius: BorderRadius.circular(SonderRadius.pill),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            Text('– mode offline', style: tokens.mono(12, color: tokens.muted)),
          ]),
        ),
      ),
    );
  }
}

/// The privilege axis, as its own badge outside the mode chip.
///
/// Elevation is a different question from autonomy — `permission_modes.py`
/// grants it from no mode at all — so it is kept distinct: outside the chip,
/// danger tone, a shield, spaced capitals.
class ElevatedBadge extends StatelessWidget {
  final String reason;
  const ElevatedBadge({super.key, required this.reason});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return Tooltip(
      message: reason.trim().isEmpty
          ? 'Elevated privileges are on. This is separate from the mode — no '
              'mode turns it on.'
          : 'Elevated privileges are on: ${reason.trim()}',
      child: Semantics(
        label: 'Elevated privileges: on',
        hint: reason.trim().isEmpty ? null : reason.trim(),
        child: Container(
          key: const Key('permission-elevated-badge'),
          height: 28,
          padding: const EdgeInsets.symmetric(horizontal: SonderSpace.sm),
          decoration: BoxDecoration(
            color: tokens.dangerDim,
            borderRadius: BorderRadius.circular(SonderRadius.control),
            border: Border.all(color: tokens.danger, width: 1.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.shield_outlined, size: 13, color: tokens.danger),
              const SizedBox(width: SonderSpace.xs),
              Text(
                'ADMIN',
                style: tokens
                    .mono(11, color: tokens.danger, weight: FontWeight.w600)
                    .copyWith(letterSpacing: 1.0),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The mode picker: every mode the server publishes, with its blurb, and
/// the privilege axis called out as separate. Returns the picked name.
class PermissionModeDialog extends StatelessWidget {
  final PermissionMode state;
  const PermissionModeDialog({super.key, required this.state});

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final visibleOptions = state.options
        .where((o) =>
            o.name != PermissionMode.bypass ||
            state.canBypass ||
            state.mode == PermissionMode.bypass)
        .toList(growable: false);
    return AlertDialog(
      key: const Key('permission-mode-picker'),
      title: const Text('Autonomy mode'),
      contentPadding: const EdgeInsets.fromLTRB(0, SonderSpace.md, 0, 0),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final option in visibleOptions)
                InkWell(
                  key: Key('permission-mode-option-${option.name}'),
                  onTap: () => Navigator.of(context).pop(option.name),
                  child: Container(
                    width: double.infinity,
                    color: option.name == state.mode
                        ? cs.primary.withValues(alpha: 0.10)
                        : null,
                    padding: const EdgeInsets.symmetric(
                        horizontal: SonderSpace.xl, vertical: SonderSpace.snug),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Padding(
                          padding: const EdgeInsets.only(top: SonderSpace.xxs),
                          child: Icon(permissionModeIcon(option.name),
                              size: 16,
                              color: permissionModeColor(cs, option.name)),
                        ),
                        const SizedBox(width: SonderSpace.snug),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                option.displayLabel,
                                style: TextStyle(
                                  fontWeight: option.name == state.mode
                                      ? FontWeight.w700
                                      : FontWeight.w500,
                                  color: permissionModeColor(cs, option.name),
                                ),
                              ),
                              if (option.blurb.trim().isNotEmpty)
                                Text(option.blurb,
                                    style: TextStyle(
                                        fontSize: 12,
                                        color: cs.onSurfaceVariant)),
                            ],
                          ),
                        ),
                        if (isModeRaise(state.mode, option.name))
                          Padding(
                            padding: const EdgeInsets.only(left: SonderSpace.sm, top: SonderSpace.xxs),
                            child: Text('asks to confirm',
                                style: TextStyle(
                                    fontSize: 11, color: cs.onSurfaceVariant)),
                          ),
                        if (option.name == state.mode)
                          Icon(Icons.check, size: 18, color: cs.primary),
                      ],
                    ),
                  ),
                ),
              const Divider(height: 20),
              Padding(
                padding: const EdgeInsets.fromLTRB(SonderSpace.xl, 0, SonderSpace.xl, SonderSpace.sm),
                child: Text(
                  state.elevated
                      ? 'Privilege: elevated — a separate switch. No mode '
                          'grants it, and changing mode does not turn it off.'
                      : 'Privilege: normal. Elevation is a separate switch — '
                          'no mode grants it.',
                  style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
                ),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Close'),
        ),
      ],
    );
  }
}
