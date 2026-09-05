import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'account_session.dart';
import 'agent_command_id.dart';
part 'app_work.dart';

/// Private composition snapshot. Never persisted or passed to the chat API.
class AppControlContext {
  final String serverUrl, deploymentKey;
  final AccountSession? account;
  const AppControlContext(
      {required this.serverUrl, required this.deploymentKey, this.account});
  String get origin {
    final value = serverOrigin(serverUrl);
    final path = Uri.parse(serverUrl.trim()).path;
    if (path.isNotEmpty && path != '/') {
      throw const AppControlFailure('CONTEXT_REQUIRED');
    }
    return value;
  }

  bool same(AppControlContext other) =>
      serverUrl == other.serverUrl &&
      deploymentKey == other.deploymentKey &&
      account?.origin == other.account?.origin &&
      account?.token == other.account?.token;
}

class AppControlFailure implements Exception {
  final String code;
  final bool unknown;
  final AppWorkApproval? approval;
  const AppControlFailure(this.code, {this.unknown = false, this.approval});
  @override
  String toString() => 'AppControlFailure($code)';
}

String _id(Object? value) {
  if (value is! String || !RegExp(r'^[A-Za-z0-9_-]{1,128}$').hasMatch(value)) {
    throw const AppControlFailure('INVALID_RESPONSE');
  }
  return value;
}

int _integer(Object? value) {
  if (value is! int || value < 0 || value > 9007199254740991) {
    throw const AppControlFailure('INVALID_RESPONSE');
  }
  return value;
}

String _label(Object? value) {
  if (value is! String ||
      utf8.encode(value).length > 256 ||
      value.runes.any((r) => r < 32 || (r >= 127 && r <= 159))) {
    throw const AppControlFailure('INVALID_LABEL');
  }
  return value;
}

DateTime _expiry(Object? value) {
  if (value is! num || !value.isFinite || value < 0 || value > 8640000000000) {
    throw const AppControlFailure('INVALID_RESPONSE');
  }
  return DateTime.fromMillisecondsSinceEpoch((value * 1000).round(),
      isUtc: true);
}

Map<String, dynamic> _object(Object? value) {
  if (value is! Map<String, dynamic>) {
    throw const AppControlFailure('INVALID_RESPONSE');
  }
  return value;
}

class AppConversationBinding {
  final String id, hostConversationId, project, title, localHistoryAlias;
  final int revision;
  final DateTime expiresAt;
  final bool revoked;
  const AppConversationBinding(
      {required this.id,
      required this.hostConversationId,
      required this.project,
      required this.title,
      required this.localHistoryAlias,
      required this.revision,
      required this.expiresAt,
      required this.revoked});
  factory AppConversationBinding.decode(Object? raw) {
    final value = _object(raw);
    final id = _id(value['binding_id']);
    if (value['host_conversation_id'] != 'app-session:$id' ||
        value['revoked'] is! bool) {
      throw const AppControlFailure('INVALID_RESPONSE');
    }
    return AppConversationBinding(
        id: id,
        hostConversationId: 'app-session:$id',
        project: _id(value['project']),
        title: _label(value['title']),
        localHistoryAlias: _label(value['local_history_alias']),
        revision: _integer(value['revision']),
        expiresAt: _expiry(value['expires_at']),
        revoked: value['revoked'] as bool);
  }
  String get displayTitle => title.isEmpty ? 'Untitled conversation' : title;
}

class AppControlSelection {
  final String id;
  final String? bindingId;
  final int epoch;
  final int? bindingRevision;
  const AppControlSelection(
      this.id, this.bindingId, this.epoch, this.bindingRevision);
}

class _Command {
  final String id, action, body;
  _Command.prepared(this.id, this.action, this.body);
  static _Command prepare(String action, Map<String, Object?> arguments) {
    final id = newAgentCommandId();
    return _Command.prepared(
        id, action, jsonEncode({'command_id': id, ...arguments}));
  }
}

class _Enrollment {
  final String id, project;
  final String? replacement;
  const _Enrollment(this.id, this.project, this.replacement);
}

