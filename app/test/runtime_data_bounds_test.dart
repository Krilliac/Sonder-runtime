// Runtime panel reads are bounded in bytes and in time, body included: a
// hostile or broken endpoint cannot exhaust the app's memory or hold a
// panel open past its timeout by streaming an endless body.
import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:sonder_runtime/api.dart';
import 'package:sonder_runtime/runtime/runtime_data.dart';

void main() {
  test('an oversized body is refused without being read to the end', () async {
    var chunksSent = 0;
    final client = MockClient.streaming((request, _) async {
      Stream<List<int>> body() async* {
        yield '{"data": ['.codeUnits;
        for (var i = 0; i < 100000; i++) {
          chunksSent++;
          yield List<int>.filled(1024, 0x20);
        }
        yield ']}'.codeUnits;
      }

      return http.StreamedResponse(body(), 200);
    });
    Object? error;
    await http.runWithClient(() async {
      try {
        await const HttpRuntimeDataSource(baseUrl: 'http://127.0.0.1:11435')
            .jobs();
      } catch (e) {
        error = e;
      }
    }, () => client);
    expect(error, isA<SonderException>());
    expect((error as SonderException).message, contains('too large'));
    // Stopped near the cap, not after ~100 MB.
    expect(chunksSent * 1024, lessThan(runtimeResponseMaxBytes * 2));
  });

  test('a body that never finishes times out', () async {
    final never = StreamController<List<int>>();
    addTearDown(never.close);
    final client = MockClient.streaming((request, _) async {
      never.add('{"data": ['.codeUnits);
      return http.StreamedResponse(never.stream, 200);
    });
    Object? error;
    final watch = Stopwatch()..start();
    await http.runWithClient(() async {
      try {
        await const HttpRuntimeDataSource(
                baseUrl: 'http://127.0.0.1:11435',
                timeout: Duration(milliseconds: 300))
            .jobs();
      } catch (e) {
        error = e;
      }
    }, () => client);
    expect(error, isA<SonderException>());
    expect((error as SonderException).message, contains('in time'));
    expect(watch.elapsed, lessThan(const Duration(seconds: 5)));
  });
}
