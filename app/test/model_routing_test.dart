import 'package:flutter_test/flutter_test.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/model_routing.dart';
import 'package:sonder_runtime/settings_screen.dart';

import 'runtime_fixtures.dart';

EcosystemStatus _status(Map<String, dynamic> json) =>
    EcosystemReading.parse(json).status!;

/// Every tier bound to Sonder Inference, which serves one model.
EcosystemStatus allInference() => _status(ecosystemJson(
    inference: inferenceStatusJson()..['models'] = ['qwen3:14b']));

void main() {
  group('ModelRouting', () {
    test('no ecosystem document keeps today\'s labels', () {
      const routing = ModelRouting();
      expect(routing.bypassesBinding, isFalse);
      expect(routing.pickerLabel('sonder'), 'sonder (local route)');
      expect(routing.pickerLabel('general'), 'general');
      expect(routing.pickerLabel('llama3:8b'), 'llama3:8b');
      expect(routing.chipLabel('general', 'local'), 'general - local');
      expect(routing.modelsPanelText(), ModelRouting.ollamaOnlyText);
    });

    test('everything on Ollama keeps today\'s labels', () {
      final routing = ModelRouting(_status(ecosystemAllOllama()));
      expect(routing.bypassesBinding, isFalse);
      expect(routing.pickerLabel('sonder'), 'sonder (local route)');
      expect(routing.pickerLabel('general'), 'general');
      expect(routing.chipLabel('code', 'local'), 'code - local');
      expect(routing.modelsPanelText(), ModelRouting.ollamaOnlyText);
      expect(routing.connectionSummary(['sonder', 'general', 'llama3:8b']),
          isNull);
    });

    test('routes bound to Sonder Inference name the provider and model', () {
      final routing = ModelRouting(allInference());
      expect(routing.bypassesBinding, isTrue);
      expect(routing.isRoute('general'), isTrue);
      expect(routing.isRoute('sonder'), isTrue);
      expect(routing.isRoute('qwen3:14b'), isFalse);
      expect(routing.pickerLabel('general'),
          'general · Sonder Inference (qwen3:14b)');
      expect(routing.pickerLabel('sonder'),
          'sonder · Sonder Inference (qwen3:14b)');
      // An exact pin always runs on Ollama, whatever the bindings say.
      expect(routing.pickerLabel('qwen3:14b'), 'qwen3:14b · Ollama (direct)');
      expect(
          routing.chipLabel('general', 'local'), 'general - Sonder Inference');
      expect(routing.modelsPanelText(), isNot(ModelRouting.ollamaOnlyText));
      expect(routing.modelsPanelText(), contains('Sonder Inference serves'));
      expect(routing.modelsPanelText(), contains('exact model'));
      expect(routing.modelsPanelText(), isNot(contains('local model weights.')));
    });

    test('a mixed binding lists the Ollama routes too', () {
      final json = ecosystemJson(inference: inferenceStatusJson());
      (json['providers'] as Map)['tier_providers'] = {
        'fast': 'ollama',
        'general': 'sonder_inference',
      };
      final routing = ModelRouting(_status(json));
      expect(routing.chipLabel('fast', 'local'), 'fast - local');
      expect(routing.modelsPanelText(),
          contains('Sonder Inference serves the general route'));
      expect(routing.modelsPanelText(), contains('the fast route'));
    });

    test('connection summary counts routes and exact Ollama models', () {
      final routing = ModelRouting(allInference());
      expect(
          routing.connectionSummary(
              ['sonder', 'fast', 'general', 'qwen3:14b', 'llama3:8b']),
          'Sonder Inference serves 3 routes (qwen3:14b); '
          '2 exact models run directly on Ollama.');
      expect(
          routing.connectionSummary(['general', 'llama3:8b']),
          'Sonder Inference serves 1 route (qwen3:14b); '
          '1 exact model runs directly on Ollama.');
    });
  });

  group('Models panel lists only offered routes', () {
    test('tiers missing from the offered routes are not claimed', () {
      final routing = ModelRouting(allInference());
      final text = routing
          .modelsPanelText(offered: ['sonder', 'fast', 'general', 'code']);
      expect(
          text,
          contains('Sonder Inference serves the fast, general and code '
              'routes with qwen3:14b.'));
      expect(text, isNot(contains('reasoning')));
      expect(text, isNot(contains('vision')));
    });

    test('a tier bound with its own model shows that model', () {
      final routing = ModelRouting(_status(ecosystemJson(
          inference: inferenceStatusJson()
            ..['models'] = ['qwen3:14b', 'deepseek-r1:14b']
            ..['tier_models'] = {
              'fast': 'qwen3:14b',
              'general': 'qwen3:14b',
              'code': 'qwen3:14b',
              'reasoning': 'deepseek-r1:14b',
              'vision': 'qwen3:14b',
            })));
      expect(routing.pickerLabel('reasoning'),
          'reasoning · Sonder Inference (deepseek-r1:14b)');
      expect(routing.pickerLabel('general'),
          'general · Sonder Inference (qwen3:14b)');
      expect(
          routing.modelsPanelText(
              offered: ['sonder', 'fast', 'general', 'code', 'reasoning']),
          contains('Sonder Inference serves the fast, general and code routes '
              'with qwen3:14b and the reasoning route with deepseek-r1:14b.'));
    });
  });

  group('/v1/models routing fields (non-admin fallback)', () {
    final catalog = ModelCatalog.fromJson({
      'object': 'list',
      'data': [
        {
          'id': 'sonder',
          'owned_by': 'local',
          'sonder': {
            'kind': 'route',
            'provider': 'sonder_inference',
            'served_model': 'qwen3:14b',
          },
        },
        {
          'id': 'general',
          'owned_by': 'local',
          'sonder': {
            'kind': 'route',
            'provider': 'sonder-inference',
            'served_model': 'qwen3:14b',
          },
        },
        {
          'id': 'code',
          'owned_by': 'local',
          'sonder': {
            'kind': 'route',
            'provider': 'ollama',
            'served_model': 'gemma3:12b',
          },
        },
        {
          'id': 'cloud-code',
          'owned_by': 'cloud',
          'sonder': {
            'kind': 'route',
            'provider': 'ollama',
            'served_model': 'qwen3-coder:480b-cloud',
          },
        },
        {
          'id': 'llama3:8b',
          'owned_by': 'local',
          'sonder': {'kind': 'model', 'provider': 'ollama'},
        },
        {'id': 'old-row', 'owned_by': 'local'},
        {'id': 'bad-row', 'owned_by': 'local', 'sonder': 'nonsense'},
      ],
    });

    test('parses ids and origins defensively', () {
      expect(catalog.ids, [
        'sonder',
        'general',
        'code',
        'cloud-code',
        'llama3:8b',
        'old-row',
        'bad-row'
      ]);
      expect(catalog.origins['general']?.provider, sonderInferenceProvider);
      expect(catalog.origins['general']?.isRoute, isTrue);
      expect(catalog.origins['llama3:8b']?.isRoute, isFalse);
      expect(catalog.origins.containsKey('old-row'), isFalse);
      expect(catalog.origins.containsKey('bad-row'), isFalse);
    });

    test('labels routes without the ecosystem document', () {
      final routing = ModelRouting.of(null, origins: catalog.origins);
      expect(routing.bypassesBinding, isTrue);
      expect(routing.pickerLabel('general'),
          'general · Sonder Inference (qwen3:14b)');
      expect(routing.pickerLabel('sonder'),
          'sonder · Sonder Inference (qwen3:14b)');
      expect(routing.pickerLabel('code'), 'code');
      expect(routing.pickerLabel('cloud-code'), 'cloud-code');
      expect(routing.pickerLabel('llama3:8b'), 'llama3:8b · Ollama (direct)');
      expect(routing.chipLabel('general', 'local'),
          'general - Sonder Inference');
      final text = routing.modelsPanelText(offered: catalog.ids);
      expect(text,
          contains('Sonder Inference serves the general route with qwen3:14b.'));
      expect(text, contains('Ollama runs the code and cloud-code routes'));
      expect(text, isNot(contains('reasoning')));
      expect(
          routing.connectionSummary(catalog.ids),
          'Sonder Inference serves 2 routes (qwen3:14b); '
          '3 exact models run directly on Ollama.');
    });

    test('rows naming only Ollama keep today\'s labels', () {
      const routing = ModelRouting.of(null, origins: {
        'general': ModelOrigin(
            kind: 'route', provider: 'ollama', servedModel: 'qwen3:14b'),
      });
      expect(routing.bypassesBinding, isFalse);
      expect(routing.pickerLabel('general'), 'general');
      expect(routing.modelsPanelText(), ModelRouting.ollamaOnlyText);
    });

    test('the ecosystem document wins when both are present', () {
      final routing = ModelRouting.of(_status(ecosystemAllOllama()),
          origins: catalog.origins);
      expect(routing.bypassesBinding, isFalse);
      expect(routing.pickerLabel('general'), 'general');
    });
  });

  group('Settings test connection', () {
    test('without bindings the message is unchanged', () {
      expect(diagnoseReachable('http://127.0.0.1:11435', modelCount: 58).title,
          'Connected to 127.0.0.1. 58 models available.');
    });

    test('routes bound to Sonder Inference are reported as such', () {
      final models = [
        'sonder',
        'fast',
        'general',
        'code',
        'reasoning',
        'vision',
        for (var i = 0; i < 52; i++) 'model$i:latest',
      ];
      final diagnosis = diagnoseReachable('http://127.0.0.1:11435',
          modelCount: models.length,
          routing: ModelRouting(allInference()),
          models: models);
      expect(diagnosis.ok, isTrue);
      expect(
          diagnosis.title,
          'Connected to 127.0.0.1. Sonder Inference serves 6 routes '
          '(qwen3:14b); 52 exact models run directly on Ollama.');
    });
  });
}
