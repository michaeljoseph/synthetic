from click.testing import CliRunner
from synthetic import synthetic


def test_help():
    result = CliRunner().invoke(synthetic, ['-h'], prog_name='synthetic')
    assert result.exit_code == 0
    assert 'Usage: synthetic' in result.output
