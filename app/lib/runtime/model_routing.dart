/// Which provider serves a model id the app offers, read from the runtime's
/// provider bindings (`GET /v1/sonder/ecosystem`).
///
/// Runtime contract (`provider_bridge.provider_for_tier`): a route name
/// (`sonder` or a tier such as `general`) follows the tier binding, but an
/// exact model pin (any `/v1/models` id that is not a route name) always
/// runs on Ollama, even when every tier is bound to Sonder Inference. The
/// labels here make that visible; with no document, or with everything on
/// Ollama, they are the labels the app always showed.
library;

import 'ecosystem.dart';

/// The provider id of the local Ollama host.
const ollamaProvider = 'ollama';

/// The default HTTP route names; they follow the default generation binding.
const _defaultRoutes = {'sonder', 'local'};

/// `Sonder Inference`, `Ollama`, or the id as sent.
String providerDisplayName(String provider) =>
    switch (canonicalProvider(provider)) {
      sonderInferenceProvider => 'Sonder Inference',
      ollamaProvider => 'Ollama',
      final other => other,
    };

String _plural(int n, String one, String many) => n == 1 ? one : many;

String _list(List<String> items) => items.length <= 1
    ? items.join()
    : '${items.sublist(0, items.length - 1).join(', ')} and ${items.last}';

class ModelRouting {
  final EcosystemStatus? status;

  const ModelRouting([this.status]);

  /// The Models panel text when every route runs on Ollama.
  static const ollamaOnlyText =
      'Sonder Runtime routes requests to these providers. '
      'Ollama hosts and runs the local model weights.';

  /// The heading exact models sit under when a route is bound elsewhere.
  static const ollamaDirect = 'Ollama (direct)';

  /// True when a generation binding names a provider other than Ollama, so
  /// an exact model pin bypasses that binding.
  bool get bypassesBinding =>
      status?.generationProviders.any((p) => p != ollamaProvider) ?? false;

  String _key(String id) => id.trim().toLowerCase();

  /// True for `sonder`/`local` and every tier the bindings name.
  bool isRoute(String id) {
    final key = _key(id);
    if (_defaultRoutes.contains(key)) return true;
    return status?.tierProviders.keys.any((t) => _key(t) == key) ?? false;
  }

  /// The provider a route is bound to; null for an exact model or when the
  /// runtime reported no binding.
  String? routeProvider(String id) {
    final s = status;
    if (s == null) return null;
    final key = _key(id);
    if (_defaultRoutes.contains(key)) return s.defaultGenerationProvider;
    for (final entry in s.tierProviders.entries) {
      if (_key(entry.key) == key) return entry.value;
    }
    return null;
  }

  /// The one model [provider] reports serving, if it names exactly one.
  String? servedModel(String provider) {
    final models = status?.providers[provider]?.models ?? const <String>[];
    return models.length == 1 ? models.single : null;
  }

  /// `Sonder Inference (qwen3:14b)` for a route bound off Ollama, else null.
  String? routeBinding(String id) {
    final provider = routeProvider(id);
    if (provider == null || provider == ollamaProvider) return null;
    final served = servedModel(provider);
    final name = providerDisplayName(provider);
    return served == null ? name : '$name ($served)';
  }

  /// The label in the chat model picker and pill.
  String pickerLabel(String id) {
    final binding = routeBinding(id);
    if (binding != null) return '$id · $binding';
    if (bypassesBinding && !isRoute(id)) return '$id · $ollamaDirect';
    return id == 'sonder' ? 'sonder (local route)' : id;
  }

  /// A Runtime → Models chip: the bound provider, or the server's owner.
  String chipLabel(String id, String ownedBy) {
    final provider = routeProvider(id);
    if (provider != null && provider != ollamaProvider) {
      return '$id - ${providerDisplayName(provider)}';
    }
    return '$id - $ownedBy';
  }

  /// Tiers per non-Ollama provider, and the tiers left on Ollama.
  (Map<String, List<String>>, List<String>) _tiersByProvider() {
    final bound = <String, List<String>>{};
    final onOllama = <String>[];
    for (final entry in status?.tierProviders.entries ??
        const <MapEntry<String, String>>[]) {
      if (entry.value == ollamaProvider) {
        onOllama.add(entry.key);
      } else {
        (bound[entry.value] ??= []).add(entry.key);
      }
    }
    return (bound, onOllama);
  }

  /// The Runtime → Models explanation, from the actual bindings.
  String get modelsPanelText {
    if (!bypassesBinding) return ollamaOnlyText;
    final (bound, onOllama) = _tiersByProvider();
    final parts = <String>[
      'Sonder Runtime routes requests to these providers.'
    ];
    final defaultProvider = status?.defaultGenerationProvider;
    for (final entry in bound.entries) {
      final served = servedModel(entry.key);
      parts.add('${providerDisplayName(entry.key)} serves the '
          '${_list(entry.value)} '
          '${_plural(entry.value.length, 'route', 'routes')}'
          '${served == null ? '' : ' with $served'}.');
    }
    if (defaultProvider != null &&
        defaultProvider != ollamaProvider &&
        !bound.containsKey(defaultProvider)) {
      parts.add('${providerDisplayName(defaultProvider)} serves the default '
          'route.');
    }
    parts.add(onOllama.isEmpty
        ? 'Ollama hosts the local model weights and runs every exact model '
            'you pick, whatever the route bindings.'
        : 'Ollama runs the ${_list(onOllama)} '
            '${_plural(onOllama.length, 'route', 'routes')} and every exact '
            'model you pick, whatever the route bindings.');
    return parts.join(' ');
  }

  /// `Sonder Inference serves 6 routes (qwen3:14b); 52 exact models run
  /// directly on Ollama.` for [models] from `/v1/models`; null when every
  /// route runs on Ollama.
  String? connectionSummary(List<String> models) {
    if (!bypassesBinding) return null;
    final routes = <String, int>{};
    var exact = 0;
    for (final id in models) {
      if (!isRoute(id)) {
        exact++;
        continue;
      }
      final provider = routeProvider(id);
      if (provider != null && provider != ollamaProvider) {
        routes[provider] = (routes[provider] ?? 0) + 1;
      }
    }
    final served = [
      for (final entry in routes.entries)
        '${providerDisplayName(entry.key)} serves ${entry.value} '
            '${_plural(entry.value, 'route', 'routes')}'
            '${servedModel(entry.key) == null ? '' : ' (${servedModel(entry.key)})'}',
    ];
    final direct = '$exact exact ${_plural(exact, 'model runs', 'models run')} '
        'directly on Ollama.';
    return served.isEmpty ? direct : '${served.join('; ')}; $direct';
  }
}
