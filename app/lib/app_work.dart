part of 'app_control.dart';

String _digest(Object? value) {
  if (value is! String || !RegExp(r'^[0-9a-f]{64}$').hasMatch(value)) {
    throw const AppControlFailure('INVALID_RESPONSE');
  }
  return value;
}

class AppWorkApproval {
  final String callId, digest;
  final DateTime expiresAt;
  const AppWorkApproval(this.callId, this.digest, this.expiresAt);
  factory AppWorkApproval.decode(Object? raw) {
    final value = _object(raw);
    final digest = _digest(value['call_digest']);
    if (value['tool'] != 'workspace_run' ||
        value['surface'] != 'app-control' ||
        value['call_id'] != digest.substring(0, 16)) {
      throw const AppControlFailure('INVALID_RESPONSE');
    }
    return AppWorkApproval(
        digest.substring(0, 16), digest, _expiry(value['expires_at']));
  }
}

class AppManagedWork {
  final String id, state, project, tier;
  final int revision, maxSteps;
  final bool allowWeb, allowLocation;
  final DateTime expiresAt;
  final AppWorkApproval? verificationApproval;
  final Map<String, Object?>? verificationIdentity;
  final String? completion;
  const AppManagedWork(
      {required this.id,
      required this.state,
      required this.project,
      required this.tier,
      required this.revision,
      required this.maxSteps,
      required this.allowWeb,
      required this.allowLocation,
      required this.expiresAt,
      this.verificationApproval,
      this.verificationIdentity,
      this.completion});
  factory AppManagedWork.decode(Object? raw) {
    final value = _object(raw);
    final options = _object(value['options']);
    const states = {
      'prepared',
      'admitted',
      'run_binding',
      'running',
      'verification_pending',
      'unknown',
      'terminal'
    };
    final steps = _integer(options['max_steps']);
    if (!states.contains(value['state']) ||
        steps < 1 ||
        steps > 100 ||
        options['allow_web'] is! bool ||
        options['allow_location'] is! bool) {
      throw const AppControlFailure('INVALID_RESPONSE');
    }
    AppWorkApproval? pending;
    Map<String, Object?>? pendingIdentity;
    if (value['pending'] != null) {
      final p = _object(value['pending']);
      if (p['kind'] != 'verification_approval') {
        throw const AppControlFailure('INVALID_RESPONSE');
      }
      final identity = _object(p['identity']);
      for (final key in ['bundle_digest', 'projection_digest']) {
        _digest(identity[key]);
      }
      for (final key in ['continuation_id', 'verification_id', 'command_id']) {
        _id(identity[key]);
      }
      if (identity['parent_session_id'] is! String ||
          (identity['parent_session_id'] as String).length > 256) {
        throw const AppControlFailure('INVALID_RESPONSE');
      }
      for (final key in [
        'generation',
        'parent_grant_revision',
        'projection_revision'
      ]) {
        _integer(identity[key]);
      }
      pendingIdentity = Map.unmodifiable({
        for (final key in [
          'continuation_id',
          'verification_id',
          'parent_session_id',
          'parent_grant_revision',
          'generation',
          'bundle_digest',
          'command_id',
          'projection_digest',
          'projection_revision'
        ])
          key: identity[key]
      });
      pending = AppWorkApproval.decode(p['approval']);
    }
    String? completion;
    if (value['completion'] != null) {
      final c = _object(value['completion'])['phase'];
      if (!const {
            'not_required',
            'refused',
            'certified',
            'certified_after_return'
          }.contains(c) ||
          value['state'] != 'terminal') {
        throw const AppControlFailure('INVALID_RESPONSE');
      }
      completion = c as String;
    }
    return AppManagedWork(
        id: _digest(value['work_id']),
        state: value['state'] as String,
        project: _id(value['project']),
        tier: _id(options['tier']),
        revision: _integer(value['revision']),
        maxSteps: steps,
        allowWeb: options['allow_web'] as bool,
        allowLocation: options['allow_location'] as bool,
        expiresAt: _expiry(value['expires_at']),
        verificationApproval: pending,
        verificationIdentity: pendingIdentity,
        completion: completion);
  }
}

