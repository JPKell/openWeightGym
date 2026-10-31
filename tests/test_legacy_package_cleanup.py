"""Deleting obsolete source must preserve private files and packaging compatibility."""
import importlib.util
from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
module = importlib.util.spec_from_file_location('prune_frontdesk', ROOT / 'scripts/prune-frontdesk.py')
cleanup = importlib.util.module_from_spec(module)
module.loader.exec_module(cleanup)


def installation(tmp_path):
    (tmp_path / 'weightroom').mkdir()
    (tmp_path / 'weightroom/app.py').write_text('')
    old = tmp_path / 'frontdesk'
    old.mkdir()
    (old / '__init__.py').write_text('from weightroom import __path__ as __path__\n')
    return old


def test_cleanup_removes_shim_and_bytecode_preserving_canonical_state(tmp_path):
    old = installation(tmp_path)
    (old / '__pycache__').mkdir()
    (old / '__pycache__/app.cpython-314.pyc').write_bytes(b'old bytecode')
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/state.db').write_bytes(b'private state')
    cleanup.prune(tmp_path)
    assert not old.exists()
    assert (tmp_path / 'weightroom/app.py').is_file()
    assert (tmp_path / 'data/state.db').read_bytes() == b'private state'
    cleanup.prune(tmp_path)  # already retired is safe


@pytest.mark.parametrize('unexpected', ['settings.json', 'data/private.db', '__pycache__/private.key'])
def test_cleanup_refuses_unexpected_files(tmp_path, unexpected):
    old = installation(tmp_path)
    private = old / unexpected
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b'preserve me')
    with pytest.raises(ValueError, match='unexpected'):
        cleanup.prune(tmp_path)
    assert private.read_bytes() == b'preserve me'


def test_cleanup_refuses_alias_directory_and_unknown_initializer(tmp_path):
    old = installation(tmp_path)
    (old / '__init__.py').write_text('from weightroom import __path__ as __path__\nimportant = "private customization"')
    with pytest.raises(ValueError, match='initializer'):
        cleanup.prune(tmp_path)
    (old / '__init__.py').unlink()
    old.rmdir()
    old.symlink_to(tmp_path / 'weightroom', target_is_directory=True)
    with pytest.raises(ValueError, match='ordinary directory'):
        cleanup.prune(tmp_path)
    assert (tmp_path / 'weightroom/app.py').is_file()


def test_package_and_deployer_use_canonical_source_with_legacy_command_compatibility():
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert 'frontdesk' not in metadata['tool']['setuptools']['packages']
    assert 'weightroom' in metadata['tool']['setuptools']['packages']
    assert metadata['project']['scripts']['frontdesk'] == 'weightroom.app:main'
    copies = [line for line in (ROOT / 'scripts/deploy-front.sh').read_text().splitlines() if line.lstrip().startswith('rsync ')]
    assert not any('frontdesk' in line.split() for line in copies)