/// Sole owner of a memory-only app-control bearer and dedicated transport.
/// No credential getter, serialization, storage hook, fallback or tool dispatch.
class AppControlClient extends ChangeNotifier {
  final AppControlContext Function() _context;
  final http.Client Function() _factory;
  final DateTime Function() _clock;
  late http.Client _http;
  late AppControlContext _scope;
  String? _token, _sessionId, _runtimeId;
  DateTime? _expiresAt;
  Timer? _expiryTimer;
  Completer<void>? _abort;
  _Enrollment? _enrollment;
  _Command? _pending;
  _Command? _workCommand;
  AppManagedWork? _work;
  AppControlSelection? _workScope;
  String _workPrompt = '';
  AppWorkApproval? _workApproval;
  bool _workExecutionUnknown = false;
  int _generation = 0;
  bool _disposed = false, _busy = false;
  String? _project;
  List<AppConversationBinding> _bindings = const [];
  AppControlSelection? _selection;
  bool selectionKnown = false;
  bool bindingsLoaded = false;
  int pagePosition = 0;
  int? nextPosition;
  AppControlClient(
      {required AppControlContext Function() context,
      http.Client Function()? transportFactory,
      DateTime Function()? clock})
      : _context = context,
        _factory = transportFactory ?? http.Client.new,
        _clock = clock ?? DateTime.now {
    _scope = _context();
    _http = _factory();
  }
  bool get busy => _busy;
  int get contextRevision => _generation;
  bool get hasSession =>
      !_disposed && _token != null && _expiresAt!.isAfter(_clock());
  bool get enrollmentPending => _enrollment != null;
  bool get mutationPending => _pending != null;
  String? get pendingAction => const {
        'bindings': 'create conversation',
        'select': 'select conversation',
        'clear': 'clear selection',
        'revoke': 'revoke conversation',
      }[_pending?.action];
  String? get project => _project ?? _enrollment?.project;
  String? get runtimeId => _runtimeId;
  DateTime? get expiresAt => _expiresAt;
  List<AppConversationBinding> get bindings => _bindings;
  AppControlSelection? get selection => _selection;
  bool get accountAvailable {
    try {
      final scope = _context();
      return scope.account?.matches(scope.origin) ?? false;
    } catch (_) {
      return false;
    }
  }

  String? get origin {
    try {
      return _context().origin;
    } catch (_) {
      return null;
    }
  }

  void synchronize() {
    if (_disposed) return;
    final current = _context();
    if (!_scope.same(current)) {
      _scope = current;
      _reset();
    }
    if (_expiresAt != null && !_expiresAt!.isAfter(_clock())) _reset();
  }

  void forget() => _reset();
  void _reset() {
    _generation++;
    _expiryTimer?.cancel();
    if (_abort != null && !_abort!.isCompleted) _abort!.complete();
    _abort = null;
    _token = _sessionId = _runtimeId = _project = null;
    _expiresAt = null;
    _enrollment = null;
    _pending = null;
    _clearWork();
    _bindings = const [];
    _selection = null;
    selectionKnown = false;
    bindingsLoaded = false;
    pagePosition = 0;
    nextPosition = null;
    _busy = false;
    _http.close();
    if (!_disposed) {
      _http = _factory();
      notifyListeners();
    }
  }

  void _check(int generation) {
    synchronize();
    if (_disposed || generation != _generation) {
      throw const AppControlFailure('CONTEXT_CHANGED');
    }
  }

  Future<T> _run<T>(Future<T> Function(int) operation) async {
    synchronize();
    if (_disposed || !accountAvailable) {
      throw const AppControlFailure('CONTEXT_REQUIRED');
    }
    if (_busy) throw const AppControlFailure('REQUEST_PENDING');
    final generation = _generation;
    _busy = true;
    notifyListeners();
    try {
      return await operation(generation);
    } on AppControlFailure catch (error) {
      if (generation == _generation &&
          const {'APP_CONTROL_AUTH_REQUIRED', 'APP_CONTROL_GRANT_CHANGED'}
              .contains(error.code)) {
        _reset();
      }
      rethrow;
    } finally {
      if (!_disposed && generation == _generation) {
        _busy = false;
        notifyListeners();
      }
    }
  }

