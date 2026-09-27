"""Workspace resolution and path containment for developer actions."""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    """Raised when a workspace or project path is unsafe or invalid."""


def resolve_workspace(path: str | Path | None, default: Path, *, create: bool = False) -> Path:
    """Return a resolved workspace directory, optionally creating it."""
    candidate = Path(path).expanduser() if path else default
    resolved = candidate.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise WorkspaceError(f"Workspace is not a directory: {resolved}")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    elif not resolved.exists():
        raise WorkspaceError(f"Workspace does not exist: {resolved}")
    return resolved


def safe_path(workspace: Path, relative_path: str) -> Path:
    """Resolve a project-relative path and reject traversal outside workspace."""
    if not relative_path or Path(relative_path).is_absolute():
        raise WorkspaceError("A non-empty relative path is required")
    root = workspace.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"Path escapes workspace: {relative_path}") from exc
    return target