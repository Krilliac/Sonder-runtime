// ToolInventoryApi against the server's own tool inventory bodies
// (test/fixtures/server/tool_inventory_*.json, produced by
// tests/test_app_tool_inventory_fixtures.py from the real facade).
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';

import 'fixtures/server_fixtures.dart';

const _base = 'http://192.168.1.20:11435';
const _endpoint = SonderEndpoint(baseUrl: _base, apiKey: 'test-key');

Future<SonderException> _failure(String fixture, int status,
    {Map<String, String> headers = jsonHeaders}) async {
  late SonderException caught;
  await recordClients(() async {
    try {
      await const ToolInventoryApi(_endpoint).get();
      fail('expected an error');
    } on SonderException catch (error) {
      caught = error;
    }
  }, (_) async => fixtureResponse(fixture, status, headers: headers));
  return caught;
}

void main() {
  test('GET parses the full inventory the facade produces', () async {
    late ToolInventory inventory;
    final clients = await recordClients(() async {
      inventory = await const ToolInventoryApi(_endpoint).get();
    }, (_) async => fixtureResponse('tool_inventory_200.json', 200));
    final request = clients.single.requests.single;
    expect(request.method, 'GET');
    expect(request.url.toString(), '$_base/v1/tools/inventory');
    expect(request.headers['Authorization'], 'Bearer test-key');
    expect(request is http.Request ? request.body : '', isEmpty);

    expect(inventory.os, 'Linux');
    expect(inventory.machine, 'x86_64');
    expect(inventory.age, const Duration(seconds: 754));
    expect(inventory.stale, isFalse);
    expect(inventory.total, 8);
    expect(inventory.counts['compiler'], 2);
    expect(inventory.filteredBy, isEmpty);
    expect(inventory.notes, ['skipped 1 relative or invalid PATH entries']);
    expect(inventory.createdAt,
        DateTime.fromMillisecondsSinceEpoch(1790334000 * 1000));

    final gcc = inventory.tools.firstWhere((t) => t.name == 'gcc');
    expect(gcc.version, '13.2.0');
    expect(gcc.versionStatus, ToolVersionStatus.ok);
    expect(gcc.onPath, isTrue);
    expect(gcc.alternatives, ['/usr/local/bin/gcc']);
    final pytest = inventory.tools.firstWhere((t) => t.name == 'pytest');
    expect(pytest.versionStatus, ToolVersionStatus.projectLocal);
    expect(pytest.onPath, isFalse);
    expect(pytest.path, '~/project/.venv/bin/pytest');
    final python = inventory.tools.firstWhere((t) => t.name == 'python3');
    expect(python.details, {'py:3.11': '/usr/bin/python3.11'});

    // Grouped in the server's category order.
    expect([
      for (final (category, _) in inventory.byCategory) category
    ], [
      'compiler',
      'build_system',
      'test_runner',
      'debugger_profiler',
      'runtime',
      'vcs',
    ]);
  });

  test('a category filter goes on the query string', () async {
    late ToolInventory inventory;
    final clients = await recordClients(() async {
      inventory =
          await const ToolInventoryApi(_endpoint).get(category: 'compiler');
    }, (_) async => fixtureResponse('tool_inventory_compiler_200.json', 200));
    expect(clients.single.requests.single.url.toString(),
        '$_base/v1/tools/inventory?category=compiler');
    expect(inventory.filteredBy, 'category=compiler');
    expect([for (final t in inventory.tools) t.name], ['clang', 'gcc']);
    // Counts still describe the whole snapshot.
    expect(inventory.total, 8);
  });

  test('bad filters are refused without a request', () async {
    final clients = await recordClients(() async {
      const api = ToolInventoryApi(_endpoint);
      await expectLater(
          api.get(name: 'rm -rf /'),
          throwsA(isA<SonderException>()
              .having((e) => e.code, 'code', 'INVALID_TOOL_NAME')));
      await expectLater(
          api.get(category: 'warp_drive'),
          throwsA(isA<SonderException>()
              .having((e) => e.code, 'code', 'INVALID_TOOL_CATEGORY')));
    }, (_) async => fixtureResponse('tool_inventory_200.json', 200));
    expect(clients, isEmpty);
    expect(isHostToolName('clang++'), isTrue);
    expect(isHostToolName('a/b'), isFalse);
  });

  test('Rediscover POSTs an empty JSON body to the refresh route', () async {
    late ToolInventory inventory;
    final clients = await recordClients(() async {
      inventory = await const ToolInventoryApi(_endpoint).refresh();
    }, (_) async => fixtureResponse('tool_inventory_200.json', 200));
    final request = clients.single.requests.single as http.Request;
    expect(request.method, 'POST');
    expect(request.url.toString(), '$_base/v1/tools/inventory/refresh');
    expect(request.body, '{}');
    expect(inventory.tools, hasLength(8));
  });

  test('server refusals read as sentences with their codes', () async {
    final forbidden = await _failure('tool_inventory_forbidden_403.json', 403);
    expect(forbidden.httpStatus, 403);
    expect(forbidden.code, 'FORBIDDEN');
    expect(forbidden.message,
        'Only an administrator can read the host tool inventory.');

    final busy = await _failure('tool_inventory_busy_429.json', 429);
    expect(busy.code, 'TOOL_INVENTORY_BUSY');
    expect(busy.retryable, isTrue);
    expect(busy.message, contains('already reading its tool inventory'));

    final large = await _failure('tool_inventory_too_large_413.json', 413);
    expect(large.code, 'TOOL_INVENTORY_TOO_LARGE');
    expect(large.message, contains('Pick a category'));

    final missing = await _failure('tool_inventory_unavailable_503.json', 503);
    expect(missing.code, 'TOOL_INVENTORY_UNAVAILABLE');
    expect(
        missing.message, 'This server has no host tool inventory right now.');
  });

  test('a body that is not an inventory is a parse error', () async {
    await recordClients(() async {
      await expectLater(
          const ToolInventoryApi(_endpoint).get(),
          throwsA(isA<SonderException>().having((e) => e.message, 'message',
              'Could not parse the tool inventory.')));
    }, (_) async => fixtureResponse('work_runs_empty.json', 200));
  });

  test('HttpRuntimeDataSource reads the inventory through the same client',
      () async {
    late ToolInventory inventory;
    final clients = await recordClients(() async {
      inventory = await const HttpRuntimeDataSource(baseUrl: _base)
          .toolInventory(category: 'compiler');
    }, (_) async => fixtureResponse('tool_inventory_compiler_200.json', 200));
    expect(clients.single.requests.single.url.path, '/v1/tools/inventory');
    expect(inventory.tools, hasLength(2));
  });

  test('unknown statuses and categories degrade, never throw', () {
    final inventory = ToolInventory.fromJson({
      'object': 'tool_inventory',
      'counts': {'quantum': 1},
      'tools': [
        {'name': 'qc', 'category': 'quantum', 'version_status': 'teleported'},
        {'category': 'compiler'},
        'not a map',
      ],
    });
    expect(inventory.tools.single.versionStatus, ToolVersionStatus.unknown);
    expect(inventory.byCategory.single.$1, 'quantum');
    expect(hostToolCategoryLabel('quantum'), 'quantum');
    expect(hostToolCategoryLabel('vcs'), 'Version control');
  });
  // GET discovers on first use and after the refresh window, which the
  // server bounds at about 35 s; the read must outlast that, not fail at 20 s.
  // testWidgets runs on fake time, so the waits cost nothing.
  testWidgets('a GET that discovers gets the discovery budget', (tester) async {
    Future<http.Response> slow(http.BaseRequest _) async {
      await Future<void>.delayed(const Duration(seconds: 35));
      return fixtureResponse('tool_inventory_compiler_200.json', 200);
    }

    for (final read in <Future<ToolInventory> Function()>[
      () => const ToolInventoryApi(_endpoint).get(),
      () => const HttpRuntimeDataSource(baseUrl: _base)
          .toolInventory(category: 'compiler'),
      () => const HttpRuntimeDataSource(baseUrl: _base).refreshToolInventory(),
    ]) {
      ToolInventory? inventory;
      Object? failure;
      unawaited(recordClients(() async {
        try {
          inventory = await read();
        } catch (error) {
          failure = error;
        }
      }, slow));
      await tester.pump(const Duration(seconds: 36));
      expect(failure, isNull);
      expect(inventory?.tools, hasLength(2));
    }
  });

  testWidgets('the discovery budget still ends a read that never answers',
      (tester) async {
    Object? failure;
    unawaited(recordClients(() async {
      try {
        await const ToolInventoryApi(_endpoint).get();
      } catch (error) {
        failure = error;
      }
    }, (_) => Completer<http.Response>().future));
    await tester.pump(defaultToolInventoryTimeout - const Duration(seconds: 1));
    expect(failure, isNull);
    await tester.pump(const Duration(seconds: 2));
    expect(failure, isA<SonderException>());
  });
}