  Future<Map<String, dynamic>> _request(
      int generation, String method, String path,
      {String? body,
      Map<String, String>? query,
      bool enrollment = false}) async {
    _check(generation);
    if (!enrollment && _enrollment != null) {
      throw const AppControlFailure('REQUEST_PENDING');
    }
    if (!enrollment && !hasSession) {
      throw const AppControlFailure('SESSION_REQUIRED');
    }
    final scope = _scope;
    final account = scope.account;
    if (account == null || !account.matches(scope.origin)) {
      throw const AppControlFailure('CONTEXT_REQUIRED');
    }
    if (scope.deploymentKey.length > 4096 ||
        scope.deploymentKey.codeUnits.any((c) => c < 33 || c > 126)) {
      throw const AppControlFailure('CONTEXT_REQUIRED');
    }
    final abort = Completer<void>();
    _abort = abort;
    final request = http.AbortableRequest(
        method,
        Uri.parse('${scope.origin}/v1/app-control/$path')
            .replace(queryParameters: query),
        abortTrigger: abort.future)
      ..followRedirects = false
      ..headers['X-Sonder-Account-Token'] = account.token
      ..headers['Accept'] = 'application/json';
    if (scope.deploymentKey.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer ${scope.deploymentKey}';
    }
    if (!enrollment) request.headers['X-Sonder-App-Control'] = _token!;
    if (body != null) {
      if (utf8.encode(body).length > 16384) {
        throw const AppControlFailure('INVALID_REQUEST');
      }
      request.headers['Content-Type'] = 'application/json';
      request.body = body;
    }
    try {
      final response = await (() async {
        final response = await _http.send(request);
        final bytes = <int>[];
        await for (final chunk in response.stream) {
          if (bytes.length + chunk.length > 262144) {
            throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
          }
          bytes.addAll(chunk);
        }
        return (response.statusCode, bytes);
      })()
          .timeout(const Duration(seconds: 20));
      _check(generation);
      Map<String, dynamic> value;
      try {
        value = _object(jsonDecode(utf8.decode(response.$2)));
      } catch (_) {
        throw AppControlFailure('INVALID_RESPONSE', unknown: method == 'POST');
      }
      if (response.$1 >= 300 || response.$1 < 200 || value['ok'] != true) {
        final rawCode =
            value['error'] is Map ? (value['error'] as Map)['code'] : null;
        const codes = {
          'APP_CONTROL_AUTH_REQUIRED',
          'APP_CONTROL_REFUSED',
          'APP_CONTROL_TRANSPORT_REFUSED',
          'APP_BINDING_NOT_FOUND',
          'APP_CONTROL_ROUTE_NOT_FOUND',
          'APP_CONTROL_CONFLICT',
          'APP_CONTROL_GRANT_CHANGED',
          'CREDENTIAL_DELIVERY_UNKNOWN',
          'APP_CONTROL_BUSY',
          'APP_CONTROL_CAPACITY',
          'APP_CONTROL_UNAVAILABLE',
          'APP_CONTROL_OUTCOME_UNKNOWN',
          'APP_RECOVERY_UNAVAILABLE',
          'APP_WORK_UNAVAILABLE',
          'APP_WORK_BUSY',
          'APP_WORK_APPROVAL_PENDING',
          'APP_WORK_APPROVAL_UNKNOWN',
          'INVALID_APP_CONTROL_REQUEST'
        };
        final code =
            codes.contains(rawCode) ? rawCode as String : 'INVALID_RESPONSE';
        if (code == 'APP_WORK_APPROVAL_PENDING' && response.$1 == 409) {
          throw AppControlFailure(code,
              approval: AppWorkApproval.decode(value['pending']));
        }
        throw AppControlFailure(code,
            unknown: method == 'POST' &&
                (response.$1 >= 500 ||
                    response.$1 < 200 ||
                    response.$1 >= 300 && response.$1 < 400));
      }
      if (enrollment && response.$1 != 201) {
        throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
      }
      return value;
    } on AppControlFailure {
      rethrow;
    } catch (_) {
      _check(generation);
      // Close the exact dedicated transport after timeout/network failure. No
      // automatic replay; retained prepared identity is the only retry path.
      _http.close();
      _http = _factory();
      throw AppControlFailure('CONNECTION_UNKNOWN', unknown: method == 'POST');
    } finally {
      if (!abort.isCompleted) abort.complete();
      if (identical(_abort, abort)) _abort = null;
    }
  }

