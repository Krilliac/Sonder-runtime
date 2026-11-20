import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../../theme.dart';
import 'motion.dart';

/// A [TextField] with a focus glow.
class SonderTextField extends StatefulWidget {
  final Key? fieldKey;
  final TextEditingController controller;
  final String? hint;
  final bool obscureText;
  final bool mono;
  final Widget? suffix;
  final Widget? prefix;
  final String? errorText;
  final TextInputType? keyboardType;
  final List<TextInputFormatter>? inputFormatters;
  final ValueChanged<String>? onChanged;
  final ValueChanged<String>? onSubmitted;
  final FocusNode? focusNode;
  final TextAlign textAlign;
  final String? suffixText;
  final bool readOnly;

  const SonderTextField({
    super.key,
    this.fieldKey,
    required this.controller,
    this.hint,
    this.obscureText = false,
    this.mono = false,
    this.suffix,
    this.prefix,
    this.errorText,
    this.keyboardType,
    this.inputFormatters,
    this.onChanged,
    this.onSubmitted,
    this.focusNode,
    this.textAlign = TextAlign.start,
    this.suffixText,
    this.readOnly = false,
  });

  @override
  State<SonderTextField> createState() => _SonderTextFieldState();
}

class _SonderTextFieldState extends State<SonderTextField> {
  late FocusNode _focusNode;
  bool _focused = false;

  @override
  void initState() {
    super.initState();
    _focusNode = widget.focusNode ?? FocusNode();
    _focusNode.addListener(_handleFocusChange);
  }

  @override
  void dispose() {
    if (widget.focusNode == null) {
      _focusNode.dispose();
    } else {
      _focusNode.removeListener(_handleFocusChange);
    }
    super.dispose();
  }

  void _handleFocusChange() {
    setState(() {
      _focused = _focusNode.hasFocus;
    });
  }

  @override
  Widget build(BuildContext context) {
    final tokens = SonderTokens.of(context);
    final text = Theme.of(context).textTheme;
    final style = widget.mono
        ? tokens.mono(13.5, color: tokens.text, height: 22)
        : text.bodyMedium;

    return AnimatedContainer(
      duration: SonderMotion.of(context, SonderMotion.fast),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(SonderRadius.row),
        boxShadow: _focused
            ? [
                BoxShadow(
                  color: tokens.accentDim,
                  blurRadius: 4,
                  spreadRadius: 2,
                )
              ]
            : [],
      ),
      child: TextField(
        key: widget.fieldKey,
        controller: widget.controller,
        focusNode: _focusNode,
        obscureText: widget.obscureText,
        readOnly: widget.readOnly,
        autocorrect: false,
        enableSuggestions: !widget.obscureText,
        keyboardType: widget.keyboardType,
        inputFormatters: widget.inputFormatters,
        onChanged: widget.onChanged,
        onSubmitted: widget.onSubmitted,
        textAlign: widget.textAlign,
        style: style,
        decoration: InputDecoration(
          hintText: widget.hint,
          hintStyle: (widget.mono ? tokens.mono(13.5, height: 22) : text.bodyMedium)
              ?.copyWith(color: tokens.muted),
          prefixIcon: widget.prefix,
          suffixIcon: widget.suffix,
          suffixText: widget.suffixText,
          suffixStyle: text.bodySmall?.copyWith(color: tokens.muted),
          errorText: widget.errorText,
          errorMaxLines: 3,
          focusedBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
            borderSide: BorderSide(color: tokens.accentText, width: 1.5),
          ),
          enabledBorder: OutlineInputBorder(
            borderRadius: BorderRadius.circular(SonderRadius.row),
            borderSide: BorderSide(color: tokens.hairline, width: 1),
          ),
        ),
      ),
    );
  }
}
