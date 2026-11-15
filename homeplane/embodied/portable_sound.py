"""用本地 YAMNet 分类已授权摄像头的两秒 PCM，不访问麦克风或保存音频。"""

import base64
import contextlib
import json
import math
import os
import sys
import zipfile

LABELS = {
    'baby_crying': ('Baby cry, infant cry',),
    'crying': ('Crying, sobbing',),
    'sobbing': ('Crying, sobbing',),
    'screaming': ('Screaming',),
    'glass_breaking': ('Glass', 'Shatter'),
    'smoke_detector': ('Smoke detector, smoke alarm',),
    'fire_alarm': ('Fire alarm',),
}


def aggregate(results, requested):
    """不同模型窗口取最低目标分和最高竞争分，不能把短脉冲扩成两秒确认。"""
    if not results or len(results) > 8:
        raise ValueError('sound_model_unavailable')
    samples, rivals = [], []
    known = {name for key in requested for name in LABELS[key]}
    for result in results:
        if len(result.classifications) != 1:
            raise ValueError('sound_model_unavailable')
        scores = {}
        for category in result.classifications[0].categories:
            name, score = category.category_name, category.score
            if (name in scores or not isinstance(name, str) or type(score) not in (int, float)
                    or not math.isfinite(score) or not 0 <= score <= 1):
                raise ValueError('sound_model_unavailable')
            scores[name] = score
        if not known <= scores.keys():
            raise ValueError('sound_labels_unavailable')
        samples.append({key: min(scores[name] for name in LABELS[key]) for key in requested})
        rivals.append(max((score for name, score in scores.items() if name not in known), default=0))
    return [dict(offset=0, duration=2, scores={key: min(row[key] for row in samples) for key in requested},
                 competing_confidence=max(rivals), aggregation='minimum_across_model_windows')]


def main():
    if sys.argv[1:] != ['--camera-pcm']:
        raise ValueError('explicit_camera_pcm_required')
    from .camera_guardian import exit_with_owner
    from .paths import MODELS
    from .portable_models import require_cpu_support, verify

    exit_with_owner(int(os.environ.get('FRIDAY_SOUND_OWNER_PID', '0')))
    require_cpu_support()
    folder = MODELS/'mediapipe'
    verify(folder, ['yamnet.tflite'])
    with zipfile.ZipFile(folder/'yamnet.tflite') as bundle:
        names = set(bundle.read('yamnet_label_list.txt').decode().splitlines())
    supported = {key for key, labels in LABELS.items() if set(labels) <= names}
    with contextlib.redirect_stdout(sys.stderr):
        import mediapipe as mp
        import numpy as np
        options = mp.tasks.audio.AudioClassifierOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(folder/'yamnet.tflite')),
            running_mode=mp.tasks.audio.RunningMode.AUDIO_CLIPS, max_results=-1)
        classifier = mp.tasks.audio.AudioClassifier.create_from_options(options)
    try:
        print(json.dumps(dict(type='ready', version=1, sample_rate=16000,
                              known_classifications=sorted(supported))), flush=True)
        while line := sys.stdin.buffer.readline(100_001):
            if len(line) > 100_000 or not line.endswith(b'\n'):
                raise ValueError('sound_protocol_invalid')
            value = json.loads(line)
            if (not isinstance(value, dict) or type(value.get('id')) is not int or value['id'] <= 0
                    or not isinstance(value.get('pcm'), str) or len(value['pcm']) > 86_000
                    or not isinstance(value.get('identifiers'), list)
                    or not 1 <= len(value['identifiers']) <= 16
                    or any(not isinstance(v, str) for v in value['identifiers'])
                    or len(set(value['identifiers'])) != len(value['identifiers'])
                    or not set(value['identifiers']) <= supported):
                raise ValueError('sound_protocol_invalid')
            pcm = base64.b64decode(value['pcm'], validate=True)
            if len(pcm) != 64000:
                raise ValueError('sound_window_invalid')
            data = mp.tasks.components.containers.AudioData.create_from_array(
                np.frombuffer(pcm, dtype='<i2').astype(np.float32)/32768, sample_rate=16000)
            with contextlib.redirect_stdout(sys.stderr):
                results = classifier.classify(data)
            print(json.dumps(dict(type='result', version=1, id=value['id'],
                                  windows=aggregate(results, value['identifiers'])), allow_nan=False), flush=True)
            del pcm, data, results, value, line
    finally:
        classifier.close()


if __name__ == '__main__':
    main()
