"""验证不同声音窗口不会被合并成虚假的持续事件。"""

from types import SimpleNamespace as Row

import pytest

from homeplane.embodied.portable_sound import aggregate


def result(**scores):
    return Row(classifications=[Row(categories=[Row(category_name=k, score=v) for k, v in scores.items()])])


def test_one_loud_window_does_not_make_entire_clip_confident():
    rows = [result(**{'Screaming': .99, 'Speech': .01}), result(**{'Screaming': .1, 'Speech': .9})]
    window = aggregate(rows, ['screaming'])[0]
    assert window['scores']['screaming'] == .1
    assert window['competing_confidence'] == .9


def test_glass_alone_is_not_glass_breaking():
    window = aggregate([result(Glass=.99, Shatter=.01)], ['glass_breaking'])[0]
    assert window['scores']['glass_breaking'] == .01


def test_unknown_labels_or_invalid_scores_rejected():
    with pytest.raises(ValueError):
        aggregate([result(Other=.99)], ['screaming'])
    with pytest.raises(ValueError):
        aggregate([result(Screaming=float('nan'))], ['screaming'])
