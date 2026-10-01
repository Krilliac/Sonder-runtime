part of '../runtime_screen.dart';

/// Activity: what is running now and what just ran. Work runs, agents and
/// fleets, autopilot, the live execution feed, workbench activity and jobs.
class _ActivityPage extends StatelessWidget {
  final _RuntimeScreenState s;
  const _ActivityPage(this.s);

  @override
  Widget build(BuildContext context) {
    final info = s._info;
    final response = info?.activity?.displayResponse;
    return _PageColumn(children: [
      WorkRunsPanel(
        runs: s._workRuns,
        error: s._workRunsError,
        loading: s._loadingExtras,
        now: s.widget.now,
        onRefresh: s._loadExtras,
        onStop: s._stopWorkRun,
        stopping: s._stopping,
        outcomes: s._runOutcomes,
        onDismissOutcome: (id) => s._dismissRunOutcome(id),
      ),
      if (info == null)
        _NotLoadedSection(title: 'Agents', loading: s._loading)
      else
        _AgentStatusPanel(s: s, status: info.agents),
      _AutopilotComposer(s),
      if (info?.autopilot?.latest != null) ...[
        _AutopilotRunCard(s: s, status: info!.autopilot!),
      ],
      LiveExecutionFeed(
        feed: info?.executionFeed,
        offline: info == null && (s._offline || s._serverError != null),
      ),
      if (response != null)
        WorkbenchActivityPanel(
          response: response,
          totalToolCalls: info!.activity!.totalToolCalls,
        ),
      JobsList(source: s._data, now: s.widget.now),
    ]);
  }
}
