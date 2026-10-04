/// What Settings asks the network, and how it explains the answers: the
/// "Test connection" diagnosis and the account and launcher requests, all
/// through lane A's [SonderApi]. Tests substitute [SettingsConnection].
library;

import '../account_session.dart';
import '../api.dart';
import '../runtime/model_routing.dart';
import '../ui/status_vocab.dart';

/// What "Test connection" found, as one status word plus one-line remedy
/// (plan P1-3, P0-2).
enum ServerReachability {
  reachable('reachable', StatusKind.ok),
  refused('refused (421)', StatusKind.refused),
  needsHttps('needs HTTPS for sign-in', StatusKind.warn),
  unauthorized('needs a key', StatusKind.warn),
  rateLimited('wait', StatusKind.warn),
  unreachable('unreachable', StatusKind.fail),
  failed('error', StatusKind.fail);

  final String word;
  final StatusKind status;
  const ServerReachability(this.word, this.status);
}

class ConnectionDiagnosis {
  final ServerReachability state;
  final String title;
  final String detail;

  /// The PC-side setting that fixes a 421, e.g. `SONDER_ALLOWED_HOSTS=mypc`.
  final String? serverSetting;

  /// Android emulator hint for `10.0.2.2`.
  final String? adbHint;

  const ConnectionDiagnosis(this.state, this.title,
      {this.detail = '', this.serverSetting, this.adbHint});

  bool get ok => state == ServerReachability.reachable;
}

bool isLoopbackServerHost(String host) =>
    host == 'localhost' ||
    host == '::1' ||
    RegExp(r'^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$').hasMatch(host);

int? _statusOf(Object error) {
  if (error is SonderException) {
    if (error.httpStatus != null) return error.httpStatus;
    // Older transport builds only put the status in the message.
    final match = RegExp(r'HTTP (\d{3})').firstMatch(error.message);
    if (match != null) return int.parse(match.group(1)!);
  }
  return null;
}

/// A successful probe of [serverUrl].
///
/// With [routing] from the runtime's provider bindings, a route bound to
/// Sonder Inference is not counted as an Ollama model: [models] are split
/// into routes and exact models, which always run on Ollama.
ConnectionDiagnosis diagnoseReachable(String serverUrl,
    {int modelCount = 0,
    ModelRouting routing = const ModelRouting(),
    List<String> models = const []}) {
  final uri = Uri.tryParse(serverUrl.trim());
  final host = uri?.host ?? '';
  final count = modelCount == 1 ? '1 model' : '$modelCount models';
  if (uri != null && uri.scheme == 'http' && !isLoopbackServerHost(host)) {
    return ConnectionDiagnosis(
      ServerReachability.needsHttps,
      'Reachable at $host ($count), but sign-in needs HTTPS off this device.',
      detail: 'The API key is withheld over plain HTTP unless you allow this '
          'host below. For keys and accounts, serve the PC over HTTPS '
          '(Tailscale Serve or a TLS proxy that keeps the Host header).',
    );
  }
  final summary = routing.connectionSummary(models);
  return ConnectionDiagnosis(ServerReachability.reachable,
      'Connected to $host. ${summary ?? '$count available.'}');
}

