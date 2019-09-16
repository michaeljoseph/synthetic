import logging

import click

from . import timesheets


@click.option('--debug', help='Enables debug logging.', is_flag=True, default=False)
@click.group(context_settings=dict(help_option_names=[u'-h', u'--help']))
def main(debug):
    """⏰📊 Synthetic timesheets and approvals for naturalhr"""
    logging.basicConfig(
        format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        level=logging.DEBUG if debug else logging.INFO,
    )


main.add_command(timesheets.store_missing_timesheets)
main.add_command(timesheets.confirm_draft_timesheets)
main.add_command(timesheets.list_timesheets)
main.add_command(timesheets.workflow)
