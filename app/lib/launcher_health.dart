import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

const launcherHealthPath = '/v1/sonder/launcher-health';
const launcherHealthTokenEnvironment = 'SONDER_LAUNCHER_HEALTH_TOKEN';
const launcherHealthRoleEnvironment = 'SONDER_RUNTIME_ROLE';
const launcherHealthManagedRole = 'managed-host';
const launcherHealthNonceHeader = 'X-Sonder-Launcher-Health-Nonce';

String newLauncherHealthToken() =>
    base64UrlEncode(_randomBytes(32)).replaceAll('=', '');

String newLauncherHealthNonce() => _randomBytes(32)
    .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
    .join();

bool launcherHealthPayloadMatches(
  Object? decoded, {
  required String token,
  required String nonce,
  required int port,
}) {
  if (token.length < 32 ||
      !RegExp(r'^[0-9a-f]{64}$').hasMatch(nonce) ||
      decoded is! Map<String, dynamic> ||
      decoded.keys.toSet().difference(_payloadKeys).isNotEmpty ||
      _payloadKeys.difference(decoded.keys.toSet()).isNotEmpty ||
      decoded['identity'] != 'sonder-launcher-health-v3' ||
      decoded['service'] != 'sonder-serve' ||
      decoded['version'] != 3 ||
      decoded['role'] != launcherHealthManagedRole ||
      decoded['nonce'] != nonce ||
      decoded['port'] != port ||
      decoded['pid'] is! int ||
      (decoded['pid'] as int) <= 0 ||
      decoded['proof'] is! String ||
      !RegExp(r'^[0-9a-f]{64}$').hasMatch(decoded['proof'] as String)) {
    return false;
  }
  final message = 'contract=sonder-launcher-health-hmac-sha256\n'
      'identity=${decoded['identity']}\n'
      'service=${decoded['service']}\n'
      'version=${decoded['version']}\n'
      'role=${decoded['role']}\n'
      'pid=${decoded['pid']}\n'
      'port=${decoded['port']}\n'
      'nonce=${decoded['nonce']}';
  final expected = _hex(_hmacSha256(utf8.encode(token), utf8.encode(message)));
  return _constantTimeEquals(expected, decoded['proof'] as String);
}

const _payloadKeys = <String>{
  'identity',
  'service',
  'version',
  'role',
  'pid',
  'port',
  'nonce',
  'proof',
};

List<int> _randomBytes(int count) {
  final random = Random.secure();
  return List<int>.generate(count, (_) => random.nextInt(256));
}

bool _constantTimeEquals(String left, String right) {
  if (left.length != right.length) return false;
  var difference = 0;
  for (var i = 0; i < left.length; i++) {
    difference |= left.codeUnitAt(i) ^ right.codeUnitAt(i);
  }
  return difference == 0;
}

String _hex(List<int> bytes) =>
    bytes.map((byte) => byte.toRadixString(16).padLeft(2, '0')).join();

List<int> _hmacSha256(List<int> key, List<int> message) {
  const blockSize = 64;
  final normalized =
      key.length > blockSize ? _sha256(key) : List<int>.from(key);
  normalized.addAll(List<int>.filled(blockSize - normalized.length, 0));
  final innerPad = normalized.map((byte) => byte ^ 0x36).toList();
  final outerPad = normalized.map((byte) => byte ^ 0x5c).toList();
  return _sha256(<int>[
    ...outerPad,
    ..._sha256(<int>[...innerPad, ...message])
  ]);
}