  Future<void> enroll(
      {required String project,
      required String password,
      bool replace = false}) {
    if (_pending != null || _enrollment != null || _workCommand != null) {
      throw const AppControlFailure('REQUEST_PENDING');
    }
    if (hasSession && !replace) throw const AppControlFailure('SESSION_EXISTS');
    final prepared = _Enrollment(
        newAgentCommandId(), _id(project), replace ? _sessionId : null);
    return _enroll(prepared, password);
  }

  Future<void> reconcileEnrollment({required String password}) {
    final pending = _enrollment;
    if (pending == null) throw const AppControlFailure('NOTHING_PENDING');
    return _enroll(pending, password);
  }

  Future<void> _enroll(_Enrollment prepared, String password) =>
      _run((generation) async {
        if (password.isEmpty || utf8.encode(password).length > 4096) {
          throw const AppControlFailure('PASSWORD_REQUIRED');
        }
        _enrollment = prepared;
        if (prepared.replacement != null) {
          _token = null;
          _expiresAt = null;
          _expiryTimer?.cancel();
          _selection = null;
          selectionKnown = false;
          _bindings = const [];
          bindingsLoaded = false;
        }
        try {
          final value = await _request(generation, 'POST', 'enroll',
              enrollment: true,
              body: jsonEncode({
                'command_id': prepared.id,
                'project': prepared.project,
                'password': password,
                if (prepared.replacement != null)
                  'replace_session_id': prepared.replacement
              }));
          final session = _id(value['control_session_id']);
          final token = value['control_token'];
          final runtime = _id(value['runtime_id']);
          final expires = _expiry(value['expires_at']);
          if (!RegExp(r'^[0-9a-f]{32}$').hasMatch(session) ||
              token is! String ||
              !RegExp('^sac1\\.$session\\.[A-Za-z0-9_-]{43}\$')
                  .hasMatch(token) ||
              !expires.isAfter(_clock())) {
            throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
          }
          _check(generation);
          _token = token;
          _sessionId = session;
          _runtimeId = runtime;
          _expiresAt = expires;
          _project = prepared.project;
          _enrollment = null;
          _bindings = const [];
          _selection = null;
          selectionKnown = true;
          pagePosition = 0;
          nextPosition = null;
          _expiryTimer?.cancel();
          _expiryTimer = Timer(expires.difference(_clock()), () {
            if (!_disposed) synchronize();
          });
        } on AppControlFailure catch (error) {
          if (const {'INVALID_RESPONSE', 'INVALID_LABEL'}
              .contains(error.code)) {
            throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
          }
          if (generation == _generation && !error.unknown) _enrollment = null;
          rethrow;
        }
      });

  Future<void> loadBindings({int afterPosition = 0}) =>
      _run((generation) async {
        _integer(afterPosition);
        final value = await _request(generation, 'GET', 'bindings',
            query: {'after_position': '$afterPosition', 'limit': '50'});
        final raw = value['items'];
        if (raw is! List || raw.length > 50) {
          throw const AppControlFailure('INVALID_RESPONSE');
        }
        final items =
            raw.map(AppConversationBinding.decode).toList(growable: false);
        if (items.any((item) => item.project != _project) ||
            items.map((b) => b.id).toSet().length != items.length) {
          throw const AppControlFailure('INVALID_RESPONSE');
        }
        final next = value['next_position'] == null
            ? null
            : _integer(value['next_position']);
        if (next != null && next <= afterPosition) {
          throw const AppControlFailure('INVALID_RESPONSE');
        }
        _check(generation);
        _bindings = List.unmodifiable(items);
        bindingsLoaded = true;
        pagePosition = afterPosition;
        nextPosition = next;
      });
  Future<void> loadSelection() => _run((generation) async {
        selectionKnown = false;
        final value = await _request(generation, 'GET', 'selection');
        final raw = value['selection'];
        AppControlSelection? next;
        if (raw != null) {
          final data = _object(raw);
          if ((data['binding_id'] == null) !=
              (data['binding_revision'] == null)) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          next = AppControlSelection(
              _id(data['selection_id']),
              data['binding_id'] == null ? null : _id(data['binding_id']),
              _integer(data['epoch']),
              data['binding_revision'] == null
                  ? null
                  : _integer(data['binding_revision']));
        }
        _check(generation);
        _selection = next;
        selectionKnown = true;
      });
  Future<void> createBinding(
          {String title = '', String localHistoryAlias = ''}) =>
      _mutate(_Command.prepare('bindings', {
        'title': _label(title),
        'local_history_alias': _label(localHistoryAlias)
      }));
  Future<void> selectBinding(AppConversationBinding binding) {
    if (!selectionKnown) throw const AppControlFailure('REFRESH_REQUIRED');
    _validateBinding(binding);
    return _mutate(_Command.prepare('select', {
      'binding_id': binding.id,
      'expected_binding_revision': binding.revision,
      'expected_epoch': _selection?.epoch ?? 0
    }));
  }

