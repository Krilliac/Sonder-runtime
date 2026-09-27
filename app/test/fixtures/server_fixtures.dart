import 'dart:async';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// A server response body from `test/fixtures/server/` (see FIXTURES.md).
String serverFixture(String name) =>
    File('test/fixtures/server/$name').readAsStringSync();

/// JSON response headers the server sends.
const jsonHeaders = {'content-type': 'application/json'};

/// A response built from a fixture.
http.Response fixtureResponse(String name, int status,
        {Map<String, String> headers = jsonHeaders}) =>
    http.Response(serverFixture(name), status, headers: headers);

/// One `http.Client` created by the code under test, with what it saw.
class RecordingClient extends http.BaseClient {
  final http.Client inner;
  final List<http.BaseRequest> requests = [];
  int closes = 0;

  RecordingClient(this.inner);

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    requests.add(request);
    return inner.send(request);
  }

  @override
  void close() {
    closes++;
    inner.close();
  }
}

/// Runs [body] with every `http.Client()` replaced by a [RecordingClient]
/// around [handler]; returns the clients in creation order.
Future<List<RecordingClient>> recordClients(
  Future<void> Function() body,
  MockClientHandler handler,
) async {
  final clients = <RecordingClient>[];
  await http.runWithClient(body, () {
    final client = RecordingClient(MockClient(handler));
    clients.add(client);
    return client;
  });
  return clients;
}

/// Like [recordClients] for streaming handlers.
Future<List<RecordingClient>> recordStreamingClients(
  Future<void> Function() body,
  MockClientStreamHandler handler,
) async {
  final clients = <RecordingClient>[];
  await http.runWithClient(body, () {
    final client = RecordingClient(MockClient.streaming(handler));
    clients.add(client);
    return client;
  });
  return clients;
}

/// Delay helper for handlers.
Future<void> pause(int ms) => Future<void>.delayed(Duration(milliseconds: ms));
