import 'dart:math';

/// A retry keeps this identity; every new command gets 128 random bits.
/// Byte-sized bounds also work in compiled JavaScript, where `1 << 32` is 0.
String newAgentCommandId() {
  final random = Random.secure();
  final bytes = List.generate(
      16, (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'));
  return 'ui-${bytes.join()}';
}
