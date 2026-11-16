"""The profiles of a new repository: every shipped one renders, a new folder is a new profile, a broken one says what."""
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import repoprofiles  # noqa: E402

SEED = pathlib.Path(__file__).resolve().parents[2]
VALUES = {"name": "widgets", "description": "a small service for the lab", "owner": "owner/repository",
          "default_branch": "main", "validate_command": "python -m pytest -q"}
TEMPLATE = "# {{name}}\n\n{{validate_command}} on {{default_branch}}, maintained by {{owner}}.\n"


def without_expressions(text: str) -> str:
    """The text without the expressions of a workflow (${{ ... }}), which are not placeholders."""
    return text.replace("${{", "")


def sample_profile(folder: pathlib.Path, files: list[dict] | None = None, **meta) -> repoprofiles.Profile:
    """A profile in a temporary folder, with templates that use all five placeholders."""
    files = files if files is not None else [
        {"source": "agents.md.tmpl", "target": "AGENTS.md"},
        {"source": "readme.md.tmpl", "target": "README.md"},
        {"source": "contributing.md.tmpl", "target": "CONTRIBUTING.md"}]
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"name": folder.name, "description": "a sample project", "validate_command": "python -m pytest -q",
           "files": files, **meta}
    (folder / "profile.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    for f in files:
        if not (folder / f["source"]).exists():
            (folder / f["source"]).write_text(TEMPLATE)
    return repoprofiles.load(folder)


def run(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "HOMELAB_ROOT"}
    return subprocess.run([sys.executable, "-B", "-I", str(SEED / "homelab.py"), *args], cwd=str(SEED), env=env,
                          capture_output=True, text=True, timeout=120)


def test_the_three_profiles_are_python_node_and_generic():
    found = repoprofiles.catalog()
    assert list(found) == ["generic", "node", "python"]
    assert [p.name for p in found.values()] == ["generic", "node", "python"]


@pytest.mark.parametrize("name", ["generic", "node", "python"])
def test_a_shipped_profile_renders_every_file_without_a_leftover_placeholder(name):
    profile = repoprofiles.catalog()[name]
    files = repoprofiles.render(profile, VALUES)
    assert set(files) == {f.target for f in profile.files}
    assert {"AGENTS.md", "README.md", "CONTRIBUTING.md", ".gitignore"} <= set(files)
    for target, text in files.items():
        assert "{{" not in without_expressions(text), f"{target} of {name} keeps a placeholder"
        assert "{{{{" not in text
    assert "${{" not in files["AGENTS.md"]          # a file for people, not a workflow


@pytest.mark.parametrize("name", ["generic", "node", "python"])
def test_agents_md_names_the_validation_command_and_the_pull_request_rules(name):
    agents = repoprofiles.render(repoprofiles.catalog()[name], VALUES)["AGENTS.md"]
    assert VALUES["validate_command"] in agents
    assert "ai/issue-<n>" in agents
    assert "WIP: " in agents
    assert f"`{VALUES['default_branch']}` is the default branch. Never push to it" in agents
    assert "Conventional Commits" in agents


def test_the_python_profile_brings_pytest_the_gitignore_of_python_and_a_pinned_ci_job():
    files = repoprofiles.render(repoprofiles.catalog()["python"], VALUES)
    assert "__pycache__/" in files[".gitignore"]
    job = files[".forgejo/workflows/ci.yaml"]
    assert "image: python:3.11-slim-bookworm@sha256:" in job
    assert "${{ github.token }}" in job and "git fetch" in job and "actions/checkout" not in job
    assert "pip install" in job
    assert "{{" not in without_expressions(job)


def test_the_node_profile_brings_npm_ci_and_checks_that_need_a_script():
    profile = repoprofiles.catalog()["node"]
    files = repoprofiles.render(profile, VALUES | {"validate_command": profile.validate_command})
    assert "node_modules/" in files[".gitignore"]
    job = files[".forgejo/workflows/ci.yaml"]
    assert "image: node:26-bookworm-slim@sha256:" in job
    assert "${{ github.token }}" in job and "git fetch" in job and "actions/checkout" not in job
    assert "npm ci" in job
    assert "npm run test --if-present" in job


def test_the_generic_profile_brings_no_ci():
    profile = repoprofiles.catalog()["generic"]
    files = repoprofiles.render(profile, VALUES | {"validate_command": profile.validate_command})
    assert [t for t in files if t.startswith(".forgejo/")] == []
    assert "git diff --check" in files["AGENTS.md"]


@pytest.mark.parametrize("name", ["node", "python"])
def test_a_rendered_ci_file_is_a_workflow_that_fetches_the_sources(name, tmp_path):
    profile = repoprofiles.catalog()[name]
    job = yaml.safe_load(repoprofiles.render(profile, VALUES | {"validate_command": profile.validate_command})
                         [".forgejo/workflows/ci.yaml"])
    [test_job] = job["jobs"].values()
    image = test_job["container"]["image"]
    assert image.startswith(("python:3.11-slim-bookworm@sha256:", "node:26-bookworm-slim@sha256:")), image
    runs = [step["run"] for step in test_job["steps"] if "run" in step]
    assert any("http.extraheader" in run and "fetch -q --no-tags" in run for run in runs), runs
    for run in runs:
        assert subprocess.run(["bash", "-n", "-"], input=run, text=True, capture_output=True).returncode == 0, run


def test_the_json_the_app_reads_lists_every_profile_and_the_files_it_writes():
    got = run("repo-profiles", "--json")
    assert got.returncode == 0, got.stderr
    rows = json.loads(got.stdout)
    assert [r["name"] for r in rows] == ["generic", "node", "python"]
    assert [sorted(r) for r in rows] == [["description", "files", "name", "validate_command"]] * 3
    assert [r["files"] for r in rows] == [
        ["AGENTS.md", "README.md", "CONTRIBUTING.md", ".gitignore"],
        ["AGENTS.md", "README.md", "CONTRIBUTING.md", ".gitignore", ".forgejo/workflows/ci.yaml"],
        ["AGENTS.md", "README.md", "CONTRIBUTING.md", ".gitignore", ".forgejo/workflows/ci.yaml"]]
    assert rows[1]["validate_command"].startswith("npm run test --if-present")
    assert rows[2]["validate_command"] == "python -m pytest -q"


def test_the_listing_says_what_each_profile_brings():
    got = run("repo-profiles")
    assert got.returncode == 0, got.stderr
    lines = [line for line in got.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    assert lines[0].startswith("generic ") and "without code" in lines[0]
    assert lines[1].startswith("node ") and "npm ci" in lines[1]
    assert lines[2].startswith("python ") and "pytest" in lines[2]


def test_a_missing_value_is_refused(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    with pytest.raises(repoprofiles.ProfileError, match=r"agents.md.tmpl: needs a value for \{\{owner\}\}"):
        repoprofiles.render(profile, {k: v for k, v in VALUES.items() if k != "owner"})


def test_an_empty_value_counts_as_missing(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    with pytest.raises(repoprofiles.ProfileError, match=r"needs a value for \{\{validate_command\}\}"):
        repoprofiles.render(profile, VALUES | {"validate_command": "  "})


def test_an_unknown_placeholder_is_refused(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    (profile.folder / "agents.md.tmpl").write_text("# {{name}} in {{colour}}\n")
    with pytest.raises(repoprofiles.ProfileError, match=r"unknown placeholder \{\{colour\}\}; the profiles use"):
        repoprofiles.render(profile, VALUES)


def test_an_unclosed_placeholder_is_refused(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    (profile.folder / "agents.md.tmpl").write_text("# {{name\n")
    with pytest.raises(repoprofiles.ProfileError, match=r"leaves a \{\{ behind"):
        repoprofiles.render(profile, VALUES)


def test_a_value_that_brings_a_placeholder_is_refused(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    with pytest.raises(repoprofiles.ProfileError, match="after filling in the values"):
        repoprofiles.render(profile, VALUES | {"name": "the {{owner}} of the lab"})


def test_a_workflow_expression_is_not_a_placeholder(tmp_path):
    profile = sample_profile(tmp_path / "sample")
    (profile.folder / "agents.md.tmpl").write_text("run: ${{ github.token }}\n")
    assert repoprofiles.render(profile, VALUES)["AGENTS.md"] == "run: ${{ github.token }}\n"


def test_a_folder_without_a_profile_yaml_is_refused(tmp_path):
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "agents.md.tmpl").write_text("# {{name}}\n")
    with pytest.raises(repoprofiles.ProfileError, match=r"is not a profile: it has no profile.yaml"):
        repoprofiles.load(folder)
    with pytest.raises(repoprofiles.ProfileError, match="has no profile.yaml"):
        repoprofiles.catalog(tmp_path)


def test_a_profile_yaml_without_a_field_is_refused(tmp_path):
    folder = tmp_path / "half"
    folder.mkdir()
    (folder / "profile.yaml").write_text("name: half\ndescription: a sample\n")
    with pytest.raises(repoprofiles.ProfileError, match="validate_command is missing or empty"):
        repoprofiles.load(folder)


def test_a_source_outside_the_folder_and_a_target_above_it_are_refused(tmp_path):
    folder = tmp_path / "sample"
    folder.mkdir()
    meta = {"name": "sample", "description": "a sample", "validate_command": "true"}
    (folder / "profile.yaml").write_text(yaml.safe_dump(
        meta | {"files": [{"source": "missing.md", "target": "README.md"}]}, sort_keys=False))
    with pytest.raises(repoprofiles.ProfileError, match="source missing.md is not in"):
        repoprofiles.load(folder)
    (folder / "agents.md.tmpl").write_text(TEMPLATE)
    (folder / "profile.yaml").write_text(yaml.safe_dump(
        meta | {"files": [{"source": "agents.md.tmpl", "target": "../README.md"}]}, sort_keys=False))
    with pytest.raises(repoprofiles.ProfileError, match="must be a path inside the new repository"):
        repoprofiles.load(folder)


def test_two_sources_writing_one_target_are_refused(tmp_path):
    with pytest.raises(repoprofiles.ProfileError, match="two entries write the same target"):
        sample_profile(tmp_path / "sample", [{"source": "agents.md.tmpl", "target": "AGENTS.md"},
                                             {"source": "readme.md.tmpl", "target": "AGENTS.md"}])


def test_a_folder_added_in_a_temp_directory_is_a_profile_without_a_code_change(tmp_path):
    profile = sample_profile(tmp_path / "tiny", [
        {"source": "agents.md.tmpl", "target": "AGENTS.md"},
        {"source": "ci.yaml.tmpl", "target": ".forgejo/workflows/ci.yaml"}])
    (profile.folder / "ci.yaml.tmpl").write_text("on: [{{default_branch}}]\ntoken: ${{ github.token }}\n")
    found = repoprofiles.catalog(tmp_path)
    assert list(found) == ["tiny"]
    files = repoprofiles.render(found["tiny"], VALUES)
    assert files["AGENTS.md"] == "# widgets\n\npython -m pytest -q on main, maintained by owner/repository.\n"
    assert files[".forgejo/workflows/ci.yaml"] == "on: [main]\ntoken: ${{ github.token }}\n"
