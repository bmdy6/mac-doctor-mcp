"""Which paths a cleanup plan may touch.

Two kinds of allowed roots:
- ``allow_roots``: anything strictly below the root may be planned (caches, logs, Downloads).
- ``artifact_roots``: only directories whose name is a known build artifact
  (node_modules, .next, DerivedData ...) may be planned (project folders).

Deny rules always win, both for the item path and for anything found inside a directory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

ARTIFACT_NAMES = frozenset({
    "node_modules", ".next", ".nuxt", ".turbo", ".parcel-cache", ".vite",
    "dist", "build", "target", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".gradle", "DerivedData", "Pods",
})

# Any path component with one of these names is never touched.
DENY_NAMES = frozenset({
    ".git", ".ssh", ".gnupg", "Keychains", "ai-vault", "knowledge", "notes", "vault", ".hermes",
    ".password-store", "1Password",
})

# Any path containing one of these component sequences is never touched
# (agent memory, skills and config).
DENY_SEQUENCES = (
    (".claude", "projects"), (".claude", "skills"), (".agents", "skills"),
    (".codex",), (".config", "ai-vault"), ("Library", "Keychains"),
    ("Library", "Mobile Documents"),
)


class PolicyError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Policy:
    allow_roots: list[Path] = field(default_factory=list)
    artifact_roots: list[Path] = field(default_factory=list)
    artifact_names: frozenset[str] = ARTIFACT_NAMES

    @classmethod
    def default(cls, home: Path | None = None) -> "Policy":
        home = home or Path.home()
        allow = [home / "Library/Caches", home / "Library/Logs", home / "Downloads",
                 home / "Library/Developer/Xcode/DerivedData", home / ".cache", home / ".npm/_cacache"]
        artifact = [home / "项目", home / "Projects", home / "code", home / "src"]
        return cls(allow_roots=allow, artifact_roots=artifact)

    @classmethod
    def load(cls, config_file: Path | None) -> "Policy":
        if config_file is None or not config_file.exists():
            return cls.default()
        data = json.loads(config_file.read_text("utf-8"))
        default = cls.default()
        return cls(
            allow_roots=[Path(p).expanduser() for p in data.get("allow_roots", default.allow_roots)],
            artifact_roots=[Path(p).expanduser() for p in data.get("artifact_roots", default.artifact_roots)],
        )

    def check(self, raw: str) -> Path:
        """Return the normalized path if a plan may include it, else raise PolicyError."""
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise PolicyError("E_BAD_PATH", repr(raw))
        if not os.path.isabs(raw):
            raise PolicyError("E_ABSOLUTE_PATH_REQUIRED", raw)
        if os.path.normpath(raw) != raw:
            raise PolicyError("E_NORMALIZED_PATH_REQUIRED", raw)
        path = Path(raw)
        # No symlink anywhere in the path, including the item itself.
        if os.path.realpath(raw) != raw:
            raise PolicyError("E_SYMLINK_IN_PATH", raw)
        denied(path.parts)
        if any(_strictly_under(path, root) for root in self.allow_roots):
            return path
        if any(_strictly_under(path, root) for root in self.artifact_roots):
            if path.name in self.artifact_names:
                return path
            raise PolicyError("E_NOT_ARTIFACT", raw)
        raise PolicyError("E_OUTSIDE_ALLOWED_ROOTS", raw)


def denied(parts: tuple[str, ...]) -> None:
    for name in parts:
        if name in DENY_NAMES or name.casefold().startswith("knowledge-"):
            raise PolicyError("E_DENIED_PATH", "/".join(parts))
    for seq in DENY_SEQUENCES:
        n = len(seq)
        for i in range(len(parts) - n + 1):
            if tuple(parts[i:i + n]) == seq:
                raise PolicyError("E_DENIED_PATH", "/".join(parts))


def _strictly_under(path: Path, root: Path) -> bool:
    root_s = os.path.realpath(str(root))
    return str(path) != root_s and str(path).startswith(root_s.rstrip("/") + "/")
