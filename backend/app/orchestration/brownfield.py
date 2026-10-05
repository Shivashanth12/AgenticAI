"""Additive, bounded static analysis for brownfield assessment evidence."""

import ast
import os
import re
from pathlib import Path
from typing import Any

from app.core.config import Settings

ALLOWED_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".yaml", ".yml", ".toml", ".md"}
IGNORED_PARTS = {".git", ".next", ".venv", "__pycache__", "node_modules", "dist", "build"}
ROUTE_PATTERN = re.compile(r"""(?:app|router)\.(?:get|post|put|patch|delete)\(\s*["']([^"']+)""")
IMPORT_PATTERN = re.compile(r"(?:from|import)\s+([A-Za-z0-9_./-]+)")


class BrownfieldAnalyzer:
    """Produce explainable repository structure, dependency, and impact evidence."""

    def __init__(self, settings: Settings) -> None:
        self.root = Path(settings.workspace_path).resolve()
        self.max_files = settings.max_codebase_files

    def analyze(self, requirement: str) -> dict[str, Any]:
        files = self._files()
        keywords = self._keywords(requirement)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        impacts: list[dict[str, Any]] = []
        inventories = {"apis": [], "models": [], "schemas": [], "tests": [], "config": []}

        for path in files:
            relative = path.relative_to(self.root).as_posix()
            text = self._read(path)
            node = {"path": relative, "kind": self._kind(path), "symbols": []}
            score = sum(term in relative.lower() for term in keywords)
            matches = sorted(term for term in keywords if term in text.lower())[:12]
            score += len(matches)
            if path.suffix == ".py":
                self._inspect_python(text, relative, node, edges, inventories)
            elif path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
                self._inspect_javascript(text, relative, node, edges, inventories)
            if path.name in {".env", "pyproject.toml", "package.json", "compose.yaml"}:
                inventories["config"].append(relative)
            nodes.append(node)
            if score:
                impacts.append(
                    {
                        "path": relative,
                        "relevance_score": score,
                        "matched_terms": matches,
                        "symbols": node["symbols"],
                        "kind": node["kind"],
                    }
                )

        impacts.sort(key=lambda item: (-item["relevance_score"], item["path"]))
        return {
            "available": self.root.is_dir(),
            "workspace": str(self.root),
            "files_inspected": len(files),
            "nodes": nodes,
            "edges": edges,
            "impacted_files": impacts[:50],
            "inventory": {key: sorted(set(value)) for key, value in inventories.items()},
            "data_flows_to_review": [
                "HTTP request → API validation → application service → persistence",
                "short URL request → lookup/cache → redirect → analytics event",
                "workflow request → dependency graph → provider → artifact/approval",
            ],
            "limitations": [
                "Static bounded analysis; it does not execute code or build a compiler-level "
                "call graph.",
                "Dynamic imports and runtime-generated routes may not be discoverable.",
            ],
        }

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        found: list[Path] = []
        for directory, subdirectories, names in os.walk(self.root):
            subdirectories[:] = sorted(item for item in subdirectories if item not in IGNORED_PARTS)
            for name in sorted(names):
                path = Path(directory) / name
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in ALLOWED_SUFFIXES
                ):
                    found.append(path)
                    if len(found) >= self.max_files:
                        return found
        return found

    @staticmethod
    def _keywords(requirement: str) -> set[str]:
        return {
            word.lower()
            for word in re.findall(r"[a-zA-Z_][a-zA-Z0-9_/-]{2,}", requirement)
            if word.lower() not in {"the", "and", "with", "for", "from", "into"}
        }

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(errors="ignore")[:100_000]
        except OSError:
            return ""

    @staticmethod
    def _kind(path: Path) -> str:
        name = path.name.lower()
        if name.startswith("test") or "/tests/" in path.as_posix():
            return "test"
        if name in {"pyproject.toml", "package.json", "compose.yaml", ".env"}:
            return "configuration"
        return path.suffix.lstrip(".") or "file"

    def _inspect_python(self, text, relative, node, edges, inventories) -> None:
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError:
            node["parse_error"] = True
            return
        for item in ast.walk(tree):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node["symbols"].append(item.name)
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                imported = (
                    [alias.name for alias in item.names]
                    if isinstance(item, ast.Import)
                    else [item.module or ""]
                )
                edges.extend({"source": relative, "target": name} for name in imported if name)
            if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute):
                if item.func.attr in {"get", "post", "put", "patch", "delete"} and item.args:
                    if isinstance(item.args[0], ast.Constant) and isinstance(
                        item.args[0].value, str
                    ):
                        inventories["apis"].append(
                            f"{relative}:{item.func.attr.upper()} {item.args[0].value}"
                        )
                if item.func.attr in {"mapped_column", "relationship"}:
                    inventories["models"].append(relative)
            if isinstance(item, ast.ClassDef):
                bases = {ast.unparse(base) for base in item.bases}
                if any("BaseModel" in base for base in bases):
                    inventories["schemas"].append(f"{relative}:{item.name}")
        if node["kind"] == "test":
            inventories["tests"].append(relative)

    def _inspect_javascript(self, text, relative, node, edges, inventories) -> None:
        node["symbols"] = sorted(set(ROUTE_PATTERN.findall(text)))
        inventories["apis"].extend(f"{relative}:{route}" for route in node["symbols"])
        edges.extend(
            {"source": relative, "target": imported}
            for imported in IMPORT_PATTERN.findall(text)
        )
        if node["kind"] == "test":
            inventories["tests"].append(relative)
