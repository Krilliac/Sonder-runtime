/// Permission-mode rules chat uses (P0-4). The rules themselves are lane B's
/// (`lib/ui/status_vocab.dart`), which resolve a mode name exactly as the
/// server does, so `/mode AUTO` or `/mode au` cannot skip the raise sheet.
library;

import '../ui/strings.dart';

export '../ui/status_vocab.dart'
    show isModeRaise, modeBlurbs, permissionModes, resolvePermissionMode;

const modeReadOnlyText = SonderStrings.modeAdminOnly;
