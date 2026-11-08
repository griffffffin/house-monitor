"""scripts/check_pinned_deps.py: the requirements.txt pins vs. what is installed.

The pins follow the Pi's apt packages (production runs the system Python, no
venv), and this check is what notices when the two drift apart, e.g. after an
apt upgrade. A check that quietly passes is worse than none, so most of these
tests are about it failing when it should.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import check_pinned_deps as cpd

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_pinned_deps.py"

CURRENT_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
PYTEST_VERSION = pytest.__version__
DEV_TOOLS = {"pytest", "black", "ruff", "mypy", "types-beautifulsoup4"}
# The automatically used packages this environment has (the dev venv and CI
# pin aiodns): a requirements file of our own has to pin them too.
AUTO_USED_HERE = "".join(
    f"{dist}=={cpd.installed_version(dist)}\n"
    for module, dist in cpd.AUTO_USED
    if cpd.importable(module) and cpd.installed_version(dist)
)


def lookup_from(installed):
    return lambda name: installed.get(cpd.normalize(name))


def read_pyproject():
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def repo_pins():
    return cpd.parse_pins(cpd.DEFAULT_REQUIREMENTS.read_text(encoding="utf-8"))


class TestParsePins:
    def test_pins_comments_and_blank_lines(self):
        text = "# header\n\naiohttp==3.13.3\nlxml==6.0.2  # inline comment\n"
        assert cpd.parse_pins(text) == {"aiohttp": "3.13.3", "lxml": "6.0.2"}

    def test_keeps_file_order(self):
        assert list(cpd.parse_pins("b==1\na==2\n")) == ["b", "a"]

    @pytest.mark.parametrize(
        "line",
        [
            "aiohttp>=3.13.3",
            "aiohttp~=3.13",
            "aiohttp",
            "aiohttp==3.13.3; python_version < '3.15'",
            "aiohttp[speedups]==3.13.3",
            "-r requirements-dev.txt",
            "aiohttp==3.13.3,<4",
            "aiohttp===3.13.3",
        ],
    )
    def test_refuses_anything_but_an_exact_pin(self, line):
        with pytest.raises(ValueError, match="line 2"):
            cpd.parse_pins(f"yarl==1.22.0\n{line}\n")

    def test_refuses_a_duplicate_even_with_another_spelling(self):
        with pytest.raises(ValueError, match="already pinned on line 1"):
            cpd.parse_pins("typing_extensions==4.15.0\nTyping-Extensions==4.15.0\n")


class TestPythonVersion:
    def test_first_non_comment_line(self):
        assert cpd.parse_python_version("# CI and the Pi\n\n3.14\n") == "3.14"

    @pytest.mark.parametrize("text", ["", "# only a comment\n", "python3.14\n", "3\n", "3.x\n"])
    def test_refuses_a_missing_or_malformed_version(self, text):
        with pytest.raises(ValueError):
            cpd.parse_python_version(text)

    def test_minor_version_matches_any_micro(self):
        assert cpd.python_matches("3.14", (3, 14, 4, "final", 0))
        assert cpd.python_matches("3.14", (3, 14, 0, "final", 0))

    def test_other_minor_or_major_does_not_match(self):
        assert not cpd.python_matches("3.14", (3, 13, 9, "final", 0))
        assert not cpd.python_matches("3.14", (3, 15, 0, "final", 0))
        assert not cpd.python_matches("3.14", (4, 14, 0, "final", 0))

    def test_micro_version_must_match_exactly(self):
        assert cpd.python_matches("3.14.4", (3, 14, 4, "final", 0))
        assert not cpd.python_matches("3.14.4", (3, 14, 5, "final", 0))


class TestCheckPins:
    def test_all_match(self):
        rows = cpd.check_pins({"aiohttp": "3.13.3"}, lookup_from({"aiohttp": "3.13.3"}))
        assert [row.ok for row in rows] == [True]

    def test_a_different_version_is_drift(self):
        rows = cpd.check_pins({"aiohttp": "3.13.3"}, lookup_from({"aiohttp": "3.13.4"}))
        assert rows == [cpd.Row("aiohttp", "3.13.3", "3.13.4")]
        assert not rows[0].ok

    def test_a_missing_package_is_drift(self):
        rows = cpd.check_pins({"aiodns": "4.0.0"}, lookup_from({}))
        assert rows[0].installed is None
        assert not rows[0].ok

    def test_compares_whole_versions_not_prefixes(self):
        rows = cpd.check_pins({"yarl": "1.2"}, lookup_from({"yarl": "1.22.0"}))
        assert not rows[0].ok

    def test_real_lookup_finds_an_installed_distribution(self):
        assert cpd.installed_version("pytest") == PYTEST_VERSION
        assert cpd.installed_version("PyTest") == PYTEST_VERSION
        assert cpd.installed_version("no-such-distribution-xyz") is None

    def test_report_shows_every_package_and_its_status(self):
        rows = [
            cpd.Row("aiohttp", "3.13.3", "3.13.3"),
            cpd.Row("yarl", "1.22.0", "1.25.1"),
            cpd.Row("aiodns", "4.0.0", None),
        ]
        report = cpd.format_report(rows, "3.14", (3, 13, 9, "final", 0), "/usr/bin/python3")
        assert "Python 3.13.9 (/usr/bin/python3), expected 3.14: MISMATCH" in report
        lines = report.splitlines()
        assert any(line.split() == ["aiohttp", "3.13.3", "3.13.3", "OK"] for line in lines)
        assert any(line.split() == ["yarl", "1.22.0", "1.25.1", "DIFFERS"] for line in lines)
        assert any(line.split() == ["aiodns", "4.0.0", "(missing)", "MISSING"] for line in lines)


class TestAutoUsed:
    """Optional packages aiohttp switches to by itself (aiodns -> the c-ares
    resolver, Brotli -> `br` decoding): importable but unpinned means the dev
    venv and production take different code paths, which the pins alone
    wouldn't show — e.g. a python3-brotli installed on the Pi later on."""

    def test_importable_and_pinned_is_fine(self):
        assert cpd.unpinned_auto_used({"aiodns": "4.0.0"}, lambda m: m == "aiodns") == []

    def test_importable_but_unpinned_is_reported(self):
        assert cpd.unpinned_auto_used({"aiohttp": "3.13.3"}, lambda m: m == "aiodns") == ["aiodns"]

    def test_not_importable_is_fine(self):
        assert cpd.unpinned_auto_used({}, lambda m: False) == []

    def test_pins_are_matched_by_normalized_name(self):
        pinned = {"brotli": "1.2.0", "Backports_Zstd": "1.0.0"}
        assert cpd.unpinned_auto_used(pinned, lambda m: True) == ["aiodns", "brotlicffi"]

    def test_looks_for_the_module_and_reports_the_distribution(self):
        # The module is `brotli`, the distribution `Brotli`: find_spec is case
        # sensitive, so asking for the distribution's name would miss it.
        assert cpd.unpinned_auto_used({}, lambda m: m == "brotli") == ["Brotli"]
        assert cpd.unpinned_auto_used({}, lambda m: m == "backports.zstd") == ["backports.zstd"]

    def test_covers_what_aiohttp_picks_up(self):
        modules = {module for module, _ in cpd.AUTO_USED}
        assert {"aiodns", "brotli", "brotlicffi", "backports.zstd"} <= modules

    def test_real_importable(self):
        assert cpd.importable("json")
        assert not cpd.importable("no_such_module_xyz")
        assert not cpd.importable("no_such_package_xyz.sub")  # parent missing: no exception

    def test_report_shows_an_unpinned_package(self):
        report = cpd.format_report([], "3.14", (3, 14, 4), "/usr/bin/python3", ["aiodns"])
        assert any(
            line.split() == ["aiodns", "(none)", "(importable)", "UNPINNED"]
            for line in report.splitlines()
        )