extension AppControlWork on AppControlClient {
  AppManagedWork? get work => _work;
  String get workPrompt => _workPrompt;
  AppWorkApproval? get workApproval => _workApproval;
  bool get workPreparationPending => _workCommand != null;
  bool get workExecutionUnknown => _workExecutionUnknown;
  bool get workScopeCurrent =>
      hasSession && selectionKnown && _sameWorkScope(_selection, _workScope);
  bool _sameWorkScope(AppControlSelection? a, AppControlSelection? b) =>
      a != null &&
      b != null &&
      a.bindingId != null &&
      a.id == b.id &&
      a.epoch == b.epoch &&
      a.bindingId == b.bindingId &&
      a.bindingRevision == b.bindingRevision;
  void _clearWork() {
    _workCommand = null;
    _work = null;
    _workScope = null;
    _workPrompt = '';
    _workApproval = null;
    _workExecutionUnknown = false;
  }

  void _requireWorkScope() {
    if (!workScopeCurrent) {
      throw const AppControlFailure('WORK_SELECTION_CHANGED');
    }
    if (_pending != null) throw const AppControlFailure('REQUEST_PENDING');
  }

  Future<void> prepareWork({required String prompt}) {
    synchronize();
    if (_workCommand != null ||
        _work != null ||
        _pending != null ||
        !selectionKnown ||
        _selection?.bindingId == null) {
      throw const AppControlFailure('REQUEST_PENDING');
    }
    if (prompt.trim().isEmpty || utf8.encode(prompt).length > 8000) {
      throw const AppControlFailure('INVALID_REQUEST');
    }
    _workScope = _selection;
    _workPrompt = prompt;
    _workCommand = _Command.prepare('work', {
      'prompt': prompt,
      'tier': 'auto',
      'max_steps': 8,
      'allow_web': false,
      'allow_location': false
    });
    return retryWorkPreparation();
  }

  Future<void> retryWorkPreparation() => _run((generation) async {
        _requireWorkScope();
        final command = _workCommand;
        if (command == null) throw const AppControlFailure('NOTHING_PENDING');
        final value =
            await _request(generation, 'POST', 'work', body: command.body);
        try {
          final next = AppManagedWork.decode(value['work']);
          final receipt = _object(value['receipt']);
          if (next.project != project ||
              next.tier != 'auto' ||
              next.maxSteps != 8 ||
              next.allowWeb ||
              next.allowLocation ||
              receipt['command_id'] != command.id ||
              receipt['action'] != 'prepare_work' ||
              receipt['result_code'] != 'COMMITTED' ||
              receipt['entity_id'] != next.id ||
              receipt['entity_revision'] != 1 ||
              receipt['selection_epoch'] != null) {
            throw const AppControlFailure('INVALID_RESPONSE');
          }
          _work = next;
          _workCommand = null;
        } catch (_) {
          throw const AppControlFailure('INVALID_RESPONSE', unknown: true);
        }
      });
  Future<void> executeWork() => _run((generation) async {
        _requireWorkScope();
        final current = _work;
        if (current == null ||
            current.state != 'prepared' ||
            _workExecutionUnknown ||
            _workCommand != null) {
          throw const AppControlFailure('WORK_NOT_DISPATCHABLE');
        }
        try {
          final value = await _request(
              generation, 'POST', 'work/${current.id}/execute',
              body: '{}');
          _acceptWork(value, current);
          _workApproval = null;
        } on AppControlFailure catch (error) {
          if (error.approval != null) {
            _workApproval = error.approval;
          } else if (error.unknown || error.code == 'INVALID_RESPONSE') {
            _workExecutionUnknown = true;
          }
          rethrow;
        }
      });
  void _acceptWork(Map<String, dynamic> value, AppManagedWork current) {
    final next = AppManagedWork.decode(value['work']);
    if (next.id != current.id ||
        next.project != current.project ||
        next.revision < current.revision ||
        next.revision == current.revision && next.state != current.state ||
        current.state != 'prepared' && next.state == 'prepared' ||
        next.tier != current.tier ||
        next.maxSteps != current.maxSteps ||
        next.allowWeb != current.allowWeb ||
        next.allowLocation != current.allowLocation ||
        next.expiresAt != current.expiresAt) {
      throw const AppControlFailure('INVALID_RESPONSE');
    }
    _work = next;
    if (next.state != 'prepared') _workApproval = null;
  }

  Future<void> refreshWork() => _run((generation) async {
        _requireWorkScope();
        final current = _work;
        if (current == null) throw const AppControlFailure('NOTHING_PENDING');
        final value = await _request(generation, 'GET', 'work/${current.id}');
        _acceptWork(value, current);
      });
}
