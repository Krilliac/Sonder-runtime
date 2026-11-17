/// Sampling temperature as Settings edits it: Auto (null, the model's own
/// preset) or a value from 0 to 2 in steps of 0.05, with a slider.
library;

import 'package:flutter/material.dart';

import '../theme.dart';

const temperatureMin = 0.0;
const temperatureMax = 2.0;

/// Slider steps: 0 to 2 in 0.05 increments.
const temperatureDivisions = 40;

/// Where the slider starts when Auto is turned off.
const temperatureStart = 0.7;

/// [value] clamped to 0..2 and rounded to 0.05; null (Auto) and values that
/// are not finite become null.
double? normalizeTemperature(double? value) {
  if (value == null || !value.isFinite) return null;
  final steps = (value.clamp(temperatureMin, temperatureMax) * 20).round();
  return steps / 20;
}

/// The stored text as a temperature; missing or garbled text is Auto (null).
double? parseTemperature(String? text) {
  if (text == null) return null;
  return normalizeTemperature(double.tryParse(text.trim()));
}

/// "0.70", or "auto".
String temperatureLabel(double? value) =>
    value == null ? 'auto' : value.toStringAsFixed(2);

/// The slider half of the temperature control. With [value] null (Auto) the
/// slider is disabled and the label says "auto".
class TemperatureSlider extends StatelessWidget {
  final double? value;
  final ValueChanged<double> onChanged;

  const TemperatureSlider({
    super.key,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    final colors = SonderTokens.of(context);
    final current = value;
    return Row(children: [
      Expanded(
        child: Semantics(
          container: true,
          label: 'Temperature',
          child: Slider(
            key: const Key('settings-temperature-slider'),
            value: current ?? temperatureStart,
            min: temperatureMin,
            max: temperatureMax,
            divisions: temperatureDivisions,
            semanticFormatterCallback: temperatureLabel,
            onChanged: current == null
                ? null
                : (v) => onChanged(normalizeTemperature(v)!),
          ),
        ),
      ),
      SizedBox(
        width: 48,
        child: Text(
          temperatureLabel(current),
          key: const Key('settings-temperature-value'),
          textAlign: TextAlign.end,
          style: colors.mono(11,
              color: current == null ? colors.muted : colors.accentText,
              weight: FontWeight.w400),
        ),
      ),
    ]);
  }
}
