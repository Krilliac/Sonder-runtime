"""Language baseline metadata for repository intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepositoryLanguage(StrEnum):
    CPP = "cpp"
    CSHARP = "csharp"
    PYTHON = "python"
    RUST = "rust"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    GO = "go"
    JAVA = "java"
    SQL = "sql"
    HLSL = "hlsl"
    GLSL = "glsl"


@dataclass(frozen=True)
class LanguageBaseline:
    language: RepositoryLanguage
    extensions: tuple[str, ...]
    symbol_indexing: bool = True
    lsp_candidate: bool = True


BASELINES = {
    RepositoryLanguage.CPP: LanguageBaseline(RepositoryLanguage.CPP, (".cpp", ".cc", ".h", ".hpp")),
    RepositoryLanguage.CSHARP: LanguageBaseline(RepositoryLanguage.CSHARP, (".cs",)),
    RepositoryLanguage.PYTHON: LanguageBaseline(RepositoryLanguage.PYTHON, (".py",)),
    RepositoryLanguage.RUST: LanguageBaseline(RepositoryLanguage.RUST, (".rs",)),
    RepositoryLanguage.TYPESCRIPT: LanguageBaseline(RepositoryLanguage.TYPESCRIPT, (".ts", ".tsx")),
    RepositoryLanguage.JAVASCRIPT: LanguageBaseline(RepositoryLanguage.JAVASCRIPT, (".js", ".jsx")),
    RepositoryLanguage.GO: LanguageBaseline(RepositoryLanguage.GO, (".go",)),
    RepositoryLanguage.JAVA: LanguageBaseline(RepositoryLanguage.JAVA, (".java",)),
    RepositoryLanguage.SQL: LanguageBaseline(RepositoryLanguage.SQL, (".sql",)),
    RepositoryLanguage.HLSL: LanguageBaseline(RepositoryLanguage.HLSL, (".hlsl",)),
    RepositoryLanguage.GLSL: LanguageBaseline(RepositoryLanguage.GLSL, (".glsl", ".vert", ".frag")),
}


def baseline_for(language: RepositoryLanguage | str) -> LanguageBaseline:
    try:
        return BASELINES[language if isinstance(language, RepositoryLanguage) else RepositoryLanguage(language)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"unsupported repository language: {language!r}") from exc


def detect_by_extension(extension: str) -> LanguageBaseline:
    normalized = extension if extension.startswith(".") else "." + extension
    for baseline in BASELINES.values():
        if normalized.lower() in baseline.extensions:
            return baseline
    raise ValueError(f"unsupported repository extension: {extension!r}")
