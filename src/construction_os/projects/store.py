"""Project context: what was supplied for a project and what was generated for it (SYS-00 1.3, principle 10).

Everything loaded for a project is read through that project's handle, by project ID. A handle is bound to
one project folder and cannot reach another project, the corpus or the package. The file store is the first
implementation; a database can replace it behind the same interface without changing callers.

Layout under the root (PROJECTS_DIR, default ~/construction-os/projects):

    <project id>/project.yaml       the project ID and name
    <project id>/inputs/            notes and quotes as supplied, never changed; inputs/index.json lists them
    <project id>/drafts/v001/ …     each draft version: draft.json, pco.docx, checklist.docx, manifest.json
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import yaml

DEFAULT_ROOT = Path.home() / "construction-os" / "projects"
PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
INPUT_KINDS = ("notes", "quote")


class ProjectError(Exception):
    """A project can't be created, opened or written as asked."""


class ProjectIsolationError(ProjectError):
    """A read or write would leave the project's own folder."""


@dataclass(frozen=True)
class InputRecord:
    id: str             # notes-1, quote-1, quote-2 …: how the draft and its evidence refer to the input
    kind: str           # notes | quote
    file: str           # the stored file's name, inside inputs/
    original_name: str
    sha256: str
    added_at: str


class Project:
    """One project's context. The only way to read or write anything for that project."""

    def __init__(self, root, project_id, name):
        self.id = project_id
        self.name = name
        self._root = Path(root).resolve()

    def __repr__(self):
        return f"Project({self.id!r})"

    def path(self, *parts):
        """A path inside this project's folder. Anything that resolves outside it is refused."""
        target = self._root.joinpath(*parts).resolve()
        if target != self._root and self._root not in target.parents:
            raise ProjectIsolationError(f"{Path(*parts)} is outside project {self.id}")
        return target

    # Inputs: copied in once, never changed

    def inputs(self):
        index = self.path("inputs", "index.json")
        return [InputRecord(**item) for item in json.loads(index.read_text())] if index.exists() else []

    def add_input(self, kind, source):
        """Copy a supplied file into the project. The stored copy is read-only and never replaced."""
        if kind not in INPUT_KINDS:
            raise ProjectError(f"unknown input kind {kind!r}; expected one of {INPUT_KINDS}")
        source = Path(source)
        if not source.is_file():
            raise ProjectError(f"{source} is not a file")
        records = self.inputs()
        number = 1 + sum(1 for r in records if r.kind == kind)
        input_id = f"{kind}-{number}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name).strip("._") or "input"
        stored = self.path("inputs", f"{input_id}_{safe}")
        stored.parent.mkdir(parents=True, exist_ok=True)
        if stored.exists():
            raise ProjectError(f"{stored.name} already exists; inputs are never replaced")
        shutil.copyfile(source, stored)
        stored.chmod(stat.S_IRUSR | stat.S_IRGRP)
        record = InputRecord(input_id, kind, stored.name, source.name, sha256(stored),
                             datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.path("inputs", "index.json").write_text(json.dumps([asdict(r) for r in records + [record]], indent=2))
        return record

    def input_path(self, record):
        if record not in self.inputs():
            raise ProjectIsolationError(f"{record.id} is not an input of project {self.id}")
        return self.path("inputs", record.file)

    def read_input_bytes(self, record):
        return self.input_path(record).read_bytes()

    # Drafts: one folder per version

    def drafts(self):
        folder = self.path("drafts")
        return sorted(p.name for p in folder.iterdir() if re.fullmatch(r"v\d{3}", p.name)) if folder.exists() else []

    def new_draft_folder(self):
        existing = self.drafts()
        version = f"v{int(existing[-1][1:]) + 1 if existing else 1:03d}"
        folder = self.path("drafts", version)
        folder.mkdir(parents=True)
        return folder


class ProjectStore(Protocol):
    def create(self, project_id: str, name: str | None = None) -> Project: ...
    def open(self, project_id: str) -> Project: ...
    def exists(self, project_id: str) -> bool: ...


class FileProjectStore:
    """Project folders under one root. Refuses a root inside the corpus or package, or an unignored Git tree."""

    def __init__(self, root=None, forbidden=()):
        root = Path(root or os.environ.get("PROJECTS_DIR") or DEFAULT_ROOT).expanduser().resolve()
        check_root(root, [Path(p).resolve() for p in forbidden])
        self.root = root

    def _folder(self, project_id):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise ProjectError(f"{project_id!r} isn't a valid project ID (letters, digits, '.', '_', '-'; up to 64)")
        return self.root / project_id

    def exists(self, project_id):
        return (self._folder(project_id) / "project.yaml").is_file()

    def create(self, project_id, name=None):
        folder = self._folder(project_id)
        if self.exists(project_id):
            raise ProjectError(f"project {project_id} already exists")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "project.yaml").write_text(yaml.safe_dump({"id": project_id, "name": name}, sort_keys=False))
        return Project(folder, project_id, name)

    def open(self, project_id):
        folder = self._folder(project_id)
        if not self.exists(project_id):
            raise ProjectError(f"project {project_id} doesn't exist under {self.root}")
        data = yaml.safe_load((folder / "project.yaml").read_text()) or {}
        if data.get("id") != project_id:
            raise ProjectError(f"{folder / 'project.yaml'} says id {data.get('id')!r}, not {project_id!r}")
        return Project(folder, project_id, data.get("name"))


def check_root(root, forbidden):
    for path in forbidden:
        if root == path or path in root.parents:
            raise ProjectError(f"PROJECTS_DIR {root} is inside {path}; project context never goes into a package")
    existing = next(p for p in [root, *root.parents] if p.exists())
    top = subprocess.run(["git", "-C", str(existing), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return
    # A path inside the root: a directory rule like "/projects/" matches it even before the root exists.
    probe = str(root / "project.yaml")
    ignored = subprocess.run(["git", "-C", top.stdout.strip(), "check-ignore", "-q", probe],
                             capture_output=True)
    if ignored.returncode != 0:
        raise ProjectError(f"PROJECTS_DIR {root} is inside the Git repository {top.stdout.strip()} and isn't "
                           f"gitignored; project context must never be committed")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
