/// Which provider serves a model id the app offers, read from the runtime's
/// provider bindings (`GET /v1/sonder/ecosystem`, administrator-only) or,
/// when that cannot be read, from the per-row routing field of
/// `GET /v1/models` ([ModelOrigin]).
///
/// Runtime contract (`provider_bridge.provider_for_tier`): a route name
/// (`sonder` or a tier such as `general`) follows the tier binding, but an
/// exact model pin (any `/v1/models` id that is not a route name) always
/// runs on Ollama, even when every tier is bound to Sonder Inference. The
/// labels here make that visible; with no document, or with everything on
/// Ollama, they are the labels the app always showed.
library;

import 'ecosystem.dart';
import 'model_catalog.dart';

/// The provider id of the local Ollama host.
const ollamaProvider = 'ollama';

/// The default HTTP route names; their policy tier is resolved by the server.
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

  /// `/v1/models` row origins keyed by lower-case id: the fallback when
  /// [status] (the admin-only ecosystem document) is unavailable.
  final Map<String, ModelOrigin> origins;

  const ModelRouting([this.status]) : origins = const {};

  const ModelRouting.of(this.status, {this.origins = const {}});

  /// The Models panel text when every route runs on Ollama.
  static const ollamaOnlyText =
      'Sonder Runtime routes requests to these providers. '
      'Ollama hosts and runs the local model weights.';

  /// The heading exact models sit under when a route is bound elsewhere.
  static const ollamaDirect = 'Ollama (direct)';

  static String _key(String id) => id.trim().toLowerCase();

  ModelOrigin? _origin(String id) => origins[_key(id)];

  /// Route origins reported by `/v1/models`, in the runtime's order.
  Iterable<MapEntry<String, ModelOrigin>> get _routeOrigins =>
      origins.entries.where((e) => e.value.isRoute);

  /// True when a generation binding names a provider other than Ollama, so
  /// an exact model pin bypasses that binding.
  bool get bypassesBinding {
    final s = status;
    if (s != null) return s.generationProviders.any((p) => p != ollamaProvider);
    return _routeOrigins.any(
        (e) => e.value.provider != null && e.value.provider != ollamaProvider);
  }

  /// True for `sonder`/`local`, every tier the bindings name, and every row
  /// `/v1/models` reports as a route.
  bool isRoute(String id) {
    final key = _key(id);
    if (_defaultRoutes.contains(key)) return true;
    if (status?.tierProviders.keys.any((t) => _key(t) == key) ?? false) {
      return true;
    }
    return _origin(id)?.isRoute ?? false;
  }

  /// The provider a route is bound to; null for an exact model or when the
  /// runtime reported no binding. Default routes use their resolved
  /// `/v1/models` row, else the default binding only when every reported tier
  /// shares it; named tiers use the ecosystem binding when present.
  String? routeProvider(String id) {
    final key = _key(id);
    final s = status;
    if (_defaultRoutes.contains(key)) {
      final origin = _origin(id);
      if (origin != null && origin.isRoute) return origin.provider;
      // Without a resolved row, the server's choice of policy tier is unknown;
      // the default binding is only certain when every reported tier agrees.
      if (s == null) return null;
      final bound = s.tierProviders.values.toSet();
      final fallback = s.defaultGenerationProvider;
      if (bound.isEmpty || (bound.length == 1 && bound.single == fallback)) {
        return fallback;
      }
      return null;
    }
    if (s != null) {
      for (final entry in s.tierProviders.entries) {
        if (_key(entry.key) == key) return entry.value;
      }
    }
    final origin = _origin(id);
    return origin != null && origin.isRoute ? origin.provider : null;
  }

  /// The one model [provider] reports serving, if it names exactly one.
  String? servedModel(String provider) {
    final models = status?.providers[provider]?.models ?? const <String>[];
    return models.length == 1 ? models.single : null;
  }

  /// The model route [id] is served with: the bound provider's per-tier
  /// model from the ecosystem document, else the `/v1/models` row's
  /// `served_model` for that provider, else the provider's only model.
  String? routeServedModel(String id) {
    final provider = routeProvider(id);
    if (provider == null) return null;
    final tierModel = status?.providers[provider]?.tierModels[_key(id)];
    if (tierModel != null) return tierModel;
    final origin = _origin(id);
    if (origin != null && origin.provider == provider) {
      final served = origin.servedModel;
      if (served != null) return served;
    }
    return servedModel(provider);
  }

  /// `Sonder Inference (qwen3:14b)` for a route bound off Ollama, else null.
  String? routeBinding(String id) {
    final provider = routeProvider(id);
    if (provider == null || provider == ollamaProvider) return null;
    final served = routeServedModel(id);
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

  /// Who serves [id] on Runtime → Models: the provider a route is bound to,
  /// or the server's owner field.
  String servedBy(String id, String ownedBy) {
    final provider = routeProvider(id);
    if (provider != null && provider != ollamaProvider) {
      return providerDisplayName(provider);
    }
    return ownedBy;
  }

  /// `code - Sonder Inference`: the id and [servedBy] on one line.
  String chipLabel(String id, String ownedBy) =>
      '$id - ${servedBy(id, ownedBy)}';

  /// The tier routes to describe, with their provider: the ecosystem
  /// bindings, else the `/v1/models` route rows (default routes excluded).
  /// With [offered], only tiers the runtime actually offers are kept: a
  /// tier the local policy leaves unset is dropped from `/v1/models` and is
  /// not available, whatever it is bound to.
  List<(String, String)> _tierRoutes(Iterable<String>? offered) {
    final keep = offered?.map(_key).toSet();
    final s = status;
    final routes = s != null
        ? [for (final e in s.tierProviders.entries) (e.key, e.value)]
        : [
            for (final e in _routeOrigins)
              if (!_defaultRoutes.contains(e.key) && e.value.provider != null)
                (e.key, e.value.provider!),
          ];
    return [
      for (final route in routes)
        if (keep == null || keep.contains(_key(route.$1))) route,
    ];
  }

  /// `the fast and general routes with qwen3:14b and the reasoning route
  /// with deepseek-r1:14b`, grouping [tiers] by their served model.
  String _servedPhrase(List<String> tiers) {
    final byModel = <String?, List<String>>{};
    for (final tier in tiers) {
      (byModel[routeServedModel(tier)] ??= []).add(tier);
    }
    return _list([
      for (final entry in byModel.entries)
        'the ${_list(entry.value)} '
            '${_plural(entry.value.length, 'route', 'routes')}'
            '${entry.key == null ? '' : ' with ${entry.key}'}',
    ]);
  }

  /// The Runtime → Models explanation, from the actual bindings. Pass the
  /// route ids the runtime offers ([offered], e.g. the status `models`) so
  /// a tier with no model is not claimed.
  String modelsPanelText({Iterable<String>? offered}) {
    if (!bypassesBinding) return ollamaOnlyText;
    final bound = <String, List<String>>{};
    final onOllama = <String>[];
    for (final (tier, provider) in _tierRoutes(offered)) {
      if (provider == ollamaProvider) {
        onOllama.add(tier);
      } else {
        (bound[provider] ??= []).add(tier);
      }
    }
    final parts = <String>[
      'Sonder Runtime routes requests to these providers.'
    ];
    for (final entry in bound.entries) {
      parts.add('${providerDisplayName(entry.key)} serves '
          '${_servedPhrase(entry.value)}.');
    }
    final defaultProvider = routeProvider('sonder');
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
    final served = <String, Set<String>>{};
    var exact = 0;
    for (final id in models) {
      if (!isRoute(id)) {
        exact++;
        continue;
      }
      final provider = routeProvider(id);
      if (provider != null && provider != ollamaProvider) {
        routes[provider] = (routes[provider] ?? 0) + 1;
        final model = routeServedModel(id);
        if (model != null) (served[provider] ??= {}).add(model);
      }
    }
    final summary = [
      for (final entry in routes.entries)
        '${providerDisplayName(entry.key)} serves ${entry.value} '
            '${_plural(entry.value, 'route', 'routes')}'
            '${served[entry.key] == null ? '' : ' (${served[entry.key]!.join(', ')})'}',
    ];
    final direct = '$exact exact ${_plural(exact, 'model runs', 'models run')} '
        'directly on Ollama.';
    return summary.isEmpty ? direct : '${summary.join('; ')}; $direct';
  }
}