/// A failed probe or sign-in, turned into what the person can do next.
ConnectionDiagnosis diagnoseConnectionError(Object error, String serverUrl) {
  final uri = Uri.tryParse(serverUrl.trim());
  final host = uri?.host.isNotEmpty == true ? uri!.host : serverUrl.trim();
  final port = uri?.hasPort == true ? uri!.port : 11435;
  final status = _statusOf(error);
  final code = error is SonderException ? error.code : '';
  if (status == 421 || code == 'HOST_NOT_ALLOWED') {
    final emulator = host == '10.0.2.2';
    return ConnectionDiagnosis(
      ServerReachability.refused,
      'Refused: the server at $host refused this address.',
      detail: "Connect with the PC's IP (or 127.0.0.1 with adb reverse), or "
          'add $host to [server].allowed_hosts / SONDER_ALLOWED_HOSTS on the '
          'PC and restart Sonder.',
      serverSetting: 'SONDER_ALLOWED_HOSTS=$host',
      adbHint: emulator
          ? 'Android emulator: run adb reverse tcp:$port tcp:$port, then use '
              'http://127.0.0.1:$port.'
          : null,
    );
  }
  if (status == 401 || status == 403) {
    return ConnectionDiagnosis(
      ServerReachability.unauthorized,
      'Reached $host, but it needs a valid API key or account.',
      detail: 'Paste the deployment API key from the PC, or sign in on the '
          'Account page.',
    );
  }
  if (status == 429) {
    final wait = error is SonderException ? error.retryAfterSeconds : null;
    return ConnectionDiagnosis(
      ServerReachability.rateLimited,
      wait == null
          ? 'Too many failed sign-ins from this network. Try again shortly.'
          : 'Too many failed sign-ins from this network. Try again in $wait s.',
    );
  }
  if (status != null) {
    return ConnectionDiagnosis(
        ServerReachability.failed, 'The server at $host answered HTTP $status.',
        detail: error is SonderException ? error.message : '');
  }
  if (error is ArgumentError) {
    return const ConnectionDiagnosis(
      ServerReachability.needsHttps,
      'Sign-in needs HTTPS off this device.',
      detail: 'Use an https:// server URL for accounts, or keep using the '
          'API key over the LAN.',
    );
  }
  return ConnectionDiagnosis(
    ServerReachability.unreachable,
    "Can't reach $host.",
    detail: 'Check that Sonder is running on the PC, that both devices are on '
        'the same network or tailnet, and that the port ($port) is right.',
  );
}

/// The first admin needs the bootstrap secret the server printed.
class BootstrapSecretRequired implements Exception {
  final String message;
  const BootstrapSecretRequired(this.message);
  @override
  String toString() => message;
}

/// Network actions Settings performs, all through lane A's [SonderApi].
/// Tests substitute a fake.
class SettingsConnection {
  const SettingsConnection();

  /// `GET /v1/models`: ids plus each row's routing field.
  Future<ModelCatalog> testServer(
          String serverUrl, String apiKey, AccountSession? account) =>
      SonderApi(baseUrl: serverUrl, apiKey: apiKey, accountSession: account)
          .modelCatalog();

  /// The runtime's provider bindings, or null when it cannot say (older
  /// runtime, non-administrator key, any failure). Only wording depends on
  /// it, so a failure never fails the connection test.
  Future<EcosystemStatus?> routingStatus(
      String serverUrl, String apiKey, AccountSession? account) async {
    try {
      final reading = await SonderApi(
              baseUrl: serverUrl, apiKey: apiKey, accountSession: account)
          .ecosystemStatus();
      return reading.status;
    } catch (_) {
      return null;
    }
  }

  /// `GET {launcher}/v1/launcher/status` with the launcher's own token.
  Future<LauncherStatus> launcherStatus(String launcherUrl, String token) =>
      SonderLauncherApi(baseUrl: launcherUrl, token: token).status();

  Future<String> login(
          String serverUrl, String apiKey, String username, String password) =>
      SonderApi(baseUrl: serverUrl, apiKey: apiKey).login(username, password);

  Future<void> logout(String apiKey, AccountSession account) => SonderApi(
          baseUrl: account.origin, apiKey: apiKey, accountSession: account)
      .logout();

  /// Returns "Account <u> created (role <r>)." on 200 or 201, through lane
  /// A's [SonderApi.register] (the secret travels only as
  /// `X-Sonder-Bootstrap-Secret`). A first-admin 403 becomes
  /// [BootstrapSecretRequired] so the card can ask for the secret.
  Future<String> register(
    String serverUrl,
    String apiKey,
    String username,
    String password, {
    String? bootstrapSecret,
  }) async {
    try {
      return await SonderApi(baseUrl: serverUrl, apiKey: apiKey).register(
          username, password,
          bootstrapSecret: bootstrapSecret?.trim() ?? '');
    } on SonderException catch (error) {
      if (error.needsBootstrapSecret) {
        throw BootstrapSecretRequired(error.message);
      }
      rethrow;
    }
  }
}
