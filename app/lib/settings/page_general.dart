part of '../settings_screen.dart';

/// General: what a new conversation asks for.
extension _GeneralPage on _SettingsScreenState {
  Widget _generalPage(BuildContext context) {
    final contextError = contextSizeError(_contextSize.text);
    final note = _contextNote;
    return SettingsSection(children: [
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
    ]);
  }
}
