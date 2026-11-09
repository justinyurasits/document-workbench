"""The project store: one bound handle per project, immutable inputs, versioned drafts, and a guarded root."""
import stat
import subprocess

import pytest

from construction_os.projects import FileProjectStore, ProjectError, ProjectIsolationError


@pytest.fixture
def store(tmp_path):
    return FileProjectStore(tmp_path / "projects")


def notes(tmp_path, text, name="notes.txt"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_create_and_open(store):
    created = store.create("P-001", "Garage outlets")
    opened = store.open("P-001")
    assert (opened.id, opened.name) == ("P-001", "Garage outlets") and created.path() == opened.path()
    with pytest.raises(ProjectError):
        store.create("P-001")


@pytest.mark.parametrize("bad", ["../P-001", "P/1", "", ".hidden", "a" * 65, "P 1"])
def test_project_ids_are_validated(store, bad):
    with pytest.raises(ProjectError):
        store.create(bad)
    with pytest.raises(ProjectError):
        store.open(bad)


def test_inputs_are_copied_in_read_only_and_never_replaced(store, tmp_path):
    project = store.create("P-001")
    source = notes(tmp_path, "first")
    record = project.add_input("notes", source)
    source.write_text("changed afterwards")
    assert project.read_input_bytes(record) == b"first"
    assert not project.input_path(record).stat().st_mode & stat.S_IWUSR
    again = project.add_input("notes", source)
    assert (record.id, again.id) == ("notes-1", "notes-2") and project.inputs() == [record, again]


def test_drafts_are_numbered_versions(store):
    project = store.create("P-001")
    assert [project.new_draft_folder().name for _ in range(3)] == ["v001", "v002", "v003"]


def test_a_project_cannot_reach_another_projects_folder(store, tmp_path):
    a = store.create("A")
    a_record = a.add_input("notes", notes(tmp_path, "A's notes"))
    b = store.create("B")
    with pytest.raises(ProjectIsolationError):
        b.path("..", "A", "inputs", a_record.file)
    with pytest.raises(ProjectIsolationError):
        b.input_path(a_record)
    with pytest.raises(ProjectIsolationError):
        b.read_input_bytes(a_record)
    assert b.inputs() == []


def test_a_symlink_out_of_the_project_is_refused(store, tmp_path):
    a = store.create("A")
    b = store.create("B")
    (b.path() / "inputs").mkdir()
    (b.path() / "inputs" / "escape").symlink_to(a.path())
    with pytest.raises(ProjectIsolationError):
        b.path("inputs", "escape", "project.yaml")


def test_root_inside_the_package_is_refused(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(ProjectError, match="inside"):
        FileProjectStore(package / "projects", forbidden=[package])
    with pytest.raises(ProjectError):
        FileProjectStore(package, forbidden=[package])


def git_repo(path, ignore=None):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if ignore:
        (path / ".gitignore").write_text(ignore)
    return path


def test_root_inside_a_git_repository_must_be_ignored(tmp_path):
    repo = git_repo(tmp_path / "repo")
    with pytest.raises(ProjectError, match="gitignored"):
        FileProjectStore(repo / "projects")
    ignored = git_repo(tmp_path / "ignored", ignore="/projects/\n")
    assert FileProjectStore(ignored / "projects").root == (ignored / "projects").resolve()


def test_root_comes_from_projects_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "from-env"))
    assert FileProjectStore().root == (tmp_path / "from-env").resolve()
