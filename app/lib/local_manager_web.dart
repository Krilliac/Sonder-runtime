import 'dart:convert';

import 'package:http/http.dart' as http;

import 'api/transport.dart' show requestGet, requestPost;
import 'local_manager_models.dart';

/// Browser clients use the authenticated server/host-launcher APIs, and
/// optionally delegate client-side PC tools to the loopback client companion
/// (`python -m sonder_runtime.interfaces.client_agent` on 127.0.0.1:11439).
class LocalManager {
  static const defaultClientAgentUrl = 'http://127.0.0.1:11439';
  static const unavailableMessage =
      'Local files and processes are unavailable in the browser. '
      'Use the desktop app for local setup, or configure an authenticated host launcher in Settings.';
  static bool get canRunLocalTools => false;
  static String get platformLabel => 'Web browser';
  static String sharedHomePath() => '';
  static String runDirectoryPath() => '';
  static String serverLogPath() => '';
  static String launcherHealthTokenPath() => '';
  static String managedServerOutputTail() => '';
  static Future<String> readServerLogTail({int maxLines = 40}) async => '';
  static Future<String> readLogTail(String path, {int maxLines = 40}) async =>
      '';
  static Map<String, String> processEnvironment(
          {bool allowHosted = false, String contextSize = '8192'}) =>
      const {};
  static Future<bool> defaultServerReachable() async => false;
  static Future<bool> waitForServer({
    Duration timeout = const Duration(seconds: 25),
    Duration interval = const Duration(milliseconds: 400),
    Future<bool> Function()? reachabilityProbe,
    Future<void> Function(Duration)? delay,
    DateTime Function()? clock,
    bool Function()? shouldAbort,
  }) async =>
      false;
  static Future<LocalInstallInfo> inspect() async => const LocalInstallInfo(
      platform: 'Web browser',
      appDir: '',
      systemDir: '',
      sharedHome: '',
      canLaunch: false,
      systemExists: false,
      gitCheckout: false,
      serverScript: false,
      trainingScript: false,
      bootstrapScript: false,
      engineBundle: false,
      defaultServerReachable: false);
  static Future<LocalActionResult> setupEngine(
          {bool allowHosted = false, String contextSize = '8192'}) async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> startServer({
    bool allowHosted = false,
    String contextSize = '8192',
    bool persistOnAppClose = false,
    Duration readyTimeout = const Duration(seconds: 25),
    Future<bool> Function()? managedReachabilityProbe,
    Future<bool> Function()? portOccupiedProbe,
  }) async =>
      const LocalActionResult(false, unavailableMessage);
  static void stopManagedServerNow() {}
  static void trackManagedServerForTest(Object process) {}
  static Future<LocalActionResult> stopServers() async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> startEndlessTraining() async =>
      const LocalActionResult(false, unavailableMessage);
  static Future<LocalActionResult> updateFromGit() async =>
      const LocalActionResult(false, unavailableMessage);

  /// A browser cannot start processes: this returns the unavailable result
  /// with the Observatory web URL to copy, when one is configured (contract
  /// section 10). [executable] is accepted so callers compile on both
  /// platforms; a browser never uses it.
  static Future<ObservatoryLaunchResult> launchObservatory(
    List<String> connectUrls, {
    String runtimeUrl = '',
    String executable = '',
    String webUrl = '',
  }) async {
    final urls = observatoryConnectUrls(connectUrls);
    final blocked =
        observatoryLaunchBlocked(runtimeUrl: runtimeUrl, connectUrls: urls);
    if (blocked != null) return blocked;
    final url = observatoryWebLaunchUrl(webUrl, urls) ?? '';
    return ObservatoryLaunchResult(
      ok: false,
      mode: ObservatoryLaunchMode.unavailable,
      message: url.isEmpty
          ? 'The browser cannot start the Observatory. Set an Observatory web '
              'URL in Settings to get a link, or copy the connect URLs.'
          : 'The browser cannot start the Observatory. Copy this link and '
              'open it in a new tab.',
      url: url,
    );
  }

