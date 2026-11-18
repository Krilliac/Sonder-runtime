import 'package:flutter/material.dart';

import '../../theme.dart';
import 'motion.dart';

/// One page of a categorized surface (Runtime, Settings): its rail entry and
/// the builder for its content.
class SonderCategory {
  /// Stable id, also used in keys: `category-<id>` (rail entry) and
  /// `category-page-<id>` (page body).
  final String id;
  final String label;
  final IconData icon;

  /// One line under the page title, and the subtitle in the narrow list.
  final String? description;

  /// Rail group heading; consecutive categories with the same group share it.
  final String? group;

  /// The role required to see this category.
  final String? requiredRole;

  /// Trailing rail widget: a count or a status. It must carry text, never
  /// colour alone (see `CountBadge` and `StatusPill`).
  final Widget? badge;

  /// Extra words the rail search matches (setting names on the page).
  final List<String> keywords;
  final WidgetBuilder builder;

  const SonderCategory({
    required this.id,
    required this.label,
    required this.icon,
    required this.builder,
    this.description,
    this.group,
    this.requiredRole,
    this.badge,
    this.keywords = const [],
  });

  bool matches(String query) {
    final q = query.trim().toLowerCase();
    if (q.isEmpty) return true;
    return label.toLowerCase().contains(q) ||
        (description?.toLowerCase().contains(q) ?? false) ||
        keywords.any((k) => k.toLowerCase().contains(q));
  }
}

/// Lets a page inside a [CategoryScaffold] jump to another category, e.g. an
/// overview tile that opens "Approvals".
class CategoryNavigator extends InheritedWidget {
  final String selectedId;
  final ValueChanged<String> select;

  const CategoryNavigator({
    super.key,
    required this.selectedId,
    required this.select,
    required super.child,
  });

  static CategoryNavigator? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<CategoryNavigator>();

  @override
  bool updateShouldNotify(CategoryNavigator oldWidget) =>
      selectedId != oldWidget.selectedId;
}

/// A two-pane categorized surface, the settings layout people know from
/// desktop chat apps: a category rail on the left, one page at a time on the
/// right. The rail shares the page's surface; the selected category is a
/// soft pill, and space rather than a wall separates the panes. Below
/// [wideBreakpoint] it becomes a list of categories that opens each page
/// full width, with a back arrow to the list.
///
/// Selection is uncontrolled by default ([initialId]); pass [selectedId] and
/// [onSelected] to control it from outside (deep links, persistence).
class CategoryScaffold extends StatefulWidget {
  final String title;
  final List<SonderCategory> categories;
  final String? selectedId;
  final String? initialId;
  final ValueChanged<String>? onSelected;
  final String? userRole;

  /// App bar actions, shown in both layouts.
  final List<Widget> actions;

  /// App bar leading widget for the wide layout and the narrow list.
  final Widget? leading;

  /// Width of [leading], for a labelled control such as "← Chat" that does
  /// not fit the default square slot.
  final double? leadingWidth;

  /// Shown above every page's content, e.g. an offline or stale-data notice.
  final Widget? banner;

  /// Pinned under the content, e.g. an unsaved-changes bar.
  final Widget? bottomBar;

  /// Shown at the bottom of the rail (wide) or the list (narrow).
  final Widget? railFooter;
  final double wideBreakpoint;
  final double railWidth;
  final double contentMaxWidth;
  final bool searchable;
  final String searchHint;

  /// Key for the rail (wide) or category list (narrow) container.
  final Key? navigationKey;

  const CategoryScaffold({
    super.key,
    required this.title,
    required this.categories,
    this.selectedId,
    this.initialId,
    this.onSelected,
    this.userRole,
    this.actions = const [],
    this.leading,
    this.leadingWidth,
    this.banner,
    this.bottomBar,
    this.railFooter,
    this.wideBreakpoint = 840,
    this.railWidth = 248,
    this.contentMaxWidth = 860,
    this.searchable = true,
    this.searchHint = 'Search',
    this.navigationKey,
  }) : assert(categories.length > 0);

