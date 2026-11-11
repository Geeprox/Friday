"""验证不同画面间隔下的滑雪规则与试滑。"""

import math

import pytest

from homeplane.embodied.motion_games import MotionGames
from homeplane.embodied.ski_route import SKI_COURSES, SKI_GATES, SKI_ROUTE, get_ski_trail
from tests.embodied.test_motion_games import LOCK, human


def test_warmup_uses_real_steering_and_wrist_motion_without_consuming_a_race():
    game = MotionGames()
    game.select('ski', difficulty='challenge', ski_trail='pine-slalom')
    game.reset('ski', ski_warmup=True)
    now = 0.0

    def feed(x, *, seconds=1, arms=False, occluded=False, capture=True):
        nonlocal now
        for _ in range(int(seconds / .05)):
            now += .05
            person = human(x=x)
            if arms:
                swing = .14 * math.sin(now * math.tau / .8)
                person['joints']['leftWrist']['y'] = .47 + swing
                person['joints']['rightWrist']['y'] = .47 - swing
            if occluded:
                person['joints'].pop('rightWrist')
            game.update([person], LOCK, now, [{'person_id': 1, 'gesture': 'yeah'}],
                        captured_at=now if capture else now - 2)
        ski = game.public['ski']
        assert ski['points'] == ski['distance'] == ski['speed'] == game.ski_elapsed == game.elapsed == 0
        assert ski['event'] is None and not ski['guide']['active']
        assert game.selected_mode == 'ski'
        return ski['warmup']['step']

    assert feed(.5) == 'left'
    assert feed(.42) == 'left'
    assert feed(.58, capture=False) == 'left'
    assert feed(.58) == 'right'
    assert feed(.42) == 'arms'
    assert feed(.42, arms=True) == 'arms'
    assert feed(.5, arms=True, occluded=True) == 'arms'
    assert feed(.5, seconds=1.6, arms=True) == 'ready'

    game.reset('ski')
    assert game.public['ski']['warmup'] is None
    assert game.public['ski_trail'] == 'pine-slalom' and game.public['difficulty'] == 'challenge'
    assert game.public['ski']['distance'] == 0
    for _ in range(80):
        now += .05
        p = human()
        swing = .14 * math.sin(now * math.tau / .8)
        p['joints']['leftWrist']['y'] = .47 + swing
        p['joints']['rightWrist']['y'] = .47 - swing
        game.update([p], LOCK, now)
    assert game.public['ski']['distance'] > 0


def test_warmup_requires_new_frames_and_drops_unfinished_holds():
    game = MotionGames()
    game.reset('ski', ski_warmup=True)
    for i in range(20):
        game.update([human()], LOCK, i * .05)
    for i in range(20, 25):
        game.update([human(x=.58)], LOCK, i * .05)
    progress = game.public['ski']['warmup']
    assert progress['step'] == 'left' and 0 < progress['fraction'] < 1
    for i in range(25, 32):
        game.update([human(x=.58)], LOCK, i * .05, captured_at=1.2)
    assert game.public['ski']['warmup'] == {'step': 'left', 'fraction': 0}
    for i in range(32, 40):
        game.update([human(x=.58)], LOCK, i * .05)
    assert game.public['ski']['warmup']['step'] == 'right'
    game.set_paused(True)
    assert game.public['ski']['warmup'] == {'step': 'right', 'fraction': 0}
    game.suspend_source()
    assert game.public['ski']['warmup'] == {'step': 'left', 'fraction': 0}


def run_ski(cadence, *, duration=None, lateral=0, difficulty='standard', trail=None):
    game = MotionGames()
    if trail is not None:
        game.select('ski', ski_trail=trail)
    game.reset("ski", difficulty=difficulty)
    elapsed = 0.0
    for i in range(20000):
        dt = cadence[i % len(cadence)]
        if duration is not None:
            dt = min(dt, duration - elapsed)
        position = lateral(game) if callable(lateral) else lateral
        game.ski({"arm_effort": 1, "lateral": position}, dt, elapsed + dt)
        elapsed += dt
        if game.complete or duration is not None and elapsed >= duration - 1e-9:
            return game
    raise AssertionError("ski route never finished")


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_distance_for_constant_input_is_independent_of_frame_cadence(difficulty):
    reference = run_ski((0.02,), duration=4, difficulty=difficulty)
    for cadence in [(0.05,), (0.1,), (0.017, 0.08, 0.033, 0.12)]:
        other = run_ski(cadence, duration=4, difficulty=difficulty)
        assert other.distance == pytest.approx(reference.distance, abs=1e-8)
        assert other.speed == pytest.approx(reference.speed, abs=1e-8)


