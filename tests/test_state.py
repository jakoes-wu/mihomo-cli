"""R-02：state.yaml 原子写与并发锁。"""

import multiprocessing
import os

import pytest

from mihomo_cli.state import (LockTimeout, Paths, StateError, StateLock, ensure_home, load_state, new_profile,
                              parse_state, save_state)


def _writer(home, tag, rounds):
    paths = Paths(home)
    for i in range(rounds):
        with StateLock(paths, 30):
            st = load_state(paths)
            st["profiles"]["%s%d" % (tag, i)] = new_profile("nodes")
            save_state(paths, st)


def test_concurrent_writers(tmp_path):
    home = str(tmp_path / "h")
    paths = Paths(home)
    ensure_home(paths)
    procs = [multiprocessing.Process(target=_writer, args=(home, tag, 15)) for tag in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    st = load_state(paths)
    assert len(st["profiles"]) == 30  # 两个进程的写入一个不丢


def test_lock_timeout(tmp_path):
    paths = Paths(str(tmp_path))
    ensure_home(paths)
    with StateLock(paths, 1):
        with pytest.raises(LockTimeout):
            StateLock(paths, 0.3).acquire()


def test_home_mode(tmp_path):
    paths = Paths(str(tmp_path / "h"))
    ensure_home(paths)
    assert oct(os.stat(paths.home).st_mode & 0o777) == "0o700"


def test_bad_state(tmp_path):
    with pytest.raises(StateError):
        parse_state("version: 1\nprofiles: [1, 2]\n")
