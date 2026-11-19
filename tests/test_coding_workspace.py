"""Workspace browser confines reads and versioned edits to the selected root."""

from pathlib import Path

import pytest

from llm_gateway.dashboard.coding_workspace import list_files, read_file, read_image, save_file, search_files


def test_browse_search_and_versioned_save(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'src').mkdir()
    target = root / 'src' / 'module.py'
    target.write_text('print("hello")\n')
    (root / '.secret').write_text('hidden')
    assert list_files(root)['entries'] == [{'name': 'src', 'path': 'src', 'type': 'directory'}]
    opened = read_file(root, 'src/module.py')
    assert search_files(root, 'hello')['matches'][0]['path'] == 'src/module.py'
    saved = save_file(root, 'src/module.py', 'print("updated")\n', opened['sha256'])
    assert saved['sha256'] == read_file(root, 'src/module.py')['sha256']
    with pytest.raises(FileExistsError):
        save_file(root, 'src/module.py', 'stale', opened['sha256'])
    assert target.read_text() == 'print("updated")\n'


def test_rejects_escape_and_symlink_swap(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('private')
    (root / 'linked.txt').symlink_to(outside)
    for name in ('../outside.txt', 'linked.txt', '.secret'):
        with pytest.raises((ValueError, OSError)):
            read_file(root, name)
    assert search_files(root, 'private')['matches'] == []


def test_reads_only_bounded_signature_checked_raster_images(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    png = b'\x89PNG\r\n\x1a\n' + b'preview'
    (root / 'preview.png').write_bytes(png)
    assert read_image(root, 'preview.png') == (png, 'image/png')
    (root / 'fake.png').write_text('<svg onload="alert(1)"></svg>')
    with pytest.raises(ValueError, match='format'):
        read_image(root, 'fake.png')