def test_every_complete_run_encounters_the_same_obstacles():
    counts = []
    for cadence in [(0.02,), (0.05,), (0.1,), (0.017, 0.08, 0.033, 0.12)]:
        game = run_ski(cadence, lateral=1)
        counts.append(game.passed + game.collisions)
    assert len(set(counts)) == 1, counts


def test_route_contains_real_crossing_targets_and_edge_camping_cannot_earn_three_stars():
    game = run_ski((0.05,), lateral=1)
    public = game.publish([], "complete", "")['ski']
    assert public.get("gate_count", 0) >= 12
    assert public['gates_passed'] == 0
    assert public['gates_missed'] == public['gate_count']
    assert public['stars'] < 3


def next_gate_center(game):
    gate = game.ski_public([], 'playing')['guide']['next_gate']
    return gate['x'] if gate else 0


def test_all_three_courses_have_authored_increasing_crossing_challenges():
    assert len(SKI_GATES) == 18
    assert len({f.id for f in SKI_ROUTE}) == len(SKI_ROUTE)
    assert list(SKI_ROUTE) == sorted(SKI_ROUTE, key=lambda f: (f.at, f.id))
    assert [sum(g.course == i for g in SKI_GATES) for i in range(3)] == [6, 6, 6]
    assert all(0 < f.at < 180 and abs(f.x) <= 1 for f in SKI_ROUTE)
    assert SKI_COURSES[0]['gate_width'] > SKI_COURSES[1]['gate_width'] > SKI_COURSES[2]['gate_width']
    assert max(abs(b.x-a.x) for a, b in zip(SKI_GATES[:6], SKI_GATES[1:6])) < max(
        abs(b.x-a.x) for a, b in zip(SKI_GATES[12:], SKI_GATES[13:]))


@pytest.mark.parametrize('difficulty', ['easy', 'standard', 'challenge'])
def test_steering_through_published_targets_has_a_clear_corridor_and_stable_reward(difficulty):
    results = []
    for cadence in [(0.02,), (0.1,), (0.017, 0.08, 0.033, 0.12)]:
        game = run_ski(cadence, lateral=next_gate_center, difficulty=difficulty)
        state = game.publish([], 'complete', '')['ski']
        assert state['gates_passed'] == 18
        assert state['gates_missed'] == state['collisions'] == 0
        assert state['best_combo'] == 18
        assert state['stars'] == 3
        assert len(game.ski_results) == len(SKI_ROUTE)
        results.append((state['points'], state['passed'], state['best_combo']))
    assert len(set(results)) == 1


@pytest.mark.parametrize('offset,passed', [(0, True), (.48, True), (-.48, True), (.481, False), (-.481, False)])
def test_a_gate_is_scored_once_at_its_actual_crossing(offset, passed):
    game = MotionGames()
    game.reset('ski')
    game.distance, game.speed = 7.99, 2.4
    game.ski({'arm_effort': 1, 'lateral': offset}, .05, 1)
    assert game.gates_passed == int(passed)
    assert game.gates_missed == int(not passed)
    assert game.ski_event_sequence == 1
    assert game.ski_event['kind'] == ('gate' if passed else 'miss')
    for _ in range(10):
        game.ski({'arm_effort': 0, 'lateral': offset}, .02, 1)
    assert game.ski_event_sequence == 1


def test_collision_slowdown_applies_at_the_crossing_not_the_end_of_a_frame():
    tree = next(f for f in SKI_ROUTE if f.kind == 'tree')
    results = []
    for cadence in [(0.08,), (0.02,) * 4]:
        game = MotionGames()
        game.reset('ski')
        game.distance, game.speed = tree.at - .01, 2.4
        for dt in cadence:
            game.ski({'arm_effort': 1, 'lateral': tree.x}, dt, 1)
        assert game.collisions == 1
        assert game.ski_event_sequence == 1
        results.append((game.distance, game.speed))
    assert results[0] == pytest.approx(results[1], abs=1e-8)


def test_course_boundary_remainder_uses_the_next_courses_speed():
    results = []
    for cadence in [(0.1,), (0.02,) * 5]:
        game = MotionGames()
        game.reset('ski')
        game.distance, game.speed = 59.99, 2.4
        for dt in cadence:
            game.ski({'arm_effort': 1, 'lateral': 0}, dt, 1)
        assert game.ski_course()[0] == 1
        results.append((game.distance, game.speed))
    assert results[0] == pytest.approx(results[1], abs=1e-8)


