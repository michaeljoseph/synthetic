import logging

import click

from . import timesheets


@click.option('--debug', help='Enables debug logging.', is_flag=True, default=False)
@click.group(context_settings=dict(help_option_names=[u'-h', u'--help']))
def main(debug):
    """⏰📊 Main command group"""
    logging.basicConfig()
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)


main.add_command(timesheets.store_missing_timesheets)
main.add_command(timesheets.confirm_draft_timesheets)
main.add_command(timesheets.list_timesheets)
main.add_command(timesheets.standup)
main.add_command(timesheets.workflow)
