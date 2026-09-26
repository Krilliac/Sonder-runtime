/// Read models for `GET /v1/sonder/ecosystem` (`sonder.runtime.ecosystem/1`).
///
/// The route reports the runtime's provider bindings, each provider's status
/// (Sonder Inference among them) and the Observatory live-export state. The
/// shape is pinned in the ecosystem integration contract, sections 3.6 and 9;
/// Runtime owns it and this file only reads it.
///
/// Parsing is defensive by design: unknown fields are ignored, a missing or
/// mistyped field reads as null (or empty), an unknown provider state reads
/// as [ProviderState.unknown], and a payload with another schema becomes an
/// [EcosystemReading] the panel can explain instead of an exception.
library;

/// The only ecosystem schema this app understands. Additive changes keep the
/// name; a rename or removal bumps the major version (contract section 12).
const ecosystemSchema = 'sonder.runtime.ecosystem/1';

/// The canonical provider id of Sonder Inference.
const sonderInferenceProvider = 'sonder_inference';

/// Provider ids the runtime treats as Sonder Inference (contract 3.1).
/// `sonder` is deliberately absent: it is a tier/model name.
const _inferenceAliases = {
  'sonder_inference',
  'sonder-inference',
  'sonder-infer',
  'inference',
};

/// The provider id in canonical form, so `sonder-inference` and
/// `sonder_inference` compare equal.
String canonicalProvider(String provider) {
  final id = provider.trim().toLowerCase();
  return _inferenceAliases.contains(id) ? sonderInferenceProvider : id;
}

String? _string(Object? value, [int limit = 400]) {
  if (value is! String) return null;
  final text = value.trim();
  if (text.isEmpty) return null;
  return text.length <= limit ? text : '${text.substring(0, limit)}…';
}

int? _int(Object? value) =>
    value is num && value.isFinite ? value.toInt() : null;

bool? _bool(Object? value) => value is bool ? value : null;

Map<String, dynamic>? _map(Object? value) {
  if (value is Map<String, dynamic>) return value;
  if (value is Map) {
    return {
      for (final entry in value.entries)
        if (entry.key is String) entry.key as String: entry.value,
    };
  }
  return null;
}

List<String> _strings(Object? value, [int limit = 400]) => value is List
    ? [
        for (final item in value)
          if (_string(item, limit) case final text?) text,
      ]
    : const [];

DateTime? _time(Object? value) {
  if (value is num && value.isFinite && value > 0) {
    return DateTime.fromMillisecondsSinceEpoch((value * 1000).round(),
        isUtc: true);
  }
  if (value is String) return DateTime.tryParse(value.trim());
  return null;
}

/// A provider's health as the runtime reports it.
enum ProviderState {
  ready,
  degraded,
  unavailable,
  unknown;

  /// An unrecognised or missing state reads as [unknown], never an error.
  static ProviderState parse(Object? value) =>
      switch (value is String ? value.trim().toLowerCase() : '') {
        'ready' => ready,
        'degraded' => degraded,
        'unavailable' => unavailable,
        _ => unknown,
      };
}

/// The model identity a backend measured (Runtime's `BackendIdentity`, nine
/// keys). Any field may be null when an older or partial payload omits it.
class InferenceIdentity {
  final String? backend;
  final String? model;
  final String? modelDigest;
  final String? quantization;
  final String? backendVersion;
  final String? tokenizerDigest;
  final String? templateDigest;
  final int? contextTokens;
  final String? hardware;

  const InferenceIdentity({
    this.backend,
    this.model,
    this.modelDigest,
    this.quantization,
    this.backendVersion,
    this.tokenizerDigest,
    this.templateDigest,
    this.contextTokens,
    this.hardware,
  });

