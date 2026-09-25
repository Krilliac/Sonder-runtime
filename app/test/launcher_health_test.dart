import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/launcher_health.dart';

// Proofs computed independently with Python's hmac/hashlib:
//   hmac.new(token, message, sha256).hexdigest()
// where message is the newline-joined contract below.
const _nonce =
    'abababababababababababababababababababababababababababababababab';
const _token = 'tttttttttttttttttttttttttttttttttttttttt'; // 40 chars
const _proof =
    'f6f43a8d494249d438c154a67585b641e5d303029d36b4917eda5b996a05ad2c';

/// A key longer than the 64-byte SHA-256 block exercises the key-hash path.
final _longToken = 'k' * 100;
const _longProof =
    'e433c42781fc9e809670746f8efe73ed2c0f5bdadda48029564337c24f005478';

Map<String, dynamic> payload({String proof = _proof}) => {
      'identity': 'sonder-launcher-health-v3',
      'service': 'sonder-serve',
      'version': 3,
      'role': launcherHealthManagedRole,
      'pid': 4321,
      'port': 11435,
      'nonce': _nonce,
      'proof': proof,
    };

bool accepts(Object? decoded,
        {String token = _token, String nonce = _nonce, int port = 11435}) =>
    launcherHealthPayloadMatches(decoded,
        token: token, nonce: nonce, port: port);

void main() {
  test('a genuine proof matches (HMAC-SHA256, short and long keys)', () {
    expect(accepts(payload()), isTrue);
    expect(accepts(payload(proof: _longProof), token: _longToken), isTrue);
  });

  test('any tampered field or wrong binding is rejected', () {
    for (final entry in <String, Object>{
      'identity': 'sonder-launcher-health-v2',
      'service': 'other',
      'version': 2,
      'role': 'worker',
      'pid': 0,
      'port': 1,
      'nonce': 'cd' * 32,
      'proof': 'f' * 64,
    }.entries) {
      final tampered = payload()..[entry.key] = entry.value;
      expect(accepts(tampered), isFalse, reason: entry.key);
    }
    expect(accepts(payload(), port: 11436), isFalse);
    expect(accepts(payload(), nonce: 'cd' * 32), isFalse);
    expect(accepts(payload(), token: '${_token}x'), isFalse);
  });

  test('shape rules: exact keys, typed values, strong token, hex nonce', () {
    expect(accepts(null), isFalse);
    expect(accepts(const ['not', 'a', 'map']), isFalse);
    expect(accepts(payload()..['extra'] = 1), isFalse);
    expect(accepts(payload()..remove('pid')), isFalse);
    expect(accepts(payload()..['pid'] = '4321'), isFalse);
    expect(accepts(payload()..['proof'] = 42), isFalse);
    expect(accepts(payload()..['proof'] = 'F' * 64), isFalse);
    expect(accepts(payload(), token: 'short'), isFalse);
    expect(accepts(payload(), nonce: 'xyz'), isFalse);
  });

  test('fresh tokens and nonces are random and well-formed', () {
    final tokens = {for (var i = 0; i < 8; i++) newLauncherHealthToken()};
    expect(tokens, hasLength(8));
    for (final token in tokens) {
      expect(token, matches(RegExp(r'^[A-Za-z0-9_-]{43}$')));
    }
    final nonce = newLauncherHealthNonce();
    expect(nonce, matches(RegExp(r'^[0-9a-f]{64}$')));
    expect(newLauncherHealthNonce(), isNot(nonce));
  });

  test('contract constants', () {
    expect(launcherHealthPath, '/v1/sonder/launcher-health');
    expect(launcherHealthNonceHeader, 'X-Sonder-Launcher-Health-Nonce');
  });
}
