from synthetic.cli import main

import click
from click.testing import CliRunner


def test_something():
    runner = CliRunner()
    result = runner.invoke(main, ['-h'], prog_name='synthetic')
    assert result.exit_code == 0
    assert 'Usage: synthetic' in result.output
