import os
from pathlib import Path
from typing import Any

from app.core.config import Settings

ALLOWED_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
}
IGNORED_PARTS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}


class CodebaseAnalyzer:
    """Bounded, read-only repository inspection for brownfield reasoning."""

    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.workspace_path).resolve()
        self.max_files = settings.max_codebase_files

    def analyze(self, requirement: str) -> dict[str, Any]:
        if not self.root.is_dir():
            return {
                "available": False,
                "workspace": str(self.root),
                "reason": "Read-only workspace is not mounted.",
                "impacted_files": [],
            }
        keywords = {
            word.lower()
            for word in requirement.replace("/", " ").replace("-", " ").split()
            if len(word) >= 4
        }
        files: list[Path] = []
        for directory, subdirectories, names in os.walk(self.root):
            subdirectories[:] = sorted(d for d in subdirectories if d not in IGNORED_PARTS)
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if path.suffix.lower() in ALLOWED_SUFFIXES:
                    files.append(path)
                if len(files) >= self.max_files:
                    break
            if len(files) >= self.max_files:
                break

        impacted: list[dict[str, Any]] = []
        modules: dict[str, int] = {}
        for path in files:
            relative = path.relative_to(self.root).as_posix()
            top_level = relative.split("/", 1)[0]
            modules[top_level] = modules.get(top_level, 0) + 1
            score = sum(keyword in relative.lower() for keyword in keywords)
            matches: list[str] = []
            try:
                content = path.read_text(errors="ignore")[:50_000].lower()
                matches = sorted(keyword for keyword in keywords if keyword in content)[:8]
                score += len(matches)
            except OSError:
                continue
            if score:
                impacted.append(
                    {
                        "path": relative,
                        "relevance_score": score,
                        "matched_terms": matches,
                    }
                )

        impacted.sort(key=lambda item: (-item["relevance_score"], item["path"]))
        return {
            "available": True,
            "workspace": str(self.root),
            "files_inspected": len(files),
            "modules": modules,
            "impacted_files": impacted[:25],
            "data_flows_to_review": [
                "HTTP request → API validation → application service → persistence",
                "short URL request → lookup/cache → redirect → analytics event",
                "workflow request → dependency graph → provider → artifact/approval",
            ],
        }
