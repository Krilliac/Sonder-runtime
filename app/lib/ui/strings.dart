/// User-visible English strings for the shared components in `lib/ui/`.
///
/// The app is English-only for now (UX-CONTRACT.md, "Language"). New
/// components keep their copy here, as plain constants and small
/// functions, so moving to `gen-l10n` later is a mechanical step: every
/// entry maps to one ARB key, and functions map to ARB placeholders.
///
/// Wording rules (DESIGN.md "Notices & approvals"): name the effect before
/// the mechanism; a button says what it does ("Approve once", never "OK");
/// never add bypass hints to server wording.
abstract final class SonderStrings {
  // Generic actions.
  static const cancel = 'Cancel';
  static const retry = 'Retry';
  static const settings = 'Settings';
  static const copy = 'Copy';
  static const copied = 'Copied';
  static const stop = 'Stop';
  static const hintLabel = 'hint:';

  // Approval sheet (§2.5).
  static const approveOnce = 'Approve once';
  static const approveThisCallOnce = 'Approve this call once';
  static const retryTheRequest = 'Retry the request';
  static const revoke = 'Revoke';
  static const validFor = 'Valid for';
  static const approvalRefusedLabel = 'refused';
  static const approvalSheetLabel = 'Approve this call';

  /// The body of the approval sheet.
  static String approvalExplainer(Duration ttl, String mode) =>
      'Runs this exact call once, from any surface, within '
      '${durationWords(ttl)}. Changing any argument needs a new approval. '
      'Your mode stays $mode.';

  /// `write_file · call 3f9a12c0`
  static String approvalTitle(String tool, String callId) =>
      '$tool · call ${shortCallId(callId)}';

  /// The receipt after a successful approval.
  static String approvalReceipt(
          String tool, String callId, String? nonce, Duration ttl) =>
      '$tool call ${shortCallId(callId)} once'
      '${nonce == null || nonce.isEmpty ? '' : ' · nonce $nonce'}'
      ' · valid ${durationWords(ttl)}';

  /// Shown when the server has no HTTP approval endpoint (a 404), above the
  /// `/approve <id>` command (drawn in mono so it never wraps mid-command).
  static const approveFromConsole = 'Approve from the console:';

  /// The console command that approves [callId] (the full id, never the
  /// shortened one).
  static String approveCommand(String callId) => '/approve $callId';

  static const approvalsNeedRole =
      'Approvals need a developer or admin account';

  // Raise-mode sheet (§2.5, P0-4).
  static const raiseModeWord = 'raise mode';

  /// `manual → auto`
  static String raiseTitle(String from, String to) => '$from → $to';

  /// The effect of switching to [to], for every chat and agent on [host].
  static String raiseEffect(String to, String host) {
    final where = host.isEmpty ? 'this server' : host;
    return switch (to) {
      'acceptEdits' => 'File edits will run without asking, for every chat '
          'and agent on $where, until someone lowers the mode. Host programs '
          'still ask first.',
      'auto' => 'File changes and host programs will run without asking, '
          'for every chat and agent on $where, until someone lowers the mode.',
      _ => 'Sonder will ask less often before acting, for every chat and '
          'agent on $where, until someone lowers the mode.',
    };
  }

  static const raiseDestructiveNote = 'Destructive tools still need a person.';

  /// `Switch to auto`
  static String switchTo(String mode) => 'Switch to $mode';

  static const modeAdminOnly = 'Only an administrator can change the mode';

  // Status rows and notices.
  static const jumpToSection = 'Jump to section';

  /// "as of 12:40", for values kept after a failed refresh.
  static String asOf(String time) => 'as of $time';

  /// `3f9a12c0` from a longer id; ids of 8 characters or fewer are kept.
  static String shortCallId(String callId) =>
      callId.length <= 8 ? callId : callId.substring(0, 8);

  /// `15 minutes`, `1 hour`, `90 seconds`.
  static String durationWords(Duration d) {
    if (d.inSeconds < 60) {
      return '${d.inSeconds} second${d.inSeconds == 1 ? '' : 's'}';
    }
    if (d.inMinutes < 60 || d.inMinutes % 60 != 0) {
      return '${d.inMinutes} minute${d.inMinutes == 1 ? '' : 's'}';
    }
    return '${d.inHours} hour${d.inHours == 1 ? '' : 's'}';
  }

  /// `15 min`, `1 h`: the short form for a dropdown.
  static String durationShort(Duration d) {
    if (d.inMinutes < 60 || d.inMinutes % 60 != 0) return '${d.inMinutes} min';
    return '${d.inHours} h';
  }
}
