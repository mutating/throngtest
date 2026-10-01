import json
import sys

import pytest

from throngtest.settings import read_settings


def test_defaults(pytester: pytest.Pytester) -> None:
    """Use the documented defaults when no configuration source supplies values."""
    settings = read_settings(pytester.parseconfig())
    assert settings.workers == 4
    assert settings.check_fingerprints is False
    assert settings.backend == 'temporary_directory'
    assert settings.distribution == 'tests'
    assert settings.python == sys.executable
    assert '.git/' in settings.exclude
    assert settings.preparation == []


def test_precedence_and_fresh_environment(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve CLI over environment over TOML and reread changing environment values.

    Reusing the same pytest config after environment changes detects settings
    cached between reads; explicit CLI values then verify the highest priority.
    """
    pytester.makepyprojecttoml('[tool.throngtest]\nworkers = 2\nbackend = "local"\ndistribution = "files"\nexclude = ["large/"]')
    config = pytester.parseconfig()
    assert read_settings(config).workers == 2
    assert read_settings(config).backend == 'local'
    assert read_settings(config).distribution == 'files'
    assert read_settings(config).exclude == ['large/']
    monkeypatch.setenv('THRONGTEST_WORKERS', '3')
    assert read_settings(config).workers == 3
    monkeypatch.setenv('THRONGTEST_WORKERS', '4')
    assert read_settings(config).workers == 4
    config = pytester.parseconfig('--isolates=0', '--distribution=tests', '--python=custom python', '--exclude=["custom/"]')
    settings = read_settings(config)
    assert settings.workers == 0
    assert settings.distribution == 'tests'
    assert settings.python == 'custom python'
    assert settings.exclude == ['custom/']


@pytest.mark.parametrize(('name', 'value'), [('workers', '-1'), ('workers', 'nope'), ('backend', ' '), ('distribution', 'other'), ('python', ''), ('exclude', 'oops'), ('preparation', 'oops')])
def test_invalid_cli(pytester: pytest.Pytester, name: str, value: str) -> None:
    """Report invalid CLI values as plugin-specific pytest usage errors."""
    option = '--isolates' if name == 'workers' else f'--{name}'
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig(f'{option}={value}'))


def test_workers_cli_was_renamed(pytester: pytest.Pytester) -> None:
    """Advertise --isolates in help and reject the removed --throngtest-workers option."""
    result = pytester.runpytest_subprocess('--help')
    assert result.ret == pytest.ExitCode.OK
    assert '--isolates=COUNT' in result.stdout.str()
    assert '--throngtest-workers' not in result.stdout.str()
    with pytest.raises(pytest.UsageError, match='unrecognized arguments: --throngtest-workers=2'):
        pytester.parseconfig('--throngtest-workers=2')


@pytest.mark.parametrize('name', ['backend', 'distribution', 'python', 'exclude', 'preparation', 'check-fingerprints', 'no-check-fingerprints'])
def test_unprefixed_options_replace_old_names(pytester: pytest.Pytester, name: str) -> None:
    """Expose the shorter CLI spelling and reject the removed prefixed name."""
    result = pytester.runpytest_subprocess('--help')
    assert result.ret == pytest.ExitCode.OK
    option = f'--{name}' if name.endswith('check-fingerprints') else f'--{name}={name.upper()}'
    assert option in result.stdout.str()
    assert f'--throngtest-{name}' not in result.stdout.str()
    with pytest.raises(pytest.UsageError, match='unrecognized arguments: --throngtest-'):
        pytester.parseconfig(f'--throngtest-{name}')


def test_invalid_toml_type(pytester: pytest.Pytester) -> None:
    """Reject a TOML worker count whose type does not match the setting."""
    pytester.makepyprojecttoml('[tool.throngtest]\nworkers = "two"')
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig())


def test_preparation_precedence_and_empty_override(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply source precedence to preparation commands and allow explicit empty overrides."""
    pytester.makepyprojecttoml('[tool.throngtest]\npreparation = ["echo first", "echo second"]')
    config = pytester.parseconfig()
    assert read_settings(config).preparation == ['echo first', 'echo second']
    monkeypatch.setenv('THRONGTEST_PREPARATION', '["echo environment"]')
    assert read_settings(config).preparation == ['echo environment']
    config = pytester.parseconfig('--preparation=["echo CLI"]')
    assert read_settings(config).preparation == ['echo CLI']
    config = pytester.parseconfig('--preparation=[]')
    assert read_settings(config).preparation == []
    monkeypatch.setenv('THRONGTEST_PREPARATION', '[]')
    assert read_settings(pytester.parseconfig()).preparation == []


@pytest.mark.parametrize('source', ['cli', 'environment', 'toml'])
@pytest.mark.parametrize('value', ['echo command', [42], [''], [' \t'], [['echo nested']]])
def test_invalid_preparation(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, source: str, value: object) -> None:
    """Reject malformed command lists and blank commands from every settings source."""
    encoded = json.dumps(value)
    arguments = []
    if source == 'cli':
        arguments = ['--preparation=' + encoded]
    elif source == 'environment':
        monkeypatch.setenv('THRONGTEST_PREPARATION', encoded)
    else:
        pytester.makepyprojecttoml('[tool.throngtest]\npreparation = ' + encoded)
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig(*arguments))


def test_fingerprint_flag_precedence(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve fingerprint booleans by source priority and the last explicit CLI flag."""
    pytester.makepyprojecttoml('[tool.throngtest]\ncheck_fingerprints = true')
    config = pytester.parseconfig()
    assert read_settings(config).check_fingerprints is True
    monkeypatch.setenv('THRONGTEST_CHECK_FINGERPRINTS', 'false')
    assert read_settings(config).check_fingerprints is False
    assert read_settings(pytester.parseconfig('--check-fingerprints')).check_fingerprints is True
    monkeypatch.setenv('THRONGTEST_CHECK_FINGERPRINTS', 'true')
    assert read_settings(config).check_fingerprints is True
    assert read_settings(pytester.parseconfig('--no-check-fingerprints')).check_fingerprints is False
    assert read_settings(pytester.parseconfig('--check-fingerprints', '--no-check-fingerprints')).check_fingerprints is False
    assert read_settings(pytester.parseconfig('--no-check-fingerprints', '--check-fingerprints')).check_fingerprints is True


@pytest.mark.parametrize(('source', 'value'), [('environment', 'maybe'), ('environment', '[]'), ('toml', '1'), ('toml', '"true"'), ('toml', '[]')])
def test_invalid_fingerprint_boolean(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, source: str, value: str) -> None:
    """Reject invalid environment booleans and nonboolean TOML fingerprint settings."""
    if source == 'environment':
        monkeypatch.setenv('THRONGTEST_CHECK_FINGERPRINTS', value)
    else:
        pytester.makepyprojecttoml('[tool.throngtest]\ncheck_fingerprints = ' + value)
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig())