  factory InferenceIdentity.fromJson(Map<String, dynamic> json) =>
      InferenceIdentity(
        backend: _string(json['backend'], 64),
        model: _string(json['model'], 200),
        modelDigest: _string(json['model_digest'], 128),
        quantization: _string(json['quantization'], 64),
        backendVersion: _string(json['backend_version'], 64),
        tokenizerDigest: _string(json['tokenizer_digest'], 128),
        templateDigest: _string(json['template_digest'], 128),
        contextTokens: _int(json['context_tokens']),
        hardware: _string(json['hardware'], 200),
      );

  /// The digests that are present, labelled, in display order.
  List<(String, String)> get digests => [
        if (modelDigest != null) ('model', modelDigest!),
        if (tokenizerDigest != null) ('tokenizer', tokenizerDigest!),
        if (templateDigest != null) ('template', templateDigest!),
      ];

  /// `mock · mock:tiny · q4_0 · ctx 4096`, from whatever is present.
  String get summary => [
        if (backend != null) backend!,
        if (model != null) model!,
        if (quantization != null) quantization!,
        if (contextTokens != null && contextTokens! > 0) 'ctx $contextTokens',
      ].join(' · ');

  /// A digest shortened for display: `sha256:` dropped, first 12 characters.
  static String shortDigest(String digest) {
    final bare = digest.startsWith('sha256:') ? digest.substring(7) : digest;
    return bare.length <= 12 ? bare : '${bare.substring(0, 12)}…';
  }
}

/// The absolute live-telemetry URLs a producer publishes.
class TelemetryLinks {
  final String? discoveryUrl;
  final String? sseUrl;
  final String? ndjsonUrl;

  const TelemetryLinks({this.discoveryUrl, this.sseUrl, this.ndjsonUrl});

  /// Null when [json] is not an object or names no URL at all.
  static TelemetryLinks? fromJson(Object? json) {
    final map = _map(json);
    if (map == null) return null;
    final links = TelemetryLinks(
      discoveryUrl: _string(map['discovery_url'], 512),
      sseUrl: _string(map['sse_url'], 512),
      ndjsonUrl: _string(map['ndjson_url'], 512),
    );
    return links.discoveryUrl == null &&
            links.sseUrl == null &&
            links.ndjsonUrl == null
        ? null
        : links;
  }
}

/// One provider's `provider_status()` entry (contract section 3.6).
class ProviderStatus {
  final String provider;
  final ProviderState state;

  /// The state string as sent, so an unrecognised one can be shown verbatim.
  final String rawState;
  final bool? healthy;
  final DateTime? checkedAt;
  final String? detail;
  final List<String> capabilities;
  final String? baseUrl;
  final String? version;
  final int? apiVersion;
  final List<String> models;

  /// True for mock/synthetic providers; null when the provider cannot say.
  final bool? synthetic;
  final InferenceIdentity? identity;
  final TelemetryLinks? telemetry;
  final String? fallback;
  final int fallbackCount;

  const ProviderStatus({
    required this.provider,
    this.state = ProviderState.unknown,
    this.rawState = '',
    this.healthy,
    this.checkedAt,
    this.detail,
    this.capabilities = const [],
    this.baseUrl,
    this.version,
    this.apiVersion,
    this.models = const [],
    this.synthetic,
    this.identity,
    this.telemetry,
    this.fallback,
    this.fallbackCount = 0,
  });

  factory ProviderStatus.fromJson(String key, Map<String, dynamic> json) {
    final identity = _map(json['identity']);
    final fallback = _string(json['fallback'], 64);
    return ProviderStatus(
      provider: canonicalProvider(_string(json['provider'], 64) ?? key),
      state: ProviderState.parse(json['state']),
      rawState: _string(json['state'], 32) ?? '',
      healthy: _bool(json['healthy']),
      checkedAt: _time(json['checked_at']),
      detail: _string(json['detail'], 240),
      capabilities: _strings(json['capabilities'], 64),
      baseUrl: _string(json['base_url'], 512),
      version: _string(json['version'], 64),
      apiVersion: _int(json['api_version']),
      models: _strings(json['models'], 200),
      synthetic: _bool(json['synthetic']),
      identity: identity == null ? null : InferenceIdentity.fromJson(identity),
      telemetry: TelemetryLinks.fromJson(json['telemetry']),
      fallback: fallback == null ? null : canonicalProvider(fallback),
      fallbackCount: _int(json['fallback_count']) ?? 0,
    );
  }
}

