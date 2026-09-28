/// `GET /v1/models` with the runtime's additive per-row routing field.
///
/// Each OpenAI-shaped row may carry `"sonder": {"kind": "route", "provider":
/// "sonder_inference", "served_model": "qwen3:14b"}` (a route) or
/// `{"kind": "model", "provider": "ollama"}` (an exact model). Any key can
/// read `/v1/models`, so this is where a non-administrator learns which
/// provider serves a route; the admin-only `GET /v1/sonder/ecosystem` stays
/// the preferred source when it can be read. An older runtime sends no field
/// and every row simply has no origin.
library;

import 'ecosystem.dart' show canonicalProvider;

/// Where one `/v1/models` row runs, as the runtime reports it.
class ModelOrigin {
  /// `route` for `sonder` and tier ids, `model` for an exact model.
  final String kind;

  /// Canonical provider id (`sonder_inference`, `ollama`, ...); null when the
  /// runtime could not resolve it.
  final String? provider;

  /// The model a route is served with; null when unknown or for a model row.
  final String? servedModel;

  const ModelOrigin({required this.kind, this.provider, this.servedModel});

  bool get isRoute => kind == 'route';

  static String? _text(Object? value, int limit) {
    if (value is! String) return null;
    final text = value.trim();
    if (text.isEmpty || text.length > limit) return null;
    return text;
  }

  /// Null unless [json] is an object whose `kind` is `route` or `model`.
  static ModelOrigin? fromJson(Object? json) {
    if (json is! Map) return null;
    final kind = _text(json['kind'], 16);
    if (kind != 'route' && kind != 'model') return null;
    final provider = _text(json['provider'], 64);
    return ModelOrigin(
      kind: kind!,
      provider: provider == null ? null : canonicalProvider(provider),
      servedModel: _text(json['served_model'], 200),
    );
  }
}

/// The ids `/v1/models` lists, in order, and each row's [ModelOrigin] keyed
/// by lower-case id.
class ModelCatalog {
  final List<String> ids;
  final Map<String, ModelOrigin> origins;

  const ModelCatalog({this.ids = const [], this.origins = const {}});

  /// Throws [FormatException] (or a type error) for a body that is not an
  /// OpenAI model list; rows without a usable routing field have no origin.
  factory ModelCatalog.fromJson(Map<String, dynamic> json) {
    final data = (json['data'] as List?) ?? const [];
    final ids = <String>[];
    final origins = <String, ModelOrigin>{};
    for (final row in data) {
      final map = row as Map<String, dynamic>;
      final id = map['id']?.toString() ?? '';
      if (id.isEmpty) continue;
      ids.add(id);
      final origin = ModelOrigin.fromJson(map['sonder']);
      if (origin != null) origins[id.trim().toLowerCase()] = origin;
    }
    return ModelCatalog(ids: ids, origins: origins);
  }
}
