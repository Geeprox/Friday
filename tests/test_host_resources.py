from homeplane.host_resources import HostResources


def test_cpu_uses_two_samples_and_memory_is_not_inferred():
    now = [0]
    values = iter([(100, 80, 30, 100), (200, 130, 40, 100)])
    owner = HostResources(reader=lambda: next(values), clock=lambda: now[0])
    first = owner.sample()
    assert first['available'] and first['cpu_percent'] is None
    assert owner.sample() == first  # Polls in the sample window do not invent a rate.
    now[0] = 3
    second = owner.sample()
    assert second['cpu_percent'] == 50
    assert second['memory_used_bytes'] == 40


def test_failure_and_counter_reset_do_not_reuse_old_cpu_or_memory():
    now = [0]
    values = iter([(100, 80, 30, 100), (90, 60, 40, 100), OSError(), (110, 80, 40, 100)])
    def read():
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value
    owner = HostResources(reader=read, clock=lambda: now[0])
    owner.sample()
    now[0] = 3
    assert owner.sample()['cpu_percent'] is None
    now[0] = 6
    failed = owner.sample()
    assert not failed['available'] and failed['memory_used_bytes'] is None
    now[0] = 9
    assert owner.sample()['cpu_percent'] is None


def test_invalid_memory_and_clock_rollback_fail_closed():
    now = [4]
    values = iter([(100, 80, 30, 100), (110, 90, 40, 100), (120, 100, 101, 100)])
    owner = HostResources(reader=lambda: next(values), clock=lambda: now[0])
    owner.sample()
    now[0] = 3
    assert owner.sample()['cpu_percent'] is None
    now[0] = 8
    assert not owner.sample()['available']
