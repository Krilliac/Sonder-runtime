import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme.dart';
import 'feedback.dart';

/// A decoded JSON value (tool arguments, a receipt, a policy) shown as
/// readable fields instead of a JSON dump: one row per key, the key in mono,
/// long or multi-line strings clipped behind "Show more", nested values as
/// compact JSON. "Raw JSON" swaps in the exact pretty-printed value and Copy
/// copies it, so the friendlier view never hides anything.
class StructuredFields extends StatefulWidget {
  final Object? value;

  /// What the value is ("Arguments"); also names the copy action.
  final String label;

  /// A string longer than this many characters is clipped.
  final int clipChars;

  /// A multi-line string shows this many lines until expanded.
  final int clipLines;

  final bool initiallyRaw;

  const StructuredFields(
    this.value, {
    super.key,
    this.label = 'Fields',
    this.clipChars = 160,
    this.clipLines = 3,
    this.initiallyRaw = false,
  });

  /// The exact value as indented JSON; non-JSON values fall back to text.
  static String prettyJson(Object? value) {
    try {
      return const JsonEncoder.withIndent('  ').convert(value);
    } on JsonUnsupportedObjectError {
      return '$value';
    }
  }

  @override
  State<StructuredFields> createState() => _StructuredFieldsState();
}

class _StructuredFieldsState extends State<StructuredFields> {
  late bool _raw = widget.initiallyRaw;
  final _open = <String>{};

  List<(String, Object?)> get _entries {
    final value = widget.value;
    if (value is Map) {
      return [for (final e in value.entries) ('${e.key}', e.value)];
    }
    if (value is List) {
      return [for (var i = 0; i < value.length; i++) ('$i', value[i])];
    }
    return [('', value)];
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final entries = _entries;
    final empty = (widget.value is Map && (widget.value as Map).isEmpty) ||
        (widget.value is List && (widget.value as List).isEmpty);
    final Widget body;
    if (_raw) {
      body = SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.all(SonderSpace.md),
        child: Text(StructuredFields.prettyJson(widget.value),
            style: tokens.mono(12, color: tokens.text2, height: 18)),
      );
    } else if (empty) {
      body = Padding(
        padding: const EdgeInsets.all(SonderSpace.md),
        child: Text('No ${widget.label.toLowerCase()}',
            style: text.bodySmall?.copyWith(color: tokens.muted)),
      );
    } else {
      body = LayoutBuilder(builder: (context, constraints) {
        final stacked = constraints.maxWidth < 420;
        final rows = <Widget>[];
        for (var i = 0; i < entries.length; i++) {
          if (i > 0) {
            rows.add(Divider(height: 1, thickness: 1, color: tokens.hairline));
          }
          rows.add(_row(context, entries[i].$1, entries[i].$2, stacked));
        }
        return Column(
            crossAxisAlignment: CrossAxisAlignment.stretch, children: rows);
      });
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(children: [
          Expanded(
            child: Semantics(
              header: true,
              child: Text(widget.label, style: text.labelMedium),
            ),
          ),
          TextButton(
            onPressed: () => setState(() => _raw = !_raw),
            child: Text(_raw ? 'Fields' : 'Raw JSON'),
          ),
          IconButton(
            tooltip: 'Copy ${widget.label.toLowerCase()}',
            iconSize: 16,
            icon: const Icon(Icons.copy_outlined),
            onPressed: () async {
              await Clipboard.setData(ClipboardData(
                  text: StructuredFields.prettyJson(widget.value)));
              if (context.mounted) {
                showSonderToast(context, '${widget.label} copied');
              }
            },
          ),
        ]),
        Container(
          decoration: BoxDecoration(
            color: tokens.canvas,
            borderRadius: BorderRadius.circular(SonderRadius.row),
            border: Border.all(color: tokens.hairline),
          ),
          clipBehavior: Clip.antiAlias,
          // One selection area instead of a selectable text per value:
          // values stay selectable without each becoming a tiny
          // long-press target for assistive technology.
          child: SelectionArea(child: body),
        ),
      ],
    );
  }

  Widget _row(BuildContext context, String key, Object? value, bool stacked) {
    final tokens = SonderTokens.of(context);
    final shown = _shown(key, value);
    final keyText = key.isEmpty
        ? null
        : Text(key,
            style: tokens.mono(12, color: tokens.muted),
            maxLines: stacked ? 2 : 1,
            overflow: TextOverflow.ellipsis);
    final valueWidget = shown.text == null
        ? Text('null', style: tokens.mono(12, color: tokens.muted))
        : Text(shown.text!,
            style: tokens.mono(12, color: tokens.text, height: 18));
    final toggle = shown.toggle == null
        ? null
        : Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              style: TextButton.styleFrom(
                padding: EdgeInsets.zero,
                minimumSize: const Size(0, 32),
                foregroundColor: tokens.accentText,
              ),
              onPressed: () => setState(() {
                if (!_open.remove(key)) _open.add(key);
              }),
              child: Text(shown.toggle!),
            ),
          );
    final Widget content;
    if (keyText == null || stacked) {
      content = Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (keyText != null) ...[
            keyText,
            const SizedBox(height: SonderSpace.xxs),
          ],
          valueWidget,
          if (toggle != null) toggle,
        ],
      );
    } else {
      // The toggle sits under the key, so a clipped value is not followed
      // by a band of empty space.
      content = Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 132,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [keyText, if (toggle != null) toggle],
            ),
          ),
          const SizedBox(width: SonderSpace.md),
          Expanded(child: valueWidget),
        ],
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(
          horizontal: SonderSpace.md, vertical: SonderSpace.sm),
      child: content,
    );
  }

  /// The text a value shows (null for JSON null) and, for long values, the
  /// label of the control that shows all of it or less again.
  ({String? text, String? toggle}) _shown(String key, Object? value) {
    if (value == null) return (text: null, toggle: null);
    if (value is num || value is bool) return (text: '$value', toggle: null);
    final nested = value is Map || value is List;
    final full = nested
        ? StructuredFields.prettyJson(value)
        : value is String
            ? value
            : '$value';
    final compact = nested ? jsonEncode(value) : full;
    final lines = compact.split('\n');
    final long =
        lines.length > widget.clipLines || compact.length > widget.clipChars;
    if (!long) return (text: compact, toggle: null);
    if (_open.contains(key)) return (text: full, toggle: 'Show less');
    var shown = lines.take(widget.clipLines).join('\n');
    if (shown.length > widget.clipChars) {
      shown = shown.substring(0, widget.clipChars);
    }
    return (
      text: '${shown.trimRight()}…',
      toggle: lines.length > widget.clipLines
          ? 'Show all ${full.split('\n').length} lines'
          : 'Show all ${full.length} characters',
    );
  }
}
