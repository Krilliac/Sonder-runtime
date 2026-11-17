import 'dart:io';

import '../chat_store.dart';

/// One JSON file per thread under `<path>/chats/`.
ChatStoreBackend? directoryBackend(String path) =>
    DirectoryChatStoreBackend(Directory(path));

class DirectoryChatStoreBackend implements ChatStoreBackend {
  final Directory root;
  DirectoryChatStoreBackend(Directory base)
      : root = Directory('${base.path}${Platform.pathSeparator}chats');

  static int _tmpCounter = 0;

  File _file(String name) => File('${root.path}${Platform.pathSeparator}$name');

  @override
  Future<List<String>> list() async {
    if (!await root.exists()) return const [];
    return root
        .list()
        .where((e) => e is File)
        .map((e) => e.uri.pathSegments.last)
        .toList();
  }

  @override
  Future<String?> read(String name) async {
    final f = _file(name);
    if (!await f.exists()) return null;
    return f.readAsString();
  }

  @override
  Future<void> write(String name, String data) async {
    await root.create(recursive: true);
    // Write-then-rename so a crash mid-write never leaves a torn thread.
    // The temp name is unique per write so overlapping writes never share
    // (and truncate) one file.
    final tmp = _file('$name.${pid}_${_tmpCounter++}.tmp');
    try {
      await tmp.writeAsString(data, flush: true);
      await tmp.rename(_file(name).path);
    } catch (_) {
      try {
        if (await tmp.exists()) await tmp.delete();
      } catch (_) {}
      rethrow;
    }
  }

  @override
  Future<void> delete(String name) async {
    final f = _file(name);
    if (await f.exists()) await f.delete();
  }

  @override
  Future<void> rename(String from, String to) async {
    final f = _file(from);
    if (await f.exists()) await f.rename(_file(to).path);
  }
}
