import json
import sys

import pytest

from throngtest.settings import Settings, read_settings


def test_defaults(pytester: pytest.Pytester) -> None:
    """Use the documented defaults when no configuration source supplies values."""
    settings = read_settings(pytester.parseconfig())
    assert settings.isolates == 4
    assert settings.check_fingerprints is False
    assert settings.backend == 'temporary_directory'
    assert settings.distribution == 'tests'
    assert settings.python == sys.executable
    assert '.git/' in settings.exclude
    assert settings.preparation == []
    assert settings.packages == []


def test_precedence_and_fresh_environment(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve CLI over environment over TOML and reread changing environment values.

    Reusing the same pytest config after environment changes detects settings
    cached between reads; explicit CLI values then verify the highest priority.
    """
    pytester.makepyprojecttoml('[tool.throngtest]\nisolates = 2\nbackend = "local"\ndistribution = "files"\nexclude = ["large/"]')
    config = pytester.parseconfig()
    assert read_settings(config).isolates == 2
    assert read_settings(config).backend == 'local'
    assert read_settings(config).distribution == 'files'
    assert read_settings(config).exclude == ['large/']
    monkeypatch.setenv('THRONGTEST_ISOLATES', '3')
    assert read_settings(config).isolates == 3
    monkeypatch.setenv('THRONGTEST_ISOLATES', '4')
    assert read_settings(config).isolates == 4
    config = pytester.parseconfig('--isolates=0', '--distribution=tests', '--python=custom python', '--exclude=["custom/"]')
    settings = read_settings(config)
    assert settings.isolates == 0
    assert settings.distribution == 'tests'
    assert settings.python == 'custom python'
    assert settings.exclude == ['custom/']


@pytest.mark.parametrize(('name', 'value'), [('isolates', '-1'), ('isolates', 'nope'), ('backend', ' '), ('distribution', 'other'), ('python', ''), ('exclude', 'oops'), ('preparation', 'oops'), ('packages', 'oops')])
def test_invalid_cli(pytester: pytest.Pytester, name: str, value: str) -> None:
    """Report invalid CLI values as plugin-specific pytest usage errors."""
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig(f'--{name}={value}'))


def test_isolates_cli(pytester: pytest.Pytester) -> None:
    """Advertise --isolates in help and reject the removed --throngtest-workers option."""
    result = pytester.runpytest_subprocess('--help')
    assert result.ret == pytest.ExitCode.OK
    assert '--isolates=COUNT' in result.stdout.str()
    assert '--throngtest-workers' not in result.stdout.str()
    with pytest.raises(pytest.UsageError, match='unrecognized arguments: --throngtest-workers=2'):
        pytester.parseconfig('--throngtest-workers=2')


@pytest.mark.parametrize('name', ['backend', 'distribution', 'python', 'exclude', 'preparation', 'packages', 'check-fingerprints', 'no-check-fingerprints'])
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
    """Reject a TOML isolate count whose type does not match the setting."""
    pytester.makepyprojecttoml('[tool.throngtest]\nisolates = "two"')
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig())


@pytest.mark.parametrize('name', ['preparation', 'packages'])
def test_list_precedence_and_empty_override(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Replace lists by source priority, reread the environment, and allow empty overrides."""
    pytester.makepyprojecttoml(f'[tool.throngtest]\n{name} = ["first", "second"]')
    config = pytester.parseconfig()
    assert getattr(read_settings(config), name) == ['first', 'second']
    monkeypatch.setenv('THRONGTEST_' + name.upper(), '["environment"]')
    assert getattr(read_settings(config), name) == ['environment']
    monkeypatch.setenv('THRONGTEST_' + name.upper(), '["changed"]')
    assert getattr(read_settings(config), name) == ['changed']
    for arguments in ([f'--{name}=["CLI"]'], [f'--{name}', '["CLI"]']):
        assert getattr(read_settings(pytester.parseconfig(*arguments)), name) == ['CLI']
    assert getattr(read_settings(pytester.parseconfig(f'--{name}=[]')), name) == []
    monkeypatch.setenv('THRONGTEST_' + name.upper(), '[]')
    assert getattr(read_settings(pytester.parseconfig()), name) == []


@pytest.mark.parametrize('name', ['preparation', 'packages'])
@pytest.mark.parametrize('source', ['cli', 'environment', 'toml'])
@pytest.mark.parametrize('value', ['not a list', [42], [True], [''], [' \t'], [['nested']]])
def test_invalid_lists(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str, source: str, value: object) -> None:
    """Reject malformed lists and blank entries from every settings source."""
    encoded = json.dumps(value)
    arguments = []
    if source == 'cli':
        arguments = [f'--{name}=' + encoded]
    elif source == 'environment':
        monkeypatch.setenv('THRONGTEST_' + name.upper(), encoded)
    else:
        pytester.makepyprojecttoml(f'[tool.throngtest]\n{name} = ' + encoded)
    with pytest.raises(pytest.UsageError, match='throngtest:'):
        read_settings(pytester.parseconfig(*arguments))


def test_package_defaults_are_independent() -> None:
    """Mutating one session's package list must not affect later sessions."""
    settings = Settings(_sources=[])
    settings.packages.append('example==1.0')
    assert Settings(_sources=[]).packages == []


def test_package_specifications_are_preserved(pytester: pytest.Pytester) -> None:
    """Leave requirements, extras, URLs, quoting, and installer flags to the backend."""
    packages = ['example[extra]>=1.2,<2', 'example @ https://example.org/package.whl', '"local wheel.whl"', '-r requirements.txt']
    assert read_settings(pytester.parseconfig('--packages=' + json.dumps(packages))).packages == packages


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
