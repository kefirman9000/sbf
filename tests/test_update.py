"""The in-app updater: only verified releases install, every install can be undone, new add-ons are found."""
import hashlib
import io
import os
import zipfile

import pytest

import engine as E

APP = E.KEYRING_SERVICE.upper()


def release_zip(version, extra=None, main_src="print('hi')\n"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        files = {"main.py": main_src, "engine.py": "X = 1\n", "VERSION": version + "\n",
                 "requirements.txt": "keyring>=24.0\n", "README.md": "readme\n"}
        files.update(extra or {})
        for name, text in files.items():
            z.writestr(f"{APP}/{name}", text)
    return buf.getvalue()


@pytest.fixture
def app_dir(tmp_path):
    for name, text in {"main.py": "old\n", "engine.py": "old\n", "VERSION": "1.0.0\n",
                       "requirements.txt": "keyring>=24.0\n"}.items():
        (tmp_path / name).write_text(text)
    return tmp_path


def fake_get(monkeypatch, files):
    monkeypatch.setattr(E, "http_get", lambda url, *a, **k: files[url])


def rel(**kw):
    r = dict(version="2.0.0", tag="v2.0.0", zip_name=f"{APP}-2.0.0.zip", zip_url="zip", sha_url="sha", notes="")
    r.update(kw)
    return r


def test_good_download_passes_its_checksum(monkeypatch):
    data = release_zip("2.0.0")
    fake_get(monkeypatch, {"zip": data, "sha": f"{hashlib.sha256(data).hexdigest()}  x.zip\n".encode()})
    assert E.download_release(rel()) == data


def test_damaged_download_is_refused(monkeypatch):
    data = release_zip("2.0.0")
    fake_get(monkeypatch, {"zip": data[:-10], "sha": f"{hashlib.sha256(data).hexdigest()}  x.zip\n".encode()})
    with pytest.raises(RuntimeError, match="checksum"):
        E.download_release(rel())


@pytest.mark.parametrize("missing", ["zip_url", "sha_url"])
def test_release_without_zip_or_checksum_is_refused(missing):
    with pytest.raises(RuntimeError):
        E.download_release(rel(**{missing: ""}))


def test_unreadable_checksum_file_is_refused(monkeypatch):
    fake_get(monkeypatch, {"sha": b"<html>not found</html>", "zip": b""})
    with pytest.raises(RuntimeError, match="checksum"):
        E.download_release(rel())


def test_latest_release_reads_github_answer(monkeypatch):
    answer = ('{"tag_name": "v2.0.0", "body": "notes", "assets": ['
              f'{{"name": "{APP}-2.0.0.zip", "browser_download_url": "https://z"}},'
              f'{{"name": "{APP}-2.0.0.zip.sha256", "browser_download_url": "https://s"}}]}}')
    monkeypatch.setattr(E, "http_get", lambda url, *a, **k: answer.encode())
    r = E.latest_release("someone/app")
    assert (r["version"], r["zip_url"], r["sha_url"], r["notes"]) == ("2.0.0", "https://z", "https://s", "notes")


def test_install_backs_up_and_undo_restores(app_dir):
    written, backup = E.install_update(release_zip("2.0.0"), str(app_dir), "1.0.0", expect_version="2.0.0")
    assert "main.py" in written and (app_dir / "VERSION").read_text().strip() == "2.0.0"
    E.restore_backup(str(app_dir), backup)
    assert (app_dir / "main.py").read_text() == "old\n"
    assert (app_dir / "VERSION").read_text().strip() == "1.0.0"
    assert E.latest_backup(str(app_dir)) == backup


def test_wrong_version_in_zip_is_refused(app_dir):
    with pytest.raises(RuntimeError, match="version"):
        E.install_update(release_zip("1.9.0"), str(app_dir), "1.0.0", expect_version="2.0.0")
    assert (app_dir / "main.py").read_text() == "old\n"


def test_code_that_does_not_compile_is_refused(app_dir):
    with pytest.raises(SyntaxError):
        E.install_update(release_zip("2.0.0", main_src="def broken(:\n"), str(app_dir), "1.0.0")
    assert (app_dir / "main.py").read_text() == "old\n"


def test_only_top_level_app_files_are_written(app_dir):
    data = release_zip("2.0.0", extra={"tests/test_x.py": "x\n", "docs/a.png": "x", "evil/../../x.py": "x"})
    written, _ = E.install_update(data, str(app_dir), "1.0.0")
    assert all("/" not in w for w in written)
    assert not (app_dir / "tests").exists() and not (app_dir / "docs").exists()


def test_new_requirements_are_found(app_dir):
    data = release_zip("2.0.0", extra={"requirements.txt": "keyring>=24.0\n# comment\nnewthing>=1.0  # why\n"})
    _, backup = E.install_update(data, str(app_dir), "1.0.0")
    assert E.new_requirements(backup, str(app_dir)) == ["newthing>=1.0"]


def test_version_order():
    assert E.version_tuple("1.10.0") > E.version_tuple("1.9.9")
    assert E.version_tuple("v2.0") > E.version_tuple("1.99.99")


def test_changelog_since_keeps_only_newer_sections():
    text = "# Changelog\n\n## 1.3.0\n- c\n\n## 1.2.0\n- b\n\n## 1.1.0\n- a\n"
    out = E.changelog_since(text, "1.1.0")
    assert "1.3.0" in out and "1.2.0" in out and "1.1.0" not in out


def test_dependency_list_matches_requirements():
    """DEPENDENCIES says 'keep in sync with requirements.txt': hold it to that."""
    import tray
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    reqs = set(E._requirement_lines(os.path.join(here, "requirements.txt")))
    known = {d["pip"] for d in E.DEPENDENCIES} | set(tray.TRAY_PIP)
    assert reqs == known
