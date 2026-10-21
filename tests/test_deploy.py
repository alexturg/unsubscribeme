import asyncio
from contextlib import contextmanager
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request
import pytest

from rssbot import web


spec = importlib.util.spec_from_file_location('deploy_receive', Path(__file__).parents[1] / 'deploy/receive.py')
receive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receive)


def test_readiness_requires_startup_and_database(monkeypatch):
    request = make_mocked_request('GET', '/healthz', app={web.READY_KEY: False})
    assert asyncio.run(web.health(request)).status == 503

    @contextmanager
    def broken_database():
        raise RuntimeError('private database error')
        yield

    request.app[web.READY_KEY] = True
    monkeypatch.setattr(web, 'session_scope', broken_database)
    response = asyncio.run(web.health(request))
    assert response.status == 503
    assert 'private' not in response.text

    @contextmanager
    def database():
        yield SimpleNamespace(execute=lambda statement: None)

    monkeypatch.setattr(web, 'session_scope', database)
    response = asyncio.run(web.health(request))
    assert response.status == 200
    assert json.loads(response.text)['ready'] is True


@pytest.mark.parametrize('name,kind', [('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE), ('.env', tarfile.REGTYPE)])
def test_deploy_rejects_unsafe_archives(tmp_path, name, kind):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        archive.addfile(entry)
    stream.seek(0)
    with pytest.raises(ValueError):
        receive.unpack(stream, tmp_path)


def test_failed_start_restores_previous_release(tmp_path, monkeypatch):
    previous = tmp_path / 'old'
    previous.mkdir()
    (tmp_path / 'releases').mkdir()
    (tmp_path / 'current').symlink_to(previous)
    monkeypatch.setattr(receive, 'BASE', tmp_path)
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND', 'deploy ' + 'a' * 40)
    monkeypatch.setattr(receive, 'unpack', lambda stream, release: None)
    monkeypatch.setattr(receive, 'run', lambda *args: None)
    commands = []
    monkeypatch.setattr(receive, 'control', commands.append)
    checks = iter([RuntimeError('failed readiness'), None])

    def ready():
        error = next(checks)
        if error:
            raise error

    monkeypatch.setattr(receive, 'wait_ready', ready)
    with pytest.raises(RuntimeError, match='failed readiness'):
        receive.main()
    assert (tmp_path / 'current').resolve() == previous
    assert commands == ['stop', 'snapshot', 'start', 'stop', 'start']