def fake_brotli(tmp_path, version=None):
    """A directory that makes `brotli` importable (and, with a version, gives
    it the metadata of a Brotli distribution), as an apt install would."""
    root = tmp_path / "site"
    root.mkdir()
    (root / "brotli.py").write_text("", encoding="utf-8")
    if version is not None:
        info = root / f"Brotli-{version}.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: Brotli\nVersion: {version}\n", encoding="utf-8"
        )
    return root


class TestMain:
    """The real script as a process, against small requirements files of our own."""

    def run(
        self,
        tmp_path,
        requirements,
        python_version=CURRENT_PYTHON + "\n",
        auto=True,
        extra_path=None,
    ):
        req = tmp_path / "requirements.txt"
        req.write_text(requirements + (AUTO_USED_HERE if auto else ""), encoding="utf-8")
        pyver = tmp_path / ".python-version"
        pyver.write_text(python_version, encoding="utf-8")
        env = dict(os.environ)
        if extra_path is not None:
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(extra_path), env.get("PYTHONPATH")) if part
            )
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--requirements",
                str(req),
                "--python-version-file",
                str(pyver),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_exit_0_when_everything_matches(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "pins match, and the Python version too." in result.stdout

    def test_exit_1_on_a_wrong_version(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}.1\n")
        assert result.returncode == 1
        assert "DIFFERS" in result.stdout
        assert "DRIFT: 1 of " in result.stdout and "packages differ" in result.stdout

    def test_exit_1_on_a_missing_package(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\nno-such-distribution-xyz==1.0\n")
        assert result.returncode == 1
        assert "MISSING" in result.stdout

    def test_exit_1_on_another_python_version(self, tmp_path):
        other = f"{sys.version_info.major}.{sys.version_info.minor + 1}\n"
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n", python_version=other)
        assert result.returncode == 1
        assert "MISMATCH" in result.stdout
        assert "Python is not" in result.stdout

    @pytest.mark.skipif(not AUTO_USED_HERE, reason="no automatically used package installed")
    def test_exit_1_on_an_importable_auto_used_package_left_unpinned(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n", auto=False)
        assert result.returncode == 1
        assert "UNPINNED" in result.stdout
        assert "aiohttp uses it by itself" in result.stdout

    def test_exit_1_when_brotli_appears_unpinned(self, tmp_path):
        result = self.run(tmp_path, f"pytest=={PYTEST_VERSION}\n", extra_path=fake_brotli(tmp_path))
        assert result.returncode == 1, result.stdout + result.stderr
        lines = result.stdout.splitlines()
        assert any(
            line.split() == ["Brotli", "(none)", "(importable)", "UNPINNED"] for line in lines
        )
        assert "aiohttp uses it by itself: Brotli" in result.stdout

    def test_exit_0_once_brotli_is_pinned_at_its_version(self, tmp_path):
        result = self.run(
            tmp_path,
            f"pytest=={PYTEST_VERSION}\nBrotli==1.2.0\n",
            extra_path=fake_brotli(tmp_path, version="1.2.0"),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("requirements", ["pytest>=1.0\n", "", "# nothing pinned\n"])
    def test_exit_2_on_requirements_it_cannot_judge(self, tmp_path, requirements):
        result = self.run(tmp_path, requirements, auto=False)
        assert result.returncode == 2
        assert result.stderr.startswith("ERROR:")

    def test_exit_2_on_a_missing_file(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--requirements", str(tmp_path / "nope.txt")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 2


class TestRepoFiles:
    """The pins and Python versions spread over several files, kept in step."""

    def test_the_repo_files_parse(self):
        assert {"aiohttp", "aiofiles", "beautifulsoup4", "lxml", "aiodns"} <= set(repo_pins())
        cpd.parse_python_version(cpd.DEFAULT_PYTHON_VERSION_FILE.read_text(encoding="utf-8"))

    def test_the_repo_pins_cover_the_auto_used_packages_installed_here(self):
        # In the dev venv and CI aiodns comes from requirements.txt itself.
        assert cpd.unpinned_auto_used(repo_pins()) == []

    def test_pyproject_pins_the_same_runtime_versions(self):
        pins = repo_pins()
        for requirement in read_pyproject()["project"]["dependencies"]:
            name, version = requirement.split("==")
            assert pins.get(name) == version, requirement

    def test_dev_tools_are_dev_requirements_not_runtime_pins(self):
        assert not DEV_TOOLS & {cpd.normalize(name) for name in repo_pins()}
        dev_text = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        dev_lines = [line for line in dev_text.splitlines() if not line.startswith("-r ")]
        dev_pins = cpd.parse_pins("\n".join(dev_lines))
        assert set(dev_pins) == DEV_TOOLS
        declared = read_pyproject()["project"]["optional-dependencies"]["dev"]
        assert sorted(declared) == sorted(f"{name}=={v}" for name, v in dev_pins.items())

    def test_pyproject_python_is_the_python_version_file(self):
        python = cpd.parse_python_version(
            cpd.DEFAULT_PYTHON_VERSION_FILE.read_text(encoding="utf-8")
        )
        pyproject = read_pyproject()
        assert pyproject["project"]["requires-python"] == f">={python}"
        assert pyproject["tool"]["mypy"]["python_version"] == python
