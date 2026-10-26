from homeplane.archive import MetadataArchive, Segment


def test_requires_authorization_and_keeps_original_bytes(tmp_path):
    path = tmp_path / 'simulated.mp4'
    path.write_bytes(b'simulated, not real media')
    record = Segment('simulated-camera', 1, 2, path.name, complete=True)
    archive = MetadataArchive(tmp_path)
    assert archive.inspect(record)['status'] == 'unavailable'
    archive.authorized = True
    assert archive.inspect(record)['status'] == 'pending'
    assert archive.inspect(record)['status'] == 'ready'
    assert path.read_bytes() == b'simulated, not real media'
    path.unlink()
    assert archive.inspect(record)['status'] == 'expired'


def test_unfinished_segment_never_ready_and_bounded(tmp_path):
    (tmp_path / 'simulated.mp4').write_bytes(b'x')
    archive = MetadataArchive(tmp_path, authorized=True, limit=1)
    segment = Segment('simulated-camera', 1, 2, 'simulated.mp4')
    assert archive.inspect(segment)['status'] == 'pending'
    assert archive.inspect(segment)['status'] == 'pending'
    assert archive.inspect(Segment('simulated-other', 2, 3, 'simulated.mp4'))['reason'] == 'index_capacity_reached'
    assert archive.inspect(Segment('simulated', 0, 1, '../other'))['reason'] == 'invalid_segment'


def test_symlink_cannot_escape(tmp_path):
    (tmp_path / 'escape').symlink_to('/etc/passwd')
    archive = MetadataArchive(tmp_path, authorized=True)
    assert archive.inspect(Segment('simulated', 0, 1, 'escape'))['reason'] == 'symlink_rejected'
