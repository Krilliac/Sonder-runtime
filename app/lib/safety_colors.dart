import 'package:flutter/material.dart';

/// Shared safety vocabulary for command and autonomy surfaces.
///
/// These helpers deliberately return a neutral theme outline for values the
/// server has not published. Unknown policy must never inherit the meaning of
/// a known safe or permissive value.
Color riskColor(ColorScheme scheme, String risk) {
  switch (risk) {
    case 'safe':
      return const Color(0xFF4CAF50);
    case 'ask':
      return const Color(0xFFFFC107);
    case 'mutation':
      return const Color(0xFFFF7043);
    case 'execution':
      return const Color(0xFF8D6E63);
    case 'dangerous':
      return const Color(0xFFE53935);
    case 'unclassified':
      return const Color(0xFF757575);
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

/// Fill color for an autonomy mode. Green is reserved for the read-only plan
/// mode; unknown modes remain neutral rather than borrowing another meaning.
Color permissionModeColor(ColorScheme scheme, String mode) {
  switch (mode) {
    case 'plan':
      return const Color(0xFF1B5E20);
    case 'manual':
      return const Color(0xFF0D47A1);
    case 'acceptEdits':
      return const Color(0xFFBF360C);
    case 'auto':
      return const Color(0xFF6A1B9A);
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