  @override
  State<CategoryScaffold> createState() => _CategoryScaffoldState();
}

class _CategoryScaffoldState extends State<CategoryScaffold> {
  String? _internal;
  String _query = '';
  final _search = TextEditingController();

  /// Narrow layout only: whether a page (not the list) is showing. A deep
  /// link ([CategoryScaffold.initialId] or a controlled id) opens the page.
  late bool _narrowDetail =
      widget.initialId != null || widget.selectedId != null;

  List<SonderCategory> get _allowedCategories {
    if (widget.userRole == null) return widget.categories;
    return widget.categories.where((c) {
      if (c.requiredRole == null) return true;
      return c.requiredRole == widget.userRole;
    }).toList();
  }

  String get _selected {
    final wanted = widget.selectedId ?? _internal ?? widget.initialId;
    if (wanted != null && _allowedCategories.any((c) => c.id == wanted)) {
      return wanted;
    }
    return _allowedCategories.first.id;
  }

  SonderCategory get _current =>
      _allowedCategories.firstWhere((c) => c.id == _selected);

  void _select(String id) {
    setState(() {
      if (widget.selectedId == null) _internal = id;
      _narrowDetail = true;
    });
    widget.onSelected?.call(id);
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final wide = constraints.maxWidth >= widget.wideBreakpoint;
      final navigator = CategoryNavigator(
        selectedId: _selected,
        select: _select,
        child: wide ? _wide(context) : _narrow(context),
      );
      return navigator;
    });
  }

  Widget _wide(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: widget.leading,
        leadingWidth: widget.leading == null ? null : widget.leadingWidth,
        automaticallyImplyLeading: widget.leading == null,
        title: Text(widget.title),
        actions: [...widget.actions, const SizedBox(width: SonderSpace.sm)],
      ),
      body: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(
            key: widget.navigationKey,
            width: widget.railWidth,
            child: _rail(context),
          ),
          Expanded(
            child: Column(children: [
              Expanded(
                child: FocusTraversalGroup(
                  child: SonderSwitcher(
                    child: KeyedSubtree(
                      key: ValueKey('page-$_selected'),
                      child: _page(context, _current),
                    ),
                  ),
                ),
              ),
              if (widget.bottomBar != null) widget.bottomBar!,
            ]),
          ),
        ],
      ),
    );
  }

  Widget _rail(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final visible = _allowedCategories.where((c) => c.matches(_query)).toList();
    final children = <Widget>[];
    String? group;
    for (final c in visible) {
      if (c.group != null && c.group != group) {
        children.add(Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.md, SonderSpace.lg, SonderSpace.md, SonderSpace.xs),
          child: Text(c.group!, style: text.labelSmall),
        ));
      }
      group = c.group;
      children.add(_RailItem(
        category: c,
        selected: c.id == _selected,
        onTap: () => _select(c.id),
      ));
    }
    if (visible.isEmpty) {
      children.add(Padding(
        padding: const EdgeInsets.all(SonderSpace.md),
        child: Text('No matches',
            style: text.bodySmall?.copyWith(color: tokens.muted)),
      ));
    }
    return FocusTraversalGroup(
      child: Column(children: [
        if (widget.searchable)
          Padding(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.md, SonderSpace.md, SonderSpace.sm, SonderSpace.xs),
            child: _SearchField(
              controller: _search,
              hint: widget.searchHint,
              onChanged: (q) => setState(() => _query = q),
            ),
          ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(
                SonderSpace.md, SonderSpace.xs, SonderSpace.sm, SonderSpace.md),
            children: children,
          ),
        ),
        if (widget.railFooter != null) widget.railFooter!,
      ]),
    );
  }

  Widget _narrow(BuildContext context) {
    final current = _current;
    if (_narrowDetail) {
      return PopScope(
        canPop: false,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop) setState(() => _narrowDetail = false);
        },
        child: Scaffold(
          appBar: AppBar(
            leading: IconButton(
              tooltip: 'All ${widget.title.toLowerCase()} sections',
              icon: const Icon(Icons.arrow_back),
              onPressed: () => setState(() => _narrowDetail = false),
            ),
            title: Text(current.label),
            actions: [...widget.actions, const SizedBox(width: SonderSpace.xs)],
          ),
          body: Column(children: [
            Expanded(
              child: SonderSwitcher(
                child: KeyedSubtree(
                  key: ValueKey('page-${current.id}'),
                  child: _page(context, current, narrow: true),
                ),
              ),
            ),
            if (widget.bottomBar != null) widget.bottomBar!,
          ]),
        ),
      );
    }
    final text = Theme.of(context).textTheme;
    final tokens = SonderTokens.of(context);
    final visible = _allowedCategories.where((c) => c.matches(_query)).toList();
    final rows = <Widget>[];
    String? group;
    for (final c in visible) {
      if (c.group != null && c.group != group) {
        rows.add(Padding(
          padding: const EdgeInsets.fromLTRB(
              SonderSpace.lg, SonderSpace.lg, SonderSpace.lg, SonderSpace.xs),
          child: Text(c.group!, style: text.labelSmall),
        ));
      }
      group = c.group;
      rows.add(_ListItem(category: c, onTap: () => _select(c.id)));
    }
    return Scaffold(
      appBar: AppBar(
        leading: widget.leading,
        leadingWidth: widget.leading == null ? null : widget.leadingWidth,
        automaticallyImplyLeading: widget.leading == null,
        title: Text(widget.title),
        actions: [...widget.actions, const SizedBox(width: SonderSpace.xs)],
      ),
      body: Column(children: [
        Expanded(
          child: ListView(
            key: widget.navigationKey,
            padding: const EdgeInsets.only(bottom: SonderSpace.xxl),
            children: [
              if (widget.banner != null)
                Padding(
                  padding: const EdgeInsets.fromLTRB(SonderSpace.lg,
                      SonderSpace.md, SonderSpace.lg, 0),
                  child: widget.banner!,
                ),
              if (widget.searchable)
                Padding(
                  padding: const EdgeInsets.fromLTRB(SonderSpace.lg,
                      SonderSpace.md, SonderSpace.lg, SonderSpace.xs),
                  child: _SearchField(
                    controller: _search,
                    hint: widget.searchHint,
                    onChanged: (q) => setState(() => _query = q),
                  ),
                ),
              ...rows,
              if (visible.isEmpty)
                Padding(
                  padding: const EdgeInsets.all(SonderSpace.lg),
                  child: Text('No matches',
                      style: text.bodySmall?.copyWith(color: tokens.muted)),
                ),
              if (widget.railFooter != null) widget.railFooter!,
            ],
          ),
        ),
        if (widget.bottomBar != null) widget.bottomBar!,
      ]),
    );
  }

  Widget _page(BuildContext context, SonderCategory c, {bool narrow = false}) {
    final text = Theme.of(context).textTheme;
    final tokens = SonderTokens.of(context);
    final horizontal = narrow ? SonderSpace.lg : SonderSpace.x3;
    return SingleChildScrollView(
      key: PageStorageKey('category-scroll-${c.id}'),
      padding: EdgeInsets.fromLTRB(
          horizontal, narrow ? SonderSpace.lg : SonderSpace.xxl, horizontal,
          SonderSpace.x4),
      child: Align(
        alignment: Alignment.topCenter,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxWidth: widget.contentMaxWidth),
          child: Column(
            key: Key('category-page-${c.id}'),
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (!narrow) ...[
                Semantics(
                  header: true,
                  child: Text(c.label, style: text.headlineSmall),
                ),
                if (c.description != null) ...[
                  const SizedBox(height: SonderSpace.xs),
                  Text(c.description!,
                      style: text.bodyMedium?.copyWith(color: tokens.text2)),
                ],
                const SizedBox(height: SonderSpace.xl),
              ] else if (c.description != null) ...[
                Text(c.description!,
                    style: text.bodyMedium?.copyWith(color: tokens.text2)),
                const SizedBox(height: SonderSpace.lg),
              ],
              if (widget.banner != null && !narrow) ...[
                widget.banner!,
                const SizedBox(height: SonderSpace.lg),
              ],
              Builder(builder: c.builder),
            ],
          ),
        ),
      ),
    );
  }
}

