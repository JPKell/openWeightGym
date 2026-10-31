"""Remove only the obsolete import shim and its bytecode from an openWeightGym installation."""
import argparse
import shutil
from pathlib import Path


def prune(root: Path) -> None:
    """Remove only the recognized obsolete Frontdesk shim while preserving unexpected files."""
    folder = root / 'frontdesk'
    if not folder.exists() and not folder.is_symlink():
        return
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Legacy package is not an ordinary directory; leaving it untouched.')
    if not (root / 'weightroom/app.py').is_file():
        raise ValueError('Canonical WeightRoom package is missing; leaving the legacy package untouched.')
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        allowed = relative.as_posix() in ('__init__.py', '__pycache__') or (
            relative.parts[0] == '__pycache__' and len(relative.parts) == 2 and path.suffix == '.pyc')
        if not allowed or path.is_symlink():
            raise ValueError('Legacy package contains unexpected files; leaving it untouched.')
    shim = folder / '__init__.py'
    import_line = 'from weightroom import __path__ as __path__'
    known = {import_line, '"""Compatibility import path for weightroom; new code should import weightroom."""\n\n' + import_line}
    if shim.exists() and shim.read_text().strip() not in known:
        raise ValueError('Legacy initializer is not the known compatibility shim; leaving it untouched.')
    shutil.rmtree(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    prune(parser.parse_args().root)
