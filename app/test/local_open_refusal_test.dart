// A local-open server refuses to approve a call or to raise the permission
// mode with a 403 whose message names what does work (the console, or a
// server restarted with an API key or an account requirement). Signing in
// does not lift it, so the app must show that message and not its own
// "needs a developer or admin account" advice.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/chat/backend.dart';

const _call = '3f9a12c0aa11bb22';
const _base = 'http://127.0.0.1:11435';

// The server's wording (facades/approval_routes.py, permission_mode_post.py).
const _approvalText =
    'approving a refused call over HTTP is refused: this server runs in '
    'local-open mode, so the request carries no credential and could come '
    'from any local process. Approve it at the Sonder console with /approve '
    '$_call, or restart the server with SONDER_API_KEY set and send that key, '
    'or with SONDER_REQUIRE_ACCOUNT=1 and send an administrator account token '
    '(an account token sent to a local-open server does not count, because '
    'the server still runs in local-open mode). Listing pending calls and '
    'revoking an approval are allowed here.';
const _modeText =
    'raising the permission mode from manual to auto is refused: this server '
    'runs in local-open mode, so the request carries no credential and could '
    'come from any local process. Run /mode auto at the Sonder console, or '
    'restart the server with SONDER_API_KEY set and send that key, or with '
    'SONDER_REQUIRE_ACCOUNT=1 and send an administrator account token (an '
    'account token sent to a local-open server does not count, because the '
    'server still runs in local-open mode). Lowering the mode, or returning '
    'from plan to manual, is allowed here.';

http.Response _refusal(String code, String message) => http.Response(
  jsonEncode({
    'error': {
      'code': code,
      'message': message,
      'type': 'permission_error',
      'retryable': false,
      'correlation_id': 'corr-1',
    },
    if (code == 'PERMISSION_MODE_ESCALATION_REFUSED') 'mode': 'manual',
  }),
  403,
  headers: {'content-type': 'application/json'},
);

Future<T> _against<T>(http.Response response, Future<T> Function() body) =>
    http.runWithClient(body, () => MockClient((r) async => response));

void main() {
  final approval = _refusal(
    'APPROVAL_REQUIRES_AUTHENTICATED_ADMIN',
    _approvalText,
  );
  final raise = _refusal('PERMISSION_MODE_ESCALATION_REFUSED', _modeText);

  test('approve: the local-open 403 keeps the server message', () async {
    await expectLater(
      _against(
        approval,
        () => SonderApi(baseUrl: _base).approvals.approve(_call),
      ),
      throwsA(
        isA<SonderException>()
            .having((e) => e.message, 'message', _approvalText)
            .having((e) => e.remedy, 'remedy', isEmpty)
            .having(
              (e) => e.code,
              'code',
              'APPROVAL_REQUIRES_AUTHENTICATED_ADMIN',
            ),
      ),
    );
  });

  test('approve: a role 403 still reads as a missing account', () async {
    await expectLater(
      _against(
        _refusal(
          'FORBIDDEN',
          'developer or administrator authorization is required',
        ),
        () => SonderApi(baseUrl: _base).approvals.approve(_call),
      ),
      throwsA(
        isA<SonderException>().having(
          (e) => e.message,
          'message',
          'Approvals need a developer or admin account.',
        ),
      ),
    );
  });

  test(
    'chat approve: the outcome carries the server message, not account advice',
    () async {
      final outcome = await _against(
        approval,
        () => SonderApiChatBackend(baseUrl: _base).approveCall(_call),
      );
      expect(outcome.status, ApprovalStatus.failed);
      expect(outcome.message, _approvalText);
    },
  );

  test(
    'setPermissionMode: the local-open 403 keeps the server message',
    () async {
      await expectLater(
        _against(
          raise,
          () => SonderApi(baseUrl: _base).setPermissionMode('auto'),
        ),
        throwsA(
          isA<SonderException>()
              .having((e) => e.message, 'message', _modeText)
              .having((e) => e.remedy, 'remedy', isEmpty),
        ),
      );
    },
  );

  test(
    'chat mode change: the backend passes the server message through',
    () async {
      await expectLater(
        _against(
          raise,
          () => SonderApiChatBackend(baseUrl: _base).setPermissionMode('auto'),
        ),
        throwsA(
          isA<SonderException>()
              .having((e) => e.message, 'message', _modeText)
              .having(
                (e) => e.code,
                'code',
                'PERMISSION_MODE_ESCALATION_REFUSED',
              ),
        ),
      );
    },
  );
}