class _SearchField extends StatelessWidget {
  final TextEditingController controller;
  final String hint;
  final ValueChanged<String> onChanged;

  const _SearchField({
    required this.controller,
    required this.hint,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    return SonderTextField(
      fieldKey: const Key('category-search'),
      controller: controller,
      onChanged: onChanged,
      hint: hint,
      prefix: Icon(Icons.search, size: 18, color: tokens.muted),
      suffix: ValueListenableBuilder<TextEditingValue>(
        valueListenable: controller,
        builder: (context, value, _) => value.text.isEmpty
            ? const SizedBox.shrink()
            : IconButton(
                tooltip: 'Clear search',
                iconSize: 16,
                icon: const Icon(Icons.close),
                onPressed: () {
                  controller.clear();
                  onChanged('');
                },
              ),
      ),
    );
  }
}

class _RailItem extends StatelessWidget {
  final SonderCategory category;
  final bool selected;
  final VoidCallback onTap;

  const _RailItem({
    required this.category,
    required this.selected,
    required this.onTap,
  });

  /// The row is a 48 px target; the pill drawn inside it is this much
  /// shorter at the top and the bottom.
  static const EdgeInsets _pillInset =
      EdgeInsets.symmetric(vertical: SonderSpace.xs);

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return HoverSurface(
      key: Key('category-${category.id}'),
      selected: selected,
      onTap: onTap,
      inset: _pillInset,
      child: Container(
        constraints: BoxConstraints(
            minHeight: SonderSpace.x5 - _pillInset.vertical),
        alignment: Alignment.centerLeft,
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.md, vertical: SonderSpace.sm),
        child: Row(children: [
          Icon(category.icon,
              size: 18,
              color: selected ? tokens.accentText : tokens.text2),
          const SizedBox(width: SonderSpace.md),
          Expanded(
            child: Text(
              category.label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: text.bodyMedium?.copyWith(
                color: selected ? tokens.text : tokens.text2,
                fontWeight: selected ? FontWeight.w500 : FontWeight.w400,
              ),
            ),
          ),
          if (category.badge != null) ...[
            const SizedBox(width: SonderSpace.sm),
            category.badge!,
          ],
        ]),
      ),
    );
  }
}

