/// Pure permission-mode rules shared by the controller, the chip and the
/// raise sheet (P0-4).
library;

const modeReadOnlyText = 'Only an administrator can change the mode';

const _modeRank = <String, int>{
  'plan': 0,
  'manual': 1,
  'acceptEdits': 2,
  'auto': 3,
};

/// True when moving [from] -> [to] lets more happen without asking:
/// manual -> acceptEdits, manual -> auto, acceptEdits -> auto (and the same
/// from plan). Lowering never needs a sheet. A mode this app does not know
/// is treated as a raise: confirming an unknown switch costs one tap,
/// skipping a real raise costs a person's consent.
bool isModeRaise(String from, String to) {
  if (from == to) return false;
  final a = _modeRank[from];
  final b = _modeRank[to];
  if (b == null) return true;
  if (a == null) return b >= 2;
  return b > a && b >= 2;
}

/// The REPL's mode blurbs (APP-PLAN §2.1).
String modeBlurb(String mode) => switch (mode) {
      'plan' => 'reads only — no changes',
      'manual' => 'asks before changes',
      'acceptEdits' => 'file edits run without asking',
      'auto' => 'edits and programs run without asking',
      _ => '',
    };

/// What the raise sheet says will happen, effect before mechanism.
String raiseEffect(String to, String host) {
  final where = host.isEmpty ? 'this server' : host;
  return switch (to) {
    'acceptEdits' =>
      'File changes will run without asking, for every chat and agent on '
          '$where, until someone lowers the mode. Host programs still ask.',
    'auto' =>
      'File changes and host programs will run without asking, for every '
          'chat and agent on $where, until someone lowers the mode. '
          'Destructive tools still need a person.',
    _ => 'Sonder will ask less often, for every chat and agent on $where, '
        'until someone lowers the mode.',
  };
}