  static Uri? _companionUri(String companionUrl, String path) {
    final raw = companionUrl.trim().isEmpty
        ? defaultClientAgentUrl
        : companionUrl.trim().replaceAll(RegExp(r'/+$'), '');
    final base = Uri.tryParse(raw);
    if (base == null ||
        !const {'http', 'https'}.contains(base.scheme.toLowerCase()) ||
        base.host.isEmpty ||
        !isLoopbackHost(base.host) ||
        base.userInfo.isNotEmpty ||
        base.hasQuery ||
        base.hasFragment ||
        (base.path.isNotEmpty && base.path != '/')) {
      return null;
    }
    return base.replace(path: path, query: null, fragment: null);
  }

  /// Probe the loopback client companion (`127.0.0.1:11439`) from the browser.
  static Future<ClientAgentHealth> checkClientAgent({
    String companionUrl = defaultClientAgentUrl,
    String token = '',
    String workspaceRoot = '',
  }) async {
    final healthUri = _companionUri(companionUrl, '/health');
    if (healthUri == null) {
      return const ClientAgentHealth(
        reachable: false,
        message: 'Client companion URL must be a loopback address (127.0.0.1).',
      );
    }
    try {
      final healthResp = await requestGet(
        healthUri,
        headers: const {'Accept': 'application/json'},
        timeout: const Duration(seconds: 3),
      );
      if (healthResp.statusCode != 200) {
        return ClientAgentHealth(
          reachable: false,
          message: 'Client companion returned HTTP ${healthResp.statusCode}.',
        );
      }
      final cleanToken = token.trim();
      if (cleanToken.isEmpty) {
        return const ClientAgentHealth(
          reachable: true,
          authenticated: false,
          message:
              'Client companion is running, but no companion token is configured in Settings.',
        );
      }
      final statusUri = _companionUri(companionUrl, '/v1/client-agent/status')!;
      final statusResp = await requestGet(
        statusUri,
        headers: {
          'Accept': 'application/json',
          'X-Sonder-Client-Agent-Token': cleanToken,
        },
        timeout: const Duration(seconds: 3),
      );
      if (statusResp.statusCode == 401) {
        return const ClientAgentHealth(
          reachable: true,
          authenticated: false,
          message: 'Client companion rejected the configured token (HTTP 401).',
        );
      }
      if (statusResp.statusCode != 200) {
        return ClientAgentHealth(
          reachable: true,
          authenticated: false,
          message:
              'Client companion status returned HTTP ${statusResp.statusCode}.',
        );
      }
      final obj = jsonDecode(utf8.decode(statusResp.bodyBytes));
      final map = obj is Map ? Map<String, dynamic>.from(obj) : const <String, dynamic>{};
      return ClientAgentHealth(
        reachable: true,
        authenticated: true,
        mode: map['mode']?.toString() ?? 'manual',
        workspace: map['workspace']?.toString() ?? '',
        canBypass: map['can_bypass'] != false,
        message: 'Connected to local client companion.',
      );
    } catch (_) {
      return ClientAgentHealth(
        reachable: false,
        message:
            'Local client companion is not running on ${healthUri.authority}.',
      );
    }
  }

