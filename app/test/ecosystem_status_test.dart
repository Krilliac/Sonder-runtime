// Parsing of GET /v1/sonder/ecosystem (sonder.runtime.ecosystem/1).
//
// The fixtures are built from the integration contract (runtime_fixtures.dart).
// With SONDER_ECOSYSTEM_JSON pointing at a payload captured from a real
// runtime, the last group parses that file too: the ecosystem e2e runs it.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/local_manager_models.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';

import 'runtime_fixtures.dart';

/// A UTF-8 JSON response, as the runtime sends it.
http.Response _json(Object body, [int status = 200]) =>
    http.Response.bytes(utf8.encode(jsonEncode(body)), status,
        headers: const {'content-type': 'application/json; charset=utf-8'});

EcosystemStatus _status(Map<String, dynamic> json) {
  final reading = EcosystemReading.parse(jsonDecode(jsonEncode(json)));
  expect(reading.availability, EcosystemAvailability.available);
  return reading.status!;
}

void main() {
  group('EcosystemReading.parse', () {
    test('ready + synthetic mock: bindings, identity, telemetry, export', () {
      final status = _status(ecosystemReadySynthetic());
      expect(status.schema, ecosystemSchema);
      expect(status.generatedAt, DateTime.utc(2026, 9, 25, 12, 41, 30));
      expect(status.runtimeVersion, '2026.09.25');
      expect(status.instanceId, 'rt-3f9a12c0');
      expect(status.runtimeBaseUrl, 'http://127.0.0.1:11435');
      expect(status.defaultGenerationProvider, sonderInferenceProvider);
      expect(status.tierProviders.keys,
          ['fast', 'general', 'code', 'reasoning', 'vision']);
      expect(status.embeddingProvider, 'ollama');
      expect(status.inferenceState, InferenceState.ready);
      final inference = status.inference!;
      expect(inference.synthetic, isTrue);
      expect(inference.apiVersion, 1);
      expect(inference.models, ['mock:tiny']);
      expect(inference.capabilities, ['chat', 'fixed-endpoint']);
      expect(inference.checkedAt, DateTime.utc(2026, 9, 25, 12, 41, 25));
      final identity = inference.identity!;
      expect(identity.summary, 'mock · mock:tiny · none · ctx 4096');
      expect(identity.digests.map((d) => d.$1),
          ['model', 'tokenizer', 'template']);
      expect(InferenceIdentity.shortDigest(identity.modelDigest!),
          '9f86d081884c…');
      expect(inference.telemetry!.sseUrl,
          'http://127.0.0.1:11437/v1/telemetry/sse');
      expect(status.inferenceFallback, isNull);
      final export = status.observatory!;
      expect(export.exportEnabled, isTrue);
      expect(export.subscribers, 1);
      expect(export.emittedEvents, 1204);
      expect(export.droppedEvents, 0);
      expect(export.retainedEvents, 512);
      expect(export.bufferCapacity, 4096);
      expect(export.runtimeStream!.ndjsonUrl,
          'http://127.0.0.1:11435/v1/observability/events?format=ndjson');
      expect(export.connectUrls,
          ['http://127.0.0.1:11435', 'http://127.0.0.1:11437']);
      // Providers without provider_status() read as unknown.
      expect(status.providers['ollama']!.state, ProviderState.unknown);
    });

    test('unavailable without fallback', () {
      final status = _status(ecosystemUnavailable());
      expect(status.inferenceState, InferenceState.unavailable);
      expect(status.inference!.synthetic, isNull);
      expect(status.inference!.identity, isNull);
      expect(status.inference!.telemetry, isNull);
      expect(status.inferenceFallback, isNull);
      expect(status.observatory!.connectUrls, ['http://127.0.0.1:11435']);
    });

    test('all-Ollama bindings: Sonder Inference not configured', () {
      final status = _status(ecosystemAllOllama());
      expect(status.inferenceConfigured, isFalse);
      expect(status.inferenceState, InferenceState.notConfigured);
      expect(status.boundProviders, {'ollama'});
    });

    test('fallback configured', () {
      final status = _status(ecosystemFallback());
      expect(status.inferenceFallback, 'ollama');
      expect(status.fallbacks, {'sonder_inference': 'ollama'});
      expect(status.inference!.fallbackCount, 2);
    });

    test('export disabled', () {
      final status = _status(ecosystemExportDisabled());
      expect(status.observatory!.exportEnabled, isFalse);
      expect(status.observatory!.runtimeStream, isNull);
      expect(status.inference!.synthetic, isFalse);
    });

    test('unknown schema is an unsupported reading, not an exception', () {
      final reading = EcosystemReading.parse(ecosystemUnknownSchema());
      expect(reading.availability, EcosystemAvailability.unsupportedSchema);
      expect(reading.schema, 'sonder.runtime.ecosystem/2');
      expect(reading.status, isNull);
      for (final junk in <Object?>[
        null,
        'x',
        42,
        <Object?>[],
        <String, dynamic>{}
      ]) {
        expect(EcosystemReading.parse(junk).availability,
            EcosystemAvailability.unsupportedSchema);
      }
    });

    test('unknown state, aliases, mistyped and missing fields', () {
      final json = ecosystemJson(
        provider: 'sonder-inference',
        inference: {
          'provider': 'sonder-inference',
          'state': 'rebooting',
          'healthy': 'yes',
          'checked_at': 17,
          'capabilities': 'chat',
          'models': ['a', 3, null, 'b'],
          'api_version': '1',
          'identity': 'none',
          'telemetry': {'unexpected': true},
          'fallback_count': 'many',
        },
      );
      (json['providers'] as Map)['tier_providers'] = {'fast': 7, 'code': 'x'};
      (json['observatory'] as Map)['stats'] = 'n/a';
      final status = _status(json);
      final inference = status.inference!;
      expect(inference.state, ProviderState.unknown);
      expect(inference.rawState, 'rebooting');
      expect(status.inferenceState, InferenceState.unknown);
      expect(inference.healthy, isNull);
      expect(inference.capabilities, isEmpty);
      expect(inference.models, ['a', 'b']);
      expect(inference.apiVersion, isNull);
      expect(inference.identity, isNull);
      expect(inference.telemetry, isNull);
      expect(inference.fallbackCount, 0);
      expect(status.defaultGenerationProvider, sonderInferenceProvider);
      expect(status.tierProviders, {'code': 'x'});
      expect(status.observatory!.subscribers, isNull);

      final bare = _status({'schema': ecosystemSchema});
      expect(bare.providers, isEmpty);
      expect(bare.observatory, isNull);
      expect(bare.inferenceState, InferenceState.notConfigured);
    });

    test('timestamps outside the DateTime range read as null', () {
      for (final value in <Object>[
        1e13,
        1e300,
        '+999999999-01-01T00:00:00Z',
      ]) {
        final reading = EcosystemReading.parse({
          'schema': ecosystemSchema,
          'generated_at': value,
          'providers': {
            'status': {
              'sonder_inference': {'state': 'ready', 'checked_at': value},
            },
          },
        });
        final status = reading.status!;
        expect(status.generatedAt, isNull, reason: '$value');
        expect(status.inference!.checkedAt, isNull, reason: '$value');
        expect(status.inferenceState, InferenceState.ready);
      }
      // The largest representable instant still parses.
      final edge = EcosystemReading.parse({
        'schema': ecosystemSchema,
        'generated_at': 8.64e12,
      });
      expect(
          edge.status!.generatedAt!.millisecondsSinceEpoch, 8640000000000000);
    });

    test('a bound provider with no status entry reads as unknown', () {
      final status = _status(ecosystemJson());
      expect(status.inference, isNull);
      expect(status.inferenceConfigured, isTrue);
      expect(status.inferenceState, InferenceState.unknown);
    });
  });

  group('SonderApi.ecosystemStatus', () {
    Future<Object> read(http.Response response,
        {List<http.BaseRequest>? seen}) async {
      try {
        return await http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
              .ecosystemStatus(),
          () => MockClient((request) async {
            seen?.add(request);
            return response;
          }),
        );
      } catch (error) {
        return error;
      }
    }

    test('GETs the route with the API key and parses the body', () async {
      final seen = <http.BaseRequest>[];
      final result = await read(_json(ecosystemReadySynthetic()), seen: seen);
      expect(result, isA<EcosystemReading>());
      expect((result as EcosystemReading).status!.inferenceState,
          InferenceState.ready);
      expect(seen.single.method, 'GET');
      expect(seen.single.url.path, '/v1/sonder/ecosystem');
      expect(seen.single.headers['Authorization'], 'Bearer k');
    });

    for (final status in [401, 403]) {
      test('$status needs an administrator', () async {
        final result = await read(http.Response(
            '{"error":{"message":"admin only","code":"FORBIDDEN"}}', status));
        expect(result, isA<SonderException>());
        final error = result as SonderException;
        expect(error.message, 'Administrator authorization is required.');
        expect(error.httpStatus, status);
      });
    }

    test('404 is the unsupported-runtime reading', () async {
      final result = await read(http.Response('{"error":{}}', 404));
      expect((result as EcosystemReading).availability,
          EcosystemAvailability.unsupportedRuntime);
    });

    test('other failures go through the failure helpers', () async {
      final result = await read(
          http.Response('{"error":{"message":"boom","code":"INTERNAL"}}', 500));
      expect(result, isA<SonderException>());
      expect((result as SonderException).httpStatus, 500);
    });

    test('an unparseable or oversized body is a readable error', () async {
      final bad = await read(http.Response('not json', 200));
      expect((bad as SonderException).message,
          'Could not parse ecosystem status.');
      final huge = await read(http.Response('x' * (300 * 1024), 200));
      expect((huge as SonderException).message,
          'Ecosystem status exceeds the response limit.');
    });

    Future<Object> readStreamed(http.StreamedResponse response) async {
      try {
        return await http.runWithClient(
          () => SonderApi(baseUrl: 'http://127.0.0.1:11435', apiKey: 'k')
              .ecosystemStatus(),
          () => MockClient.streaming((request, body) async => response),
        );
      } catch (error) {
        return error;
      }
    }

    test('a declared oversized body is refused before any byte is read',
        () async {
      var listened = false;
      final body = Stream<List<int>>.fromIterable([
        utf8.encode('{}'),
      ]);
      final controller =
          StreamController<List<int>>(onListen: () => listened = true);
      unawaited(controller.addStream(body).then((_) => controller.close()));
      final result = await readStreamed(http.StreamedResponse(
          controller.stream, 200,
          contentLength: 300 * 1024));
      expect((result as SonderException).message,
          'Ecosystem status exceeds the response limit.');
      expect(listened, isFalse);
    });

    test('an undeclared endless body stops at the limit', () async {
      var delivered = 0;
      var cancelled = false;
      final chunk = List<int>.filled(16 * 1024, 0x20);
      late final StreamController<List<int>> controller;
      controller = StreamController<List<int>>(
        onListen: () async {
          // Endless: only a cancel from the reader ends it.
          while (!cancelled) {
            controller.add(chunk);
            delivered += chunk.length;
            await Future<void>.delayed(Duration.zero);
          }
        },
        onCancel: () => cancelled = true,
      );
      final result =
          await readStreamed(http.StreamedResponse(controller.stream, 200));
      expect((result as SonderException).message,
          'Ecosystem status exceeds the response limit.');
      expect(cancelled, isTrue);
      // At most one chunk past the 256 KiB limit was ever produced.
      expect(delivered, lessThanOrEqualTo(256 * 1024 + 2 * chunk.length));
    });

    test('a body within the limit streams through', () async {
      final bytes = utf8.encode(jsonEncode(ecosystemReadySynthetic()));
      final result = await readStreamed(http.StreamedResponse(
          Stream.fromIterable([
            bytes.sublist(0, 10),
            bytes.sublist(10),
          ]),
          200,
          contentLength: bytes.length));
      expect((result as EcosystemReading).status!.inferenceState,
          InferenceState.ready);
    });

    test('HttpRuntimeDataSource.ecosystem reads the same route', () async {
      final seen = <String>[];
      final reading = await http.runWithClient(
        () => const HttpRuntimeDataSource(
                baseUrl: 'http://127.0.0.1:11435/', apiKey: 'k')
            .ecosystem(),
        () => MockClient((request) async {
          seen.add('${request.method} ${request.url.path} '
              '${request.headers['Authorization']}');
          return _json(ecosystemAllOllama());
        }),
      );
      expect(reading.status!.inferenceState, InferenceState.notConfigured);
      expect(seen, ['GET /v1/sonder/ecosystem Bearer k']);
    });
  });

  group('SONDER_ECOSYSTEM_JSON', () {
    final path = Platform.environment['SONDER_ECOSYSTEM_JSON'] ?? '';
    test('parses a payload captured from a live runtime', () {
      final reading =
          EcosystemReading.parse(jsonDecode(File(path).readAsStringSync()));
      expect(reading.availability, EcosystemAvailability.available,
          reason: 'schema ${reading.schema}');
      final status = reading.status!;
      expect(status.providers, isNotEmpty);
      final export = status.observatory;
      expect(export, isNotNull);
      // Every connect URL the runtime publishes survives the launch
      // sanitiser, so Open Observatory would pass all of them.
      expect(observatoryConnectUrls(export!.connectUrls).length,
          export.connectUrls.toSet().length);
    }, skip: path.isEmpty ? 'SONDER_ECOSYSTEM_JSON is not set' : null);
  });
}