List<int> _sha256(List<int> input) {
  final bytes = List<int>.from(input)..add(0x80);
  while (bytes.length % 64 != 56) {
    bytes.add(0);
  }
  final bitLength = input.length * 8;
  for (var shift = 56; shift >= 0; shift -= 8) {
    bytes.add((bitLength >> shift) & 0xff);
  }

  final hash = Uint32List.fromList(const <int>[
    0x6a09e667,
    0xbb67ae85,
    0x3c6ef372,
    0xa54ff53a,
    0x510e527f,
    0x9b05688c,
    0x1f83d9ab,
    0x5be0cd19,
  ]);
  final words = Uint32List(64);
  for (var offset = 0; offset < bytes.length; offset += 64) {
    for (var i = 0; i < 16; i++) {
      final start = offset + i * 4;
      words[i] = (bytes[start] << 24) |
          (bytes[start + 1] << 16) |
          (bytes[start + 2] << 8) |
          bytes[start + 3];
    }
    for (var i = 16; i < 64; i++) {
      final s0 = _rotateRight(words[i - 15], 7) ^
          _rotateRight(words[i - 15], 18) ^
          (words[i - 15] >> 3);
      final s1 = _rotateRight(words[i - 2], 17) ^
          _rotateRight(words[i - 2], 19) ^
          (words[i - 2] >> 10);
      words[i] = (words[i - 16] + s0 + words[i - 7] + s1) & 0xffffffff;
    }

    var a = hash[0];
    var b = hash[1];
    var c = hash[2];
    var d = hash[3];
    var e = hash[4];
    var f = hash[5];
    var g = hash[6];
    var h = hash[7];
    for (var i = 0; i < 64; i++) {
      final sum1 =
          _rotateRight(e, 6) ^ _rotateRight(e, 11) ^ _rotateRight(e, 25);
      final choose = (e & f) ^ ((~e) & g);
      final temp1 =
          (h + sum1 + choose + _roundConstants[i] + words[i]) & 0xffffffff;
      final sum0 =
          _rotateRight(a, 2) ^ _rotateRight(a, 13) ^ _rotateRight(a, 22);
      final majority = (a & b) ^ (a & c) ^ (b & c);
      final temp2 = (sum0 + majority) & 0xffffffff;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) & 0xffffffff;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) & 0xffffffff;
    }
    hash[0] = (hash[0] + a) & 0xffffffff;
    hash[1] = (hash[1] + b) & 0xffffffff;
    hash[2] = (hash[2] + c) & 0xffffffff;
    hash[3] = (hash[3] + d) & 0xffffffff;
    hash[4] = (hash[4] + e) & 0xffffffff;
    hash[5] = (hash[5] + f) & 0xffffffff;
    hash[6] = (hash[6] + g) & 0xffffffff;
    hash[7] = (hash[7] + h) & 0xffffffff;
  }

  return <int>[
    for (final value in hash)
      for (var shift = 24; shift >= 0; shift -= 8) (value >> shift) & 0xff,
  ];
}

int _rotateRight(int value, int count) =>
    ((value >> count) | (value << (32 - count))) & 0xffffffff;

const _roundConstants = <int>[
  0x428a2f98,
  0x71374491,
  0xb5c0fbcf,
  0xe9b5dba5,
  0x3956c25b,
  0x59f111f1,
  0x923f82a4,
  0xab1c5ed5,
  0xd807aa98,
  0x12835b01,
  0x243185be,
  0x550c7dc3,
  0x72be5d74,
  0x80deb1fe,
  0x9bdc06a7,
  0xc19bf174,
  0xe49b69c1,
  0xefbe4786,
  0x0fc19dc6,
  0x240ca1cc,
  0x2de92c6f,
  0x4a7484aa,
  0x5cb0a9dc,
  0x76f988da,
  0x983e5152,
  0xa831c66d,
  0xb00327c8,
  0xbf597fc7,
  0xc6e00bf3,
  0xd5a79147,
  0x06ca6351,
  0x14292967,
  0x27b70a85,
  0x2e1b2138,
  0x4d2c6dfc,
  0x53380d13,
  0x650a7354,
  0x766a0abb,
  0x81c2c92e,
  0x92722c85,
  0xa2bfe8a1,
  0xa81a664b,
  0xc24b8b70,
  0xc76c51a3,
  0xd192e819,
  0xd6990624,
  0xf40e3585,
  0x106aa070,
  0x19a4c116,
  0x1e376c08,
  0x2748774c,
  0x34b0bcb5,
  0x391c0cb3,
  0x4ed8aa4a,
  0x5b9cca4f,
  0x682e6ff3,
  0x748f82ee,
  0x78a5636f,
  0x84c87814,
  0x8cc70208,
  0x90befffa,
  0xa4506ceb,
  0xbef9a3f7,
  0xc67178f2,
];