class _ListItem extends StatelessWidget {
  final SonderCategory category;
  final VoidCallback onTap;

  const _ListItem({required this.category, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    return HoverSurface(
      key: Key('category-${category.id}'),
      onTap: onTap,
      borderRadius: BorderRadius.zero,
      child: Padding(
        padding: const EdgeInsets.symmetric(
            horizontal: SonderSpace.lg, vertical: SonderSpace.md),
        child: Row(children: [
          Container(
            width: SonderSpace.x4,
            height: SonderSpace.x4,
            decoration: BoxDecoration(
              color: tokens.raised,
              borderRadius: BorderRadius.circular(SonderRadius.row),
            ),
            child: Icon(category.icon, size: 18, color: tokens.text2),
          ),
          const SizedBox(width: SonderSpace.md),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(category.label,
                    style: text.bodyMedium
                        ?.copyWith(fontWeight: FontWeight.w500)),
                if (category.description != null)
                  Text(category.description!,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: text.bodySmall),
              ],
            ),
          ),
          if (category.badge != null) ...[
            const SizedBox(width: SonderSpace.sm),
            category.badge!,
          ],
          const SizedBox(width: SonderSpace.xs),
          Icon(Icons.chevron_right, size: 20, color: tokens.muted),
        ]),
      ),
    );
  }
}
