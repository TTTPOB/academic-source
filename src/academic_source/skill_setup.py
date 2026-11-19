"""Install the agent skill that ships with this package.

Only the standard library may be imported here: the default installation runs it.
"""

from __future__ import annotations

import os
import shutil
from importlib import resources
from pathlib import Path

SKILL_NAME = "academic-source"


def default_skill_root() -> Path:
    agents_home = os.environ.get("DSH_AGENTS_HOME") or "~/.agents"
    return Path(agents_home).expanduser() / "skills"


def install_skill(skill_root: Path | None = None) -> Path:
    """Replace `<skill_root>/academic-source` with the bundled copy."""
    target = (skill_root or default_skill_root()).expanduser() / SKILL_NAME
    bundled = resources.files("academic_source") / "skills" / SKILL_NAME
    with resources.as_file(bundled) as source:
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    return target
