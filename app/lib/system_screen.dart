/// Compatibility entry point. The Runtime workspace lives in `runtime/`.
///
/// Kept so existing imports (`chat_screen.dart`, tests) do not change while
/// the screen is split into `lib/runtime/**` (plan P2-10).
library;

export 'runtime/runtime_screen.dart';
