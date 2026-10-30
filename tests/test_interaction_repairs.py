import pytest

from homeplane.embodied import interaction


def test_disabled_missing_target_is_retained_for_repair_but_cannot_be_enabled(tmp_path,monkeypatch):
    monkeypatch.setattr(interaction,'PRIVATE',tmp_path)
    binding=interaction.Binding(id='a'*32,gesture='body.arms_open',target='removed-target',action='on',enabled=True)
    current=interaction.Configuration(revision=0,enabled=True,bindings=[binding])
    (tmp_path/'interactions.json').write_text(current.model_dump_json())
    with pytest.raises(ValueError):interaction.save(current,{},set())
    disabled=current.model_copy(update={'bindings':[binding.model_copy(update={'enabled':False})]})
    saved=interaction.save(disabled,{},set())
    assert not saved.bindings[0].enabled
    with pytest.raises(ValueError):interaction.save(saved.model_copy(update={'bindings':[binding]}),{},set())


def test_new_missing_target_cannot_enter_disabled_repair_storage(tmp_path,monkeypatch):
    monkeypatch.setattr(interaction,'PRIVATE',tmp_path)
    binding=interaction.Binding(id='b'*32,gesture='body.arms_open',target='unknown',action='on',enabled=False)
    with pytest.raises(ValueError):interaction.save(interaction.Configuration(revision=0,bindings=[binding]),{},set())
