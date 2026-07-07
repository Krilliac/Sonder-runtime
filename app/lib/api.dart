import 'dart:convert';
import 'package:http/http.dart' as http;

import 'models.dart';

/// Thin client for a hosted trilobite instance (trilobite_serve.py).
///
/// The server speaks the OpenAI chat-completions dialect:
///   GET  <base>/v1/models
///   POST <base>/v1/chat/completions   { model, messages[], stream }
/// with an optional `Authorization: Bearer <key>` header when the host
/// enabled auth. This mirrors trilobite_client.py, but for a GUI.
class TrilobiteApi {
  final String baseUrl; // e.g. http://192.168.1.10:11435
  final String apiKey; // empty when the server has auth disabled

  const TrilobiteApi({required this.baseUrl, this.apiKey = ''});

  Uri _uri(String path) {
    final root = baseUrl.trim().replaceAll(RegExp(r'/+$'), '');
    return Uri.parse('$root$path');
  }

  Map<String, String> _headers() {
    final h = <String, String>{'Content-Type': 'application/json'};
    if (apiKey.trim().isNotEmpty) {
      h['Authorization'] = 'Bearer ${apiKey.trim()}';
    }
    return h;
  }

  /// Verify connectivity + auth. Returns the list of model ids the server
  /// advertises (typically just ["trilobite"]). Throws [TrilobiteException]
  /// on any failure so the UI can show a precise reason.
  Future<List<String>> listModels() async {
    late http.Response resp;
    try {
      resp = await http
          .get(_uri('/v1/models'), headers: _headers())
          .timeout(const Duration(seconds: 15));
    } catch (e) {
      throw TrilobiteException('Cannot reach server: $e');
    }
    if (resp.statusCode == 401) {
      throw TrilobiteException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw TrilobiteException('Server returned HTTP ${resp.statusCode}.');
    }
    try {
      final obj = jsonDecode(resp.body) as Map<String, dynamic>;
      final data = (obj['data'] as List?) ?? const [];
      return data
          .map((m) => (m as Map<String, dynamic>)['id']?.toString() ?? '')
          .where((s) => s.isNotEmpty)
          .toList();
    } catch (_) {
      throw TrilobiteException('Unexpected response from server.');
    }
  }

  /// Send the full conversation and return the assistant's reply text.
  ///
  /// The serve layer threads history from the messages we send, and also
  /// handles slash-commands (/stats, /train, /pass, …), passive feedback,
  /// and natural-language control — so we simply forward whatever the user
  /// typed as the last user message.
  Future<String> chat(List<ChatMessage> messages,
      {String model = 'trilobite'}) async {
    final body = jsonEncode({
      'model': model,
      'messages': messages
          .where((m) => !m.pending)
          .map((m) => m.toWire())
          .toList(),
      'stream': false,
    });

    late http.Response resp;
    try {
      resp = await http
          .post(_uri('/v1/chat/completions'),
              headers: _headers(), body: body)
          .timeout(const Duration(minutes: 5));
    } catch (e) {
      throw TrilobiteException('Request failed: $e');
    }

    if (resp.statusCode == 401) {
      throw TrilobiteException('Unauthorized — check the API key.');
    }
    if (resp.statusCode != 200) {
      throw TrilobiteException('Server returned HTTP ${resp.statusCode}.');
    }

    try {
      final obj = jsonDecode(utf8.decode(resp.bodyBytes))
          as Map<String, dynamic>;
      final choices = (obj['choices'] as List?) ?? const [];
      if (choices.isEmpty) {
        throw TrilobiteException('Empty response from server.');
      }
      final msg = (choices.first as Map<String, dynamic>)['message']
          as Map<String, dynamic>?;
      final content = msg?['content']?.toString() ?? '';
      return content.trimRight();
    } on TrilobiteException {
      rethrow;
    } catch (_) {
      throw TrilobiteException('Could not parse server response.');
    }
  }
}

class TrilobiteException implements Exception {
  final String message;
  TrilobiteException(this.message);
  @override
  String toString() => message;
}
