from click.testing import CliRunner
from synthetic import cli


def test_help():
    result = CliRunner().invoke(cli, ['-h'], prog_name='synthetic')
    assert result.exit_code == 0
    assert 'Usage: synthetic' in result.output
