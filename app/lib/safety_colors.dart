import 'package:flutter/material.dart';

import 'theme.dart';

/// Shared safety vocabulary for command and autonomy surfaces.
///
/// These helpers deliberately return a neutral theme outline for values the
/// server has not published. Unknown policy must never inherit the meaning of
/// a known safe or permissive value. Every colour here is paired with a
/// label or a glyph by its callers, so meaning is never carried by colour
/// alone.
Color riskColor(ColorScheme scheme, String risk) {
  final tokens = scheme.brightness == Brightness.dark
      ? SonderTokens.dark
      : SonderTokens.light;
  switch (risk) {
    case 'safe':
      return tokens.ok;
    case 'ask':
      return tokens.warn;
    case 'mutation':
      return tokens.mutation;
    case 'execution':
      return tokens.execution;
    case 'dangerous':
      return tokens.danger;
    case 'unclassified':
      return tokens.muted;
    default:
      return scheme.outline;
  }
}

/// Human wording for a command risk band, suitable for tooltips and semantics.
String riskLabel(String risk) {
  switch (risk) {
    case 'safe':
      return 'Safe — read only';
    case 'ask':
      return 'Acts beyond a read — prompts depend on the mode';
    case 'mutation':
      return 'Changes files or state';
    case 'execution':
      return 'Runs a program on the host';
    case 'dangerous':
      return 'Dangerous — destructive';
    case 'unclassified':
      return 'Unclassified — refused until policy is defined';
    default:
      return 'Risk not published';
  }
}

/// The tone of an autonomy mode: the dot beside its label, never a fill
/// behind white text. Green is reserved for the read-only plan mode; unknown
/// modes remain neutral rather than borrowing another meaning.
Color permissionModeColor(ColorScheme scheme, String mode) {
  final tokens = scheme.brightness == Brightness.dark
      ? SonderTokens.dark
      : SonderTokens.light;
  switch (mode) {
    case 'plan':
      return tokens.ok;
    case 'manual':
      return tokens.info;
    case 'acceptEdits':
      return tokens.warn;
    case 'auto':
      return tokens.auto;
    default:
      return scheme.outline;
  }
}

/// A glyph per autonomy mode so meaning is never carried by color alone.
IconData permissionModeIcon(String mode) {
  switch (mode) {
    case 'plan':
      return Icons.visibility_outlined;
    case 'manual':
      return Icons.pan_tool_outlined;
    case 'acceptEdits':
      return Icons.edit_outlined;
    case 'auto':
      return Icons.fast_forward_outlined;
    default:
      return Icons.help_outline;
  }
}