  Future<void> clearSelection() {
    if (!selectionKnown) throw const AppControlFailure('REFRESH_REQUIRED');
    return _mutate(
        _Command.prepare('clear', {'expected_epoch': _selection?.epoch ?? 0}));
  }

  Future<void> revokeBinding(AppConversationBinding binding) {
    _validateBinding(binding, allowExpired: true);
    return _mutate(_Command.prepare('revoke',
        {'binding_id': binding.id, 'expected_revision': binding.revision}));
  }

  void _validateBinding(AppConversationBinding binding,
      {bool allowExpired = false}) {
    _id(binding.id);
    _integer(binding.revision);
    if (binding.project != _project ||
        binding.revoked ||
        (!allowExpired && !binding.expiresAt.isAfter(_clock()))) {
      throw const AppControlFailure('REFRESH_REQUIRED');
    }
  }

  Future<void> retryMutation() {
    if (_pending == null) throw const AppControlFailure('NOTHING_PENDING');
    return _mutate(_pending!, retry: true);
  }

  Future<void> _mutate(_Command command, {bool retry = false}) =>
      _run((generation) async {
        if (_workCommand != null) {
          throw const AppControlFailure('REQUEST_PENDING');
        }
        if (_pending != null && (!retry || !identical(command, _pending))) {
          throw const AppControlFailure('REQUEST_PENDING');
        }
        _pending = command;
        try {
          final value = await _request(generation, 'POST', command.action,
              body: command.body);
          final receipt = _object(value['receipt']);
          const actions = {
            'bindings': 'create_binding',
            'select': 'select_binding',
            'clear': 'clear_selection',
            'revoke': 'revoke_binding'
          };
          if (receipt['command_id'] != command.id ||
              receipt['result_code'] != 'COMMITTED' ||
              receipt['action'] != actions[command.action]) {
            throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
          }
          _id(receipt['entity_id']);
          if (command.action == 'clear') {
            if (receipt['entity_revision'] != null) {
              throw const AppControlFailure('INVALID_RESPONSE');
            }
          } else {
            _integer(receipt['entity_revision']);
          }
          if (command.action == 'clear' || command.action == 'select') {
            _integer(receipt['selection_epoch']);
          } else if (receipt['selection_epoch'] != null) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          final arguments = jsonDecode(command.body) as Map;
          if ((command.action == 'select' || command.action == 'clear') &&
              receipt['selection_epoch'] != arguments['expected_epoch'] + 1) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          if (command.action == 'select' &&
              receipt['entity_revision'] !=
                  arguments['expected_binding_revision']) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          if (command.action == 'revoke' &&
              (receipt['entity_id'] != arguments['binding_id'] ||
                  receipt['entity_revision'] !=
                      arguments['expected_revision'] + 1)) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          _check(generation);
          _pending = null;
          // Receipts acknowledge effects, but a fresh projection owns visible state.
          selectionKnown = false;
        } on AppControlFailure catch (error) {
          if (const {'INVALID_RESPONSE', 'INVALID_LABEL'}
              .contains(error.code)) {
            selectionKnown = false;
            throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
          }
          if (generation == _generation && !error.unknown) _pending = null;
          if (generation == _generation) selectionKnown = false;
          rethrow;
        }
      });
  @override
  void dispose() {
    if (_disposed) return;
    _disposed = true;
    _reset();
    super.dispose();
  }
}
