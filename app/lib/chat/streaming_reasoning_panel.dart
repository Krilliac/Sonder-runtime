import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';

import '../theme.dart';
import '../ui/kit.dart' show HoverSurface, SonderReveal;
import '../ui/status_line.dart' show compactCount;

/// Collapsible live reasoning/thought panel shown during a streaming turn.
///
/// Opens automatically while the model is actively thinking (before answer text
/// starts), and smoothly folds down once the answer begins streaming unless the
/// reader manually toggled it. Inside the expanded body, long reasoning chains
/// scroll inside a bounded viewport and follow new tokens without fighting an
/// upward scroll by the reader.
class StreamingReasoningPanel extends StatefulWidget {
  final String reasoning;

  /// True while reasoning is streaming and no answer content has arrived yet.
  final bool thinkingActive;

  const StreamingReasoningPanel({
    super.key,
    required this.reasoning,
    required this.thinkingActive,
  });

  @override
  State<StreamingReasoningPanel> createState() =>
      _StreamingReasoningPanelState();
}

class _StreamingReasoningPanelState extends State<StreamingReasoningPanel> {
  final ScrollController _bodyScroll = ScrollController();

  late bool _expanded = widget.thinkingActive;
  bool _userOverride = false;
  bool _bodyDetached = false;
  bool _scrollScheduled = false;

  @override
  void didUpdateWidget(covariant StreamingReasoningPanel oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!_userOverride && oldWidget.thinkingActive != widget.thinkingActive) {
      _expanded = widget.thinkingActive;
    }
    if (_expanded &&
        !_bodyDetached &&
        widget.reasoning.length > oldWidget.reasoning.length) {
      _scrollBodyToEnd();
    }
  }

  @override
  void dispose() {
    _bodyScroll.dispose();
    super.dispose();
  }

  void _toggle() {
    setState(() {
      _userOverride = true;
      _expanded = !_expanded;
      if (_expanded) _bodyDetached = false;
    });
    if (_expanded) _scrollBodyToEnd();
  }

  void _scrollBodyToEnd() {
    if (_scrollScheduled) return;
    _scrollScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _scrollScheduled = false;
      if (!mounted || !_expanded || _bodyDetached || !_bodyScroll.hasClients) {
        return;
      }
      final position = _bodyScroll.position;
      if (position.userScrollDirection == ScrollDirection.forward) {
        _bodyDetached = true;
        return;
      }
      final distance = position.maxScrollExtent - position.pixels;
      if (distance > 0.5) {
        position.jumpTo(position.maxScrollExtent);
      }
    });
  }

  bool _onBodyScroll(ScrollNotification notification) {
    final distance =
        notification.metrics.maxScrollExtent - notification.metrics.pixels;
    if (notification is UserScrollNotification) {
      if (notification.direction == ScrollDirection.forward) {
        _bodyDetached = true;
      } else if (notification.direction == ScrollDirection.reverse &&
          distance <= 24) {
        _bodyDetached = false;
      }
    } else if (notification is ScrollUpdateNotification) {
      final delta = notification.scrollDelta ?? 0;
      if (delta < -0.5 && distance > 2) {
        _bodyDetached = true;
      } else if (delta > 0 && distance <= 24) {
        _bodyDetached = false;
      }
    } else if (notification is ScrollEndNotification && distance <= 4) {
      _bodyDetached = false;
    }
    return false;
  }

  static String _sizeSummary(String reasoning) {
    final trimmed = reasoning.trim();
    if (trimmed.length < 40) return '';
    final estTokens = (trimmed.length + 3) ~/ 4;
    return '~${compactCount(estTokens)} tok';
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final thinkingActive = widget.thinkingActive;
    final trimmed = widget.reasoning.trimRight();
    final label = thinkingActive ? 'Thinking…' : 'Reasoning';
    final sizeSummary = _sizeSummary(trimmed);
    final accentColor = thinkingActive ? tokens.accent : tokens.muted;

    return Container(
      key: const Key('streaming-reasoning'),
      decoration: BoxDecoration(
        color: tokens.panel,
        borderRadius: const BorderRadius.horizontal(
          right: Radius.circular(SonderRadius.row),
        ),
        border: Border(
          left: BorderSide(color: accentColor, width: 2),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          HoverSurface(
            key: const Key('streaming-reasoning-toggle'),
            onTap: _toggle,
            borderRadius: const BorderRadius.horizontal(
              right: Radius.circular(SonderRadius.row),
            ),
            semanticLabel:
                '$label${sizeSummary.isNotEmpty ? ', $sizeSummary' : ''}, '
                '${_expanded ? 'expanded' : 'collapsed'}',
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 48),
              child: Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: SonderSpace.md,
                  vertical: SonderSpace.xs,
                ),
                child: ExcludeSemantics(
                  child: Row(
                    children: [
                      Icon(
                        Icons.psychology_outlined,
                        size: 15,
                        color: accentColor,
                      ),
                      const SizedBox(width: SonderSpace.xs),
                      Flexible(
                        child: Text(
                          label,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: tokens.mono(
                            11.5,
                            color:
                                thinkingActive ? tokens.text2 : tokens.muted,
                            weight: thinkingActive
                                ? FontWeight.w500
                                : FontWeight.w400,
                          ),
                        ),
                      ),
                      if (sizeSummary.isNotEmpty) ...[
                        const SizedBox(width: SonderSpace.sm),
                        Text(
                          sizeSummary,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: tokens.mono(11, color: tokens.muted),
                        ),
                      ],
                      const Spacer(),
                      AnimatedRotation(
                        turns: _expanded ? 0.25 : 0,
                        duration: SonderMotion.of(context, SonderMotion.fast),
                        curve: SonderMotion.standard,
                        child: Icon(
                          Icons.chevron_right,
                          size: 18,
                          color: tokens.muted,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
          SonderReveal(
            visible: _expanded,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(
                SonderSpace.md,
                0,
                SonderSpace.md,
                SonderSpace.sm,
              ),
              child: ConstrainedBox(
                key: const Key('streaming-reasoning-body'),
                constraints: const BoxConstraints(maxHeight: 220),
                child: NotificationListener<ScrollNotification>(
                  onNotification: _onBodyScroll,
                  child: SingleChildScrollView(
                    controller: _bodyScroll,
                    child: Align(
                      alignment: Alignment.topLeft,
                      child: SelectableText(
                        trimmed,
                        style: tokens.mono(12, color: tokens.muted),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
