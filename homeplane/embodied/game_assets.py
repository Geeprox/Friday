"""Explicit public graphics only; never resolve a request to an arbitrary path."""
from pathlib import Path

_HERE=Path(__file__).resolve().parent
_SHARED=_HERE.parent/'static'
_FILES={name:_HERE/'static'/name for name in
        ('index.js','index.css','games.js','games.css','game-scenes.js','ski-scene.js','game-teaching.js','game-presentation.js','game-sound.js')}
_FILES.update({name:_HERE/'static/game-art'/name for name in
               ('winter-props.png','winter-scene.png','summer-props.png','summer-scene.png',
                'quest-stream-scene.png','quest-camp-scene.png','quest-props.png',
                'tennis-near-v1.png','tennis-far-v1.png')})
_FILES.update({'friday-icon.png':_SHARED/'brand/friday.png',
               'body-motions.js':_SHARED/'body-motions.js'})
_FILES.update({'vendor/'+name:_SHARED/'vendor'/name for name in ('three.module.js','three.core.js')})
_FILES.update({'motion-art/body/'+name:_SHARED/'motion-art/body'/name for name in ('body.gltf','body-renderer.js')})

_BODY_TARGETS = (
    'pose-horizontal.png',
    'pose-overhead.png',
    'body-hands_up.png',
    'body-squat.png',
    'exercise-squat.png',
    'exercise-high_knee.png',
    'body-arms_open.png',
    'body-arms_close.png',
    'body-left_hand_up.png',
    'body-right_hand_up.png',
    'body-lean_left.png',
    'body-lean_right.png',
    'body-lateral.png',
    'body-arm_effort.png',
    'body-swing.png',
    'exercise-jack.png',
    'exercise-side_lunge.png',
    'exercise-side_step.png',
    'exercise-side_bend.png',
    'exercise-balance.png',
    'exercise-cross_knee.png',
)
_FILES.update({'motion-art/body/targets/'+name:_SHARED/'motion-art/body/targets'/name for name in _BODY_TARGETS})

def game_asset_path(name: str) -> Path | None:
    return _FILES.get(name)
