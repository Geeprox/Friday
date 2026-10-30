"""GUI launches must use installed tools without inheriting a login shell."""
from pathlib import Path
from homeplane import media_tools as media_probe


def test_probe_available_with_restricted_gui_path(tmp_path,monkeypatch):
    tool=tmp_path/'ffprobe';tool.write_text('#!/bin/sh\nexit 0\n');tool.chmod(0o700)
    monkeypatch.setattr(media_probe.shutil,'which',lambda name:None)
    monkeypatch.setattr(media_probe,'FALLBACKS',(str(tool),))
    assert media_probe.ffprobe_executable()==str(tool)


def test_probe_rejects_unowned_mode_and_missing_installation(tmp_path,monkeypatch):
    tool=tmp_path/'ffprobe';tool.write_text('untrusted');tool.chmod(0o777)
    monkeypatch.setattr(media_probe.shutil,'which',lambda name:None)
    monkeypatch.setattr(media_probe,'FALLBACKS',(str(tool),str(tmp_path/'missing')))
    assert media_probe.ffprobe_executable() is None
