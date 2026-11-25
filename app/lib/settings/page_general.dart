part of '../settings_screen.dart';

/// General: what a new conversation asks for.
extension _GeneralPage on _SettingsScreenState {
  Widget _generalPage(BuildContext context) {
    final contextError = contextSizeError(_contextSize.text);
    final note = _contextNote;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SettingsSection(children: [
      SettingRow(
        label: 'Default model or route',
        description:
            "Used for new conversations. Chat's model menu changes it too.",
        modified: _modelChanged,
        trailing: ModelPickerButton(value: _modelValue, onPressed: _pickModel),
      ),
      SettingRow(
        label: 'Context size',
        description: 'Requested window per conversation; the server may cap '
            'it.',
        modified: _contextChanged,
        trailing: SizedBox(
          width: 176,
          child: LabeledTextField(
            fieldKey: const Key('settings-context-size'),
            label: 'Context size in tokens',
            controller: _contextSize,
            focusNode: _contextFocus,
            mono: true,
            hint: contextSizeDefault,
            textAlign: TextAlign.end,
            suffixText: ' tokens',
            keyboardType: TextInputType.number,
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp(r'[0-9kKmM.]')),
              LengthLimitingTextInputFormatter(11),
            ],
            onChanged: (_) => _contextEditedByHand(),
            onSubmitted: (_) => _normalizeContextSize(),
          ),
        ),
        below: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            ContextSizeSlider(
              tokens: _contextTokens,
              onChanged: _setContextPreset,
            ),
            if (contextError != null || note != null) ...[
              const SizedBox(height: SonderSpace.md),
              FieldNote(
                contextError != null ? StatusKind.warn : StatusKind.note,
                contextError ?? note!,
              ),
            ],
          ],
        ),
      ),
            SettingRow(
              label: 'Temperature',
              description: "Auto sends none, so the model's own sampling preset "
                  'applies. Lower is more focused, higher more varied.',
              modified: _temperatureChanged,
              trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                Text('Auto', style: Theme.of(context).textTheme.bodySmall),
                const SizedBox(width: SonderSpace.sm),
                Semantics(
                  label: 'Automatic temperature',
                  child: Switch(
                    key: const Key('settings-temperature-auto'),
                    value: _temperature == null,
                    onChanged: (auto) =>
                        _setTemperature(auto ? null : temperatureStart),
                  ),
                ),
              ]),
              below: TemperatureSlider(
                value: _temperature,
                onChanged: _setTemperature,
              ),
            ),
            SettingRow(
              label: 'Reasoning effort',
              description:
                  'Controls thinking depth per session (also via /effort or the '
                  'chat bar). Off is fastest for tool loops; Auto follows the '
                  'server default.',
              modified: _effortChanged,
              below: EffortSelector(
                value: _reasoningEffort,
                onChanged: _setReasoningEffort,
              ),
            ),
            SettingRow(
              label: 'Persona',
              description:
                  'Shapes response voice and focus (also via /persona or the '
                  'chat bar). Auto routes by tier and prompt; Off uses no '
                  'persona prompt.',
              modified: _personaChanged,
              below: PersonaSelector(
                value: _persona,
                onChanged: _setPersona,
              ),
            ),
            SettingRow(
              label: 'Execution mode',
              description:
                  'Controls how turns run (also via /exec_mode or the composer '
                  '+ menu). Auto lets the difficulty router pick Single, '
                  'Ensemble, or Fanout.',
              modified: _executionModeChanged,
              below: ExecutionModeSelector(
                value: _executionMode,
                onChanged: _setExecutionMode,
              ),
            ),
          ]),
        _clientExecSection(context),
      ],
    );
  }

  Widget _clientExecSection(BuildContext context) {
    final companion = _companionHealth;
    final launchCmd = clientCompanionLaunchCommand(
      workspace: _clientWorkspaceRoot.text.trim(),
      allowOrigin: Settings.pageOrigin ?? '',
    );
    return SettingsSection(
      key: const Key('settings-client-exec-section'),
      title: 'Client PC execution',
      description: 'Run delegated file, git and code tools on your own PC '
          'instead of the remote server.',
      children: [
        SwitchRow(
          switchKey: const Key('settings-client-exec-enabled'),
          label: 'Run tools on my PC',
          description: 'Advertises X-Sonder-Client-Exec: 1 on chat turns.',
          value: _clientExecEnabled,
          modified: _clientExecEnabled != _saved.clientExecEnabled,
          onChanged: (v) => _stage(() => _clientExecEnabled = v),
        ),
        SettingRow(
          label: 'Client PC permission mode',
          description: modeBlurbs[_clientPermissionMode] ??
              'Controls when tools on your PC ask before running.',
          modified: _clientPermissionMode != _saved.clientPermissionMode,
          trailing: OutlinedButton(
            key: const Key('settings-client-mode'),
            onPressed: _pickClientPermissionMode,
            child: Text(_clientPermissionMode),
          ),
          below: _clientPermissionMode == PermissionMode.bypass
              ? const FieldNote(
                  StatusKind.warn,
                  'Client bypass mode is active: tools on your PC execute '
                  'without prompts or workspace root containment.',
                )
              : null,
        ),
        SettingsFieldRow(
          label: 'Client workspace root (optional)',
          description: LocalManager.canRunLocalTools
              ? 'Restricts file and command paths unless client bypass mode is on.'
              : 'Shown in the companion launch command.',
          modified: _textChanged(_clientWorkspaceRoot, _saved.clientWorkspaceRoot),
          field: LabeledTextField(
            fieldKey: const Key('settings-client-workspace'),
            label: 'Client workspace root',
            controller: _clientWorkspaceRoot,
            hint: '.',
            mono: true,
          ),
        ),
        if (!LocalManager.canRunLocalTools) ...[
          SettingsFieldRow(
            label: 'Client companion URL',
            description: 'Loopback address of the local Python companion.',
            modified: _textChanged(_clientAgentUrl, _saved.clientAgentUrl),
            field: LabeledTextField(
              fieldKey: const Key('settings-client-agent-url'),
              label: 'Client companion URL',
              controller: _clientAgentUrl,
              hint: Settings.defaultClientAgentUrl,
              mono: true,
              keyboardType: TextInputType.url,
            ),
          ),
          SettingsFieldRow(
            label: 'Client companion token',
            description: 'Printed by the companion when it starts.',
            modified: _textChanged(_clientAgentToken, _saved.clientAgentToken),
            field: LabeledTextField(
              fieldKey: const Key('settings-client-agent-token'),
              label: 'Client companion token',
              controller: _clientAgentToken,
              obscureText: _obscureClientAgentToken,
              mono: true,
              suffix: VisibilityToggle(
                obscured: _obscureClientAgentToken,
                what: 'companion token',
                onPressed: () => _update(
                    () => _obscureClientAgentToken = !_obscureClientAgentToken),
              ),
            ),
          ),
          _ActionRow(
            buttons: [
              AsyncActionButton(
                buttonKey: const Key('settings-check-companion'),
                label: 'Check companion',
                busyLabel: 'Checking…',
                doneLabel: null,
                busy: _checkingCompanion,
                icon: Icons.computer_outlined,
                onPressed: _checkCompanion,
              ),
              OutlinedButton.icon(
                key: const Key('settings-copy-companion-cmd'),
                icon: const Icon(Icons.copy_outlined, size: 16),
                label: const Text('Copy launch command'),
                onPressed: () => _copyServerSetting(launchCmd),
              ),
            ],
            outcome: companion == null
                ? null
                : ActionOutcome(
                    companion.reachable && companion.authenticated
                        ? StatusKind.ok
                        : StatusKind.warn,
                    companion.message,
                    detail: (!companion.reachable || !companion.authenticated)
                        ? 'Run on your PC: $launchCmd'
                        : (companion.workspace.isEmpty
                            ? null
                            : 'Workspace: ${companion.workspace}'),
                  ),
            onDismiss: () => _update(() => _companionHealth = null),
          ),
        ],
      ],
    );
  }
}