def test_visible_gates_and_trees_share_one_world_plane_and_pause_preserves_earned_work():
    game = run_ski((.05,), duration=12, lateral=next_gate_center)
    before = game.publish([{'lateral': 0}], 'playing', '')['ski']
    assert before['gates'] and before['obstacles']
    for item in [*before['gates'], *before['obstacles']]:
        feature = next(f for f in SKI_ROUTE if f.id == item['id'])
        assert item['z'] == pytest.approx((feature.at-game.distance) * 3)
    paused = game.set_paused(True)['ski']
    assert not paused['guide']['active']
    for key in ['points', 'gates_passed', 'gates_missed', 'best_combo', 'combo', 'gates', 'obstacles', 'trees']:
        assert paused[key] == before[key]
    game.begin_source()
    recovery = game.publish([], 'paused', '')['ski']
    assert recovery['gates_passed'] == before['gates_passed']
    assert recovery['points'] == before['points']
    game.reset('ski')
    replay = game.publish([], 'waiting', '')['ski']
    assert replay['points'] == replay['gates_passed'] == replay['gates_missed'] == replay['best_combo'] == 0
    assert replay['event'] is replay['stars'] is None
    assert not game.ski_results


def test_preview_geometry_and_each_completed_section_use_the_scored_gate_plan():
    game = MotionGames()
    assert game.public['ski']['route'] is None
    assert game.public['ski']['trees'] is None
    selected = game.select('ski', difficulty='challenge')['ski']
    assert selected['section_results'] is None
    assert selected['route'][0] == (0, 0, 0) and selected['route'][-1] == (selected['total_distance'], 0, 0)
    assert selected['route'][1:-1] == [
        (gate.at, gate.x, gate.width * game.difficulty.ski_gate) for gate in SKI_GATES
    ]
    assert selected['trees'] == [(feature.at, feature.x) for feature in SKI_ROUTE if feature.kind == 'tree']

    run = run_ski((.05,), lateral=lambda g: next_gate_center(g) if g.distance < 60 else 1)
    result = run.publish([], 'complete', '')['ski']
    assert [row['name'] for row in result['section_results']] == [spec['name'] for spec in SKI_COURSES]
    assert [row['gate_count'] for row in result['section_results']] == [6, 6, 6]
    assert [row['gates_passed'] for row in result['section_results']] == [6, 0, 0]
    assert result['gates_passed'] == sum(row['gates_passed'] for row in result['section_results'])
    run.reset('ski')
    assert run.public['ski']['section_results'] is None


@pytest.mark.parametrize('trail,length,gate_counts', [
    ('first-snow', 54, [5]), ('pine-slalom', 92, [4, 4]), ('summit', 180, [6, 6, 6]),
])
def test_each_named_trail_drives_finish_gates_time_and_replay(trail, length, gate_counts):
    plan = get_ski_trail(trail)
    outcomes = []
    for cadence in [(.02,), (.12, .019, .057)]:
        game = run_ski(cadence, lateral=next_gate_center, trail=trail)
        result = game.publish([], 'complete', '')
        ski = result['ski']
        assert result['ski_trail'] == trail
        assert ski['distance'] == ski['total_distance'] == length and ski['progress'] == 1
        assert ski['trees'] == [(feature.at, feature.x) for feature in plan.features if feature.kind == 'tree']
        assert ski['gates_passed'] == ski['gate_count'] == sum(gate_counts)
        assert ski['gates_missed'] == ski['collisions'] == 0
        assert ski['stars'] == 3 and ski['time_seconds'] > 0
        assert [row['gates_passed'] for row in ski['section_results']] == gate_counts
        assert ski['route'] == list(plan.path)
        outcomes.append((ski['time_seconds'], ski['points']))

        reconnected = game.begin_source()
        assert reconnected['ski']['time_seconds'] == ski['time_seconds']
        game.reset('ski')
        assert game.public['ski_trail'] == trail and game.public['ski']['route'] == ski['route']
        assert game.public['ski']['time_seconds'] is None and game.public['ski']['distance'] == 0

    assert outcomes[0] == outcomes[1]
    missed = run_ski((.07,), lateral=1, trail=trail).publish([], 'complete', '')['ski']
    assert missed['gates_missed'] == sum(gate_counts) and missed['stars'] == 1