/// The runtime's Observatory live-export section.
class ObservatoryExport {
  final bool exportEnabled;
  final TelemetryLinks? runtimeStream;
  final int? subscribers;
  final int? emittedEvents;
  final int? droppedEvents;
  final int? retainedEvents;
  final int? bufferCapacity;
  final List<String> corsOrigins;

  /// Base URLs of the runtime and of each provider that publishes telemetry:
  /// what the Observatory connects to.
  final List<String> connectUrls;
  final List<String> warnings;

  const ObservatoryExport({
    this.exportEnabled = false,
    this.runtimeStream,
    this.subscribers,
    this.emittedEvents,
    this.droppedEvents,
    this.retainedEvents,
    this.bufferCapacity,
    this.corsOrigins = const [],
    this.connectUrls = const [],
    this.warnings = const [],
  });

  factory ObservatoryExport.fromJson(Map<String, dynamic> json) {
    final stats = _map(json['stats']) ?? const <String, dynamic>{};
    return ObservatoryExport(
      exportEnabled: json['export_enabled'] == true,
      runtimeStream: TelemetryLinks.fromJson(json['runtime_stream']),
      subscribers: _int(stats['subscribers']),
      emittedEvents: _int(stats['emitted_events']),
      droppedEvents: _int(stats['dropped_events']),
      retainedEvents: _int(stats['retained_events']),
      bufferCapacity: _int(stats['buffer_capacity']),
      corsOrigins: _strings(json['cors_origins'], 256),
      connectUrls: _strings(json['connect_urls'], 512),
      warnings: _strings(json['warnings'], 400),
    );
  }
}

/// What the panel shows for Sonder Inference: a provider state, or that no
/// binding uses it.
enum InferenceState {
  ready('ready'),
  degraded('degraded'),
  unavailable('unavailable'),
  unknown('unknown'),
  notConfigured('not configured');

  /// The state word shown as text (colour never carries it alone).
  final String word;
  const InferenceState(this.word);
}

/// A parsed `sonder.runtime.ecosystem/1` payload.
class EcosystemStatus {
  final String schema;
  final DateTime? generatedAt;
  final String? runtimeVersion;
  final String? instanceId;
  final String? nodeId;
  final String? runtimeBaseUrl;
  final String? defaultGenerationProvider;

  /// Tier name to provider id, in the runtime's order.
  final Map<String, String> tierProviders;
  final String? embeddingProvider;

  /// Provider id to its configured fallback provider id.
  final Map<String, String> fallbacks;

  /// Provider id to its status entry.
  final Map<String, ProviderStatus> providers;
  final ObservatoryExport? observatory;

  const EcosystemStatus({
    this.schema = ecosystemSchema,
    this.generatedAt,
    this.runtimeVersion,
    this.instanceId,
    this.nodeId,
    this.runtimeBaseUrl,
    this.defaultGenerationProvider,
    this.tierProviders = const {},
    this.embeddingProvider,
    this.fallbacks = const {},
    this.providers = const {},
    this.observatory,
  });

