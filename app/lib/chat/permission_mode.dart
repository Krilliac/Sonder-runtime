import 'package:flutter/material.dart';

import '../api.dart';
import '../safety_colors.dart';
import '../theme.dart';
import 'permission_rules.dart';

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
        Tooltip(
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
                    height: 28,
                    padding: const EdgeInsets.fromLTRB(10, 0, 6, 0),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(SonderRadius.pill),
                      border: Border.all(color: tokens.hairlineStrong),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Container(
                          width: 7,
                          height: 7,
                          decoration: BoxDecoration(
                            color: tone,
                            borderRadius: BorderRadius.circular(4),
                          ),
                        ),
                        const SizedBox(width: 6),
                        Icon(permissionModeIcon(state.mode),
                            size: 13, color: tone),
                        const SizedBox(width: 5),
                        Text(modeLabel,
                            style: tokens.mono(12,
                                weight: FontWeight.w500,
                                color: readOnly ? tokens.text2 : tokens.text)),
                        if (busy)
                          Padding(
                            padding: const EdgeInsets.only(left: 6, right: 2),
                            child: SizedBox(
                              width: 11,
                              height: 11,
                              child: CircularProgressIndicator(
                                  strokeWidth: 2, color: tokens.text2),
                            ),
                          )
                        else if (readOnly)
                          Padding(
                            padding: const EdgeInsets.only(left: 4, right: 2),
                            child: Icon(Icons.lock_outline,
                                size: 13, color: tokens.muted),
                          )
                        else
                          Icon(Icons.expand_more, size: 16, color: tokens.muted),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
        if (state.elevated) ...[
          const SizedBox(width: 6),
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
          height: 28,
          padding: const EdgeInsets.symmetric(horizontal: 10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(SonderRadius.pill),
            border: Border.all(color: tokens.hairline),
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
          padding: const EdgeInsets.symmetric(horizontal: 8),
          decoration: BoxDecoration(
            color: tokens.dangerDim,
            borderRadius: BorderRadius.circular(SonderRadius.control),
            border: Border.all(color: tokens.danger, width: 1.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.shield_outlined, size: 13, color: tokens.danger),
              const SizedBox(width: 4),
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
    return AlertDialog(
      key: const Key('permission-mode-picker'),
      title: const Text('Autonomy mode'),
      contentPadding: const EdgeInsets.fromLTRB(0, 12, 0, 0),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final option in state.options)
                InkWell(
                  key: Key('permission-mode-option-${option.name}'),
                  onTap: () => Navigator.of(context).pop(option.name),
                  child: Container(
                    width: double.infinity,
                    color: option.name == state.mode
                        ? cs.primary.withValues(alpha: 0.10)
                        : null,
                    padding:
                        const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
                    child: Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Padding(
                          padding: const EdgeInsets.only(top: 2),
                          child: Icon(permissionModeIcon(option.name),
                              size: 16,
                              color: permissionModeColor(cs, option.name)),
                        ),
                        const SizedBox(width: 10),
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
                            padding: const EdgeInsets.only(left: 8, top: 2),
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
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
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

/// The raise sheet (§2.5): the person confirming a switch to a mode that
/// asks less. Returns true only on the explicit confirm button.
///
/// Lane B owns the shared presentational `lib/ui/raise_mode_sheet.dart`;
/// this is chat's copy of the same layout until that lands.
Future<bool> confirmModeRaise(
  BuildContext context, {
  required String from,
  required String to,
  required String host,
}) async {
  final wide = MediaQuery.sizeOf(context).width >= 600;
  Widget body(BuildContext ctx) => RaiseModeSheet(from: from, to: to, host: host);
  final bool? result;
  if (wide) {
    result = await showDialog<bool>(
      context: context,
      builder: (ctx) => Dialog(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 480),
          child: body(ctx),
        ),
      ),
    );
  } else {
    result = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (ctx) => SafeArea(child: body(ctx)),
    );
  }
  return result == true;
}

class RaiseModeSheet extends StatelessWidget {
  final String from;
  final String to;
  final String host;

  const RaiseModeSheet({
    super.key,
    required this.from,
    required this.to,
    required this.host,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final danger = to == 'auto';
    final tone = danger ? tokens.danger : tokens.warn;
    return Padding(
      key: const Key('raise-mode-sheet'),
      padding: const EdgeInsets.fromLTRB(20, 18, 20, 12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Semantics(
            header: true,
            label: 'warn: raise mode from $from to $to',
            child: ExcludeSemantics(
              child: Text.rich(TextSpan(children: [
                TextSpan(
                    text: '! raise mode   ',
                    style: tokens.mono(13,
                        color: tokens.warn, weight: FontWeight.w600)),
                TextSpan(
                    text: '$from → $to',
                    style: tokens.mono(13, color: tokens.text)),
              ])),
            ),
          ),
          const SizedBox(height: 14),
          Text(raiseEffect(to, host),
              style: text.bodyMedium?.copyWith(color: tokens.text2)),
          const SizedBox(height: 18),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(
                key: const Key('raise-mode-cancel'),
                onPressed: () => Navigator.of(context).pop(false),
                child: const Text('Cancel'),
              ),
              const SizedBox(width: 8),
              FilledButton(
                key: const Key('raise-mode-confirm'),
                style: FilledButton.styleFrom(
                  backgroundColor: tone,
                  foregroundColor: tokens.canvas,
                ),
                onPressed: () => Navigator.of(context).pop(true),
                child: Text('Switch to $to'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