  /// Sync client-local mode to the loopback companion when authenticated.
  static Future<bool> setClientAgentMode({
    required String mode,
    bool confirmBypass = false,
    String companionUrl = defaultClientAgentUrl,
    String token = '',
  }) async {
    final uri = _companionUri(companionUrl, '/v1/client-agent/mode');
    final cleanToken = token.trim();
    if (uri == null || cleanToken.isEmpty) return false;
    try {
      final resp = await requestPost(
        uri,
        headers: {
          'Content-Type': 'application/json',
          'X-Sonder-Client-Agent-Token': cleanToken,
        },
        body: jsonEncode({
          'mode': mode,
          if (confirmBypass) 'confirm_bypass': true,
        }),
        timeout: const Duration(seconds: 5),
      );
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  /// Execute a client tool via the loopback companion at `127.0.0.1:11439`.
  static Future<ClientToolExecResult> executeClientTool({
    required String callId,
    required String tool,
    required Map<String, dynamic> args,
    String risk = '',
    String mode = 'manual',
    String workspaceRoot = '',
    bool approved = false,
    String approvalToken = '',
    String argsDigest = '',
    String companionUrl = defaultClientAgentUrl,
    String companionToken = '',
  }) async {
    // A declared risk can raise the local classification, never lower it.
    final effectiveRisk = effectiveClientToolRisk(risk, tool);
    if (!approved && !clientModeAllowsWithoutPrompt(mode, effectiveRisk)) {
      return ClientToolExecResult(
        ok: false,
        needsApproval: true,
        error:
            "Approval required on your PC for '$tool' ($effectiveRisk) in $mode mode.",
      );
    }
    final uri = _companionUri(companionUrl, '/v1/client-agent/execute');
    if (uri == null) {
      return const ClientToolExecResult(
        ok: false,
        companionUnavailable: true,
        error: 'Client companion URL must be a loopback address (127.0.0.1).',
      );
    }
    final cleanToken = companionToken.trim();
    if (cleanToken.isEmpty) {
      return ClientToolExecResult(
        ok: false,
        companionUnavailable: true,
        error: 'Client companion token is missing. Start the companion and '
            'paste its token in Settings > Client PC:\n'
            '${clientCompanionLaunchCommand()}',
      );
    }
    final cleanApprovalToken = approvalToken.trim();
    final cleanArgsDigest = argsDigest.trim().toLowerCase();
    http.Response resp;
    try {
      resp = await requestPost(
        uri,
        headers: {
          'Content-Type': 'application/json',
          'X-Sonder-Client-Agent-Token': cleanToken,
        },
        body: jsonEncode({
          'call_id': callId,
          'tool': tool,
          'args': args,
          'approved': approved,
          'mode': mode,
          if (cleanApprovalToken.isNotEmpty)
            'approval_token': cleanApprovalToken,
          if (cleanArgsDigest.isNotEmpty) 'args_digest': cleanArgsDigest,
        }),
        timeout: const Duration(seconds: 120),
      );
    } catch (_) {
      return ClientToolExecResult(
        ok: false,
        companionUnavailable: true,
        error: 'Local client companion is not running at $companionUrl.\n'
            'Start it on your PC with:\n${clientCompanionLaunchCommand()}',
      );
    }
    Map<String, dynamic> obj;
    try {
      final decoded = jsonDecode(utf8.decode(resp.bodyBytes));
      obj = decoded is Map
          ? Map<String, dynamic>.from(decoded)
          : const <String, dynamic>{};
    } catch (_) {
      return ClientToolExecResult(
        ok: false,
        error: 'Invalid response from local client companion (HTTP ${resp.statusCode}).',
      );
    }
    if (resp.statusCode == 401 || resp.statusCode == 403) {
      final errObj = obj['error'];
      final msg = errObj is Map
          ? (errObj['message']?.toString() ?? 'Unauthorized')
          : 'Unauthorized';
      return ClientToolExecResult(
        ok: false,
        companionUnavailable: true,
        error: 'Local client companion refused request (HTTP ${resp.statusCode}): $msg',
      );
    }
    return ClientToolExecResult(
      ok: obj['ok'] == true,
      output: obj['output']?.toString() ?? '',
      error: obj['error']?.toString() ?? '',
      denied: obj['denied'] == true,
      needsApproval: obj['needs_approval'] == true,
      approvalToken: obj['approval_token']?.toString() ?? '',
      argsDigest: obj['args_digest']?.toString() ?? '',
    );
  }
}