  factory EcosystemStatus.fromJson(Map<String, dynamic> json) {
    final runtime = _map(json['runtime']) ?? const <String, dynamic>{};
    final providers = _map(json['providers']) ?? const <String, dynamic>{};
    final tiers = _map(providers['tier_providers']) ?? const {};
    final fallbacks = _map(providers['fallbacks']) ?? const {};
    final status = _map(providers['status']) ?? const {};
    final observatory = _map(json['observatory']);
    String? provider(Object? value) {
      final id = _string(value, 64);
      return id == null ? null : canonicalProvider(id);
    }

    return EcosystemStatus(
      schema: _string(json['schema'], 128) ?? '',
      generatedAt: _time(json['generated_at']),
      runtimeVersion: _string(runtime['version'], 64),
      instanceId: _string(runtime['instance_id'], 128),
      nodeId: _string(runtime['node_id'], 128),
      runtimeBaseUrl: _string(runtime['base_url'], 512),
      defaultGenerationProvider:
          provider(providers['default_generation_provider']),
      tierProviders: {
        for (final entry in tiers.entries)
          if (provider(entry.value) case final id?) entry.key: id,
      },
      embeddingProvider: provider(providers['embedding_provider']),
      fallbacks: {
        for (final entry in fallbacks.entries)
          if (provider(entry.value) case final id?)
            canonicalProvider(entry.key): id,
      },
      providers: {
        for (final entry in status.entries)
          if (_map(entry.value) case final value?)
            canonicalProvider(entry.key):
                ProviderStatus.fromJson(entry.key, value),
      },
      observatory:
          observatory == null ? null : ObservatoryExport.fromJson(observatory),
    );
  }

  /// Every distinct provider a binding names.
  Set<String> get boundProviders => {
        if (defaultGenerationProvider != null) defaultGenerationProvider!,
        ...tierProviders.values,
        if (embeddingProvider != null) embeddingProvider!,
      };

  /// Sonder Inference's status entry, if the runtime reported one.
  ProviderStatus? get inference => providers[sonderInferenceProvider];

  /// True when any binding or status entry uses Sonder Inference.
  bool get inferenceConfigured =>
      inference != null || boundProviders.contains(sonderInferenceProvider);

  InferenceState get inferenceState {
    final entry = inference;
    if (entry != null) {
      return switch (entry.state) {
        ProviderState.ready => InferenceState.ready,
        ProviderState.degraded => InferenceState.degraded,
        ProviderState.unavailable => InferenceState.unavailable,
        ProviderState.unknown => InferenceState.unknown,
      };
    }
    return inferenceConfigured
        ? InferenceState.unknown
        : InferenceState.notConfigured;
  }

  /// The provider that takes over when Sonder Inference is unreachable.
  String? get inferenceFallback =>
      inference?.fallback ?? fallbacks[sonderInferenceProvider];
}

/// Why an ecosystem read has no usable status.
enum EcosystemAvailability {
  /// A `sonder.runtime.ecosystem/1` payload was read.
  available,

  /// The runtime answered 404: an older build, or neither live export nor
  /// provider status is available.
  unsupportedRuntime,

  /// The payload names a schema this app does not read.
  unsupportedSchema,
}

/// One read of the ecosystem route.
class EcosystemReading {
  final EcosystemAvailability availability;
  final EcosystemStatus? status;

  /// The schema the payload named (for [EcosystemAvailability.unsupportedSchema]).
  final String schema;

  const EcosystemReading._(this.availability, this.status, this.schema);

  const EcosystemReading.unsupportedRuntime()
      : this._(EcosystemAvailability.unsupportedRuntime, null, '');

  /// A loaded status. A mismatched schema is kept as unsupported.
  factory EcosystemReading.of(EcosystemStatus status) =>
      status.schema == ecosystemSchema
          ? EcosystemReading._(EcosystemAvailability.available, status, '')
          : EcosystemReading._(
              EcosystemAvailability.unsupportedSchema, null, status.schema);

  /// Reads a decoded JSON body. Never throws: anything that is not a
  /// `sonder.runtime.ecosystem/1` object is an unsupported schema.
  factory EcosystemReading.parse(Object? decoded) {
    final map = _map(decoded);
    if (map == null) {
      return const EcosystemReading._(
          EcosystemAvailability.unsupportedSchema, null, '');
    }
    final schema = _string(map['schema'], 128) ?? '';
    if (schema != ecosystemSchema) {
      return EcosystemReading._(
          EcosystemAvailability.unsupportedSchema, null, schema);
    }
    return EcosystemReading.of(EcosystemStatus.fromJson(map));
  }
}
