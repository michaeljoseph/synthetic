import json
import logging
import os
from datetime import datetime
from pathlib import Path

import attr
import click
import holidays
import requests
import requests_cache
from dateutil.relativedelta import relativedelta
from dateutil.rrule import DAILY, FR, MO, TH, TU, WE, rrule
from pycookiecheat import chrome_cookies
from requests_html import HTMLSession
from terminaltables import AsciiTable
from workdays import networkdays

log = logging.getLogger(__name__)
requests_cache.install_cache()

# FIXME: envvar
STANDUP_PATH = Path.home().joinpath('Work/standups')
NATURAL_HR = 'https://www.naturalhr.net'
NATURAL_HR_COOKIE = 'PHPSESSID'
HEADERS = {
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0',
    'Upgrade-Insecure-Requests': '1',
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_12_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/67.0.3396.99 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
COOKIES = {
    '_ga': 'GA1.2.2023189312.1467903497',
    'SERVERID': 'http27_49-6020',
    '_gid': 'GA1.2.607146262.1531921353',
}
last_choice = None
DEFAULT_REFERENCES = ['Quidco BAU']


@attr.s
class TimeSheet(object):
    week = attr.ib()
    status = attr.ib()
    hours = attr.ib()
    links = attr.ib(factory=list)

    def link(self, link_type):
        for link in self.links:
            if link_type in link:
                return link
        return None


@attr.s
class TimeSheetEntry(object):
    week = attr.ib()
    date = attr.ib()
    start_time = attr.ib()
    end_time = attr.ib()
    breaks = attr.ib()
    reference = attr.ib()
    comments = attr.ib()


def echo(colour, message):
    click.secho(str(message), fg=colour, bold=True)


def to_ascii_table(data):
    return AsciiTable(
        [list(data[0].keys())] + [list(timesheet.values()) for timesheet in data]
    ).table


def get_session(cookie=None):
    session_cookie = chrome_cookies(NATURAL_HR).get(NATURAL_HR_COOKIE)
    if not session_cookie:
        log.error("Could't find a valid session cookie, please log in to Natural HR")
        raise click.Abort

    session = HTMLSession(mock_browser=True)
    session.cookies = requests.cookies.cookiejar_from_dict(
        dict(COOKIES, **{NATURAL_HR_COOKIE: session_cookie})
    )

    home_page = f'{NATURAL_HR}/hr/'
    home_response = session.get(
        home_page, headers=dict(HEADERS, **{'Origin': home_page, 'Referer': home_page})
    )

    if not home_response.status_code == 200:
        log.error("Could't find a valid session cookie, please log in to Natural HR")
        raise click.Abort

    return session


def natural_api(session, url):
    url_headers = {'Origin': url, 'Referer': url}
    return session.get(url, headers=dict(HEADERS, **url_headers))


def natural_api_post(session, url, params):
    url_headers = {'Origin': url, 'Referer': url}
    headers = dict(HEADERS, **url_headers)

    # https://stackoverflow.com/a/22974646
    r = session.post(
        url,
        headers=headers,
        files={key: (None, value) for key, value in params.items()},
    )
    r.raise_for_status()

    return r


def get_references(session):
    add_timesheet_url = '{}/hr/self-service/timesheets/timesheet-add'.format(NATURAL_HR)

    references = natural_api(session, add_timesheet_url).html.xpath(
        '//*[@id="reference"]/option'
    )
    return [reference.attrs['value'] for reference in references][1:]


def get_timesheets(session, status=None):
    timesheet_index_url = '{}/hr/self-service/timesheets/index'.format(NATURAL_HR)

    rows = natural_api(session, timesheet_index_url).html.xpath('//tr')

    timesheets = []
    for row in rows[1:]:
        values = row.text.split('\n')

        timesheet = TimeSheet(
            week=values[0], hours=values[2], status=values[3], links=list(row.links)
        )
        if not status:
            timesheets.append(timesheet)
        elif timesheet.status == status:
            timesheets.append(timesheet)

    return timesheets


def get_timesheet_entries(session, timesheet):
    timesheet_view_url = '{}{}'.format(NATURAL_HR, timesheet.link('timesheet-view'))

    rows = natural_api(session, timesheet_view_url).html.xpath('//tr')

    entries = []
    for row in rows[1:]:
        values = row.text.split('\n')[:5]
        if len(values) > 4:
            entry = TimeSheetEntry(
                week=timesheet.week,
                date=values[0],
                start_time=values[1],
                end_time=values[2],
                breaks=values[3],
                reference=values[4],
                comments=None,
            )
            entries.append(entry)

    return entries


@click.option('--debug', help='Enables debug logging.', is_flag=True, default=False)
@click.group(context_settings=dict(help_option_names=[u'-h', u'--help']))
def synthetic(debug: bool):
    """Synthetic timesheets and approvals for naturalhr"""
    logging.basicConfig(
        format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        level=logging.DEBUG if debug else logging.INFO,
    )


@synthetic.command('list')
def list_timesheets():
    """List last month's timesheets"""
    session = get_session()

    last_months_timesheets = sorted(
        get_timesheets(session),
        key=lambda timesheet: datetime.strptime(timesheet.week, '%d/%m/%Y'),
        reverse=True,
    )[:4]
    # TODO: num_weeks argument

    for timesheet in last_months_timesheets:
        echo('blue', '{week} {status} {hours}'.format(**attr.asdict(timesheet)))
        timesheet_entries = get_timesheet_entries(session, timesheet)
        print(
            to_ascii_table(
                [attr.asdict(timesheet_entry) for timesheet_entry in timesheet_entries]
            )
        )


def choose_reference(references):
    global last_choice

    choice_text = 'Choose a reference (-1 to display references)'
    reference_text = None
    while reference_text is None:
        if last_choice:
            reference_idx = click.prompt(choice_text, type=int, default=last_choice)
        else:
            click.echo(
                ' '.join(
                    [
                        '[{}] {}'.format(index, ref)
                        for index, ref in enumerate(references)
                        if ref in DEFAULT_REFERENCES
                    ]
                )
            )
            reference_idx = click.prompt(choice_text, type=int)

        if reference_idx == -1:
            click.echo(
                ' '.join(
                    [
                        '[{}] {}'.format(index, ref)
                        for index, ref in enumerate(references)
                    ]
                )
            )
        elif reference_idx > len(references):
            echo('red', 'That is not a valid reference selection.')
        else:
            last_choice = reference_idx
            return references[reference_idx]
    return None


def ensure_references(timesheet_entries, references):
    reference_store = STANDUP_PATH.joinpath('synthetic.json')
    stored_references = (
        json.loads(reference_store.read_text()) if reference_store.exists() else {}
    )

    updated_timesheets = []
    for timesheet_entry in timesheet_entries:
        ymd = '{:%Y-%m-%d}'.format(timesheet_entry.date)

        os.system('bat {}'.format(STANDUP_PATH.joinpath('{}.md'.format(ymd))))

        if not timesheet_entry.reference and not stored_references.get(ymd):
            timesheet_entry.reference = choose_reference(references)
        elif not timesheet_entry.reference:
            timesheet_entry.reference = stored_references[ymd]

        echo('yellow', timesheet_entry.reference)
        updated_timesheets.append(timesheet_entry)

    stored_references.update(
        {
            ymd: timesheet.reference
            for timesheet in updated_timesheets
            if timesheet.reference != 'Off Project Work'
        }
    )
    reference_store.write_text(json.dumps(stored_references))

    return updated_timesheets


def timesheet_from_standup(day):
    standup_path = STANDUP_PATH.joinpath('{:%Y-%m-%d}.md'.format(day))
    if not standup_path.exists():
        # FIXME: my exception
        raise Exception('Missing standup:\n\t{}'.format(standup_path))

    comments = standup_path.read_text().rstrip()
    comments = '\n'.join(comments.split('\n')[1:])

    week_start = day + relativedelta(weekday=MO(-1))
    if any([x == comments for x in ['Annual Leave', 'Public Holiday']]):
        return [
            TimeSheetEntry(week_start, day, '0900', '1700', '0', 'Holiday', comments)
        ]

    if any([x == comments for x in ['Off sick']]):
        return [
            TimeSheetEntry(week_start, day, '0900', '1700', '0', 'Off ill', comments)
        ]

    # docs: Monday is 0
    if day.weekday() == 4:
        return [
            TimeSheetEntry(week_start, day, '0900', '1700', '60', None, comments),
            TimeSheetEntry(
                week_start,
                day,
                '1700',
                '1800',
                '0',
                'Off Project Work',
                'Tips and clips.',
            ),
        ]
    else:
        return [TimeSheetEntry(week_start, day, '0900', '1800', '60', None, comments)]

    raise Exception('No entries for {}'.format(day))


def store_timesheets(session, timesheet_entries):
    add_timesheet_url = '{}/hr/self-service/timesheets/timesheet-add'.format(NATURAL_HR)

    for timesheet_entry in timesheet_entries:
        # TODO: check if there are existing entries
        natural_api_post(
            session,
            add_timesheet_url,
            {
                'week_beginning': '{:%d/%m/%y}'.format(timesheet_entry.week),
                'date': '{:%a%d/%m/%Y}'.format(timesheet_entry.date),
                'start': timesheet_entry.start_time,
                'end': timesheet_entry.end_time,
                'breaks': timesheet_entry.breaks,
                'reference': timesheet_entry.reference,
                'comments': timesheet_entry.comments,
                'billable': '',
                'submit_ts': '',
            },
        )
        echo(
            'green',
            'Added timesheet entry for {:%a%d/%m/%Y}'.format(timesheet_entry.date),
        )
        echo('yellow', timesheet_entry)


@synthetic.command('store')
def store_missing_timesheets():
    """
    Reads timesheet markdown files and creates timesheets for
    days without them.
    """
    session = get_session()

    timesheets = get_timesheets(session)
    last_approved_timesheet = sorted(
        [timesheet for timesheet in timesheets if timesheet.status == 'Approved'],
        key=lambda t: datetime.strptime(t.week, '%d/%m/%Y'),
    )[-1]
    timesheet_entries = get_timesheet_entries(session, last_approved_timesheet)

    last_approved_date = datetime.strptime(timesheet_entries[-1].date, '%d/%m/%Y')
    yesterday = datetime.now() + relativedelta(days=-1)

    # https://stackoverflow.com/a/11550426
    missing_days = list(
        rrule(
            DAILY,
            dtstart=last_approved_date + relativedelta(days=1),
            until=yesterday,
            byweekday=(MO, TU, WE, TH, FR),
        )
    )

    log.debug(
        f'last_approved_date: {last_approved_date}\n'
        f'from: {last_approved_date + relativedelta(days=-1)}\n'
        f'to yesterday: {yesterday}\n'
        f'missing_days: {missing_days}\n'
    )

    missing_timesheet_entries = []
    for missing_day in missing_days:
        missing_timesheet_entries.extend(
            ensure_references(
                timesheet_from_standup(missing_day), get_references(session)
            )
        )

    store_timesheets(session, missing_timesheet_entries)


def confirm_timesheet(session, timesheet):
    confirm_timesheet_url = '{}{}'.format(
        NATURAL_HR, timesheet.link('timesheet-confirm')
    )

    natural_api_post(
        session,
        confirm_timesheet_url,
        {
            'wb': timesheet.week,
            # todo: timesheet.hours => minutes
            'weekTotal': '144000',
            'check': '1',
            'emp_comments': '',
            'submit': '',
        },
    )
    echo('green', 'Confirmed timesheet for {}'.format(timesheet.week))


@synthetic.command('confirm')
def confirm_draft_timesheets():
    """Submits draft timesheets for approvals."""
    session = get_session()
    # https://stackoverflow.com/questions/4934783/using-python-2-6-how-do-i-get-the-day-of-the-month-as-an-integer
    beginning_of_the_month = datetime.now().day == 1
    draft_timesheets = [
        timesheet
        for timesheet in get_timesheets(session)
        if timesheet.status == 'Draft'
        and (timesheet.hours == '40h 0m' or beginning_of_the_month)
    ]

    for timesheet in draft_timesheets:
        echo('blue', '{week} {status} {hours}'.format(**attr.asdict(timesheet)))
        echo('yellow', timesheet)
        confirm_timesheet(session, timesheet)


@synthetic.command()
def show_time_off():
    """List time off requests"""
    session = get_session()

    time_off = []
    for to in natural_api(session, f'{NATURAL_HR}/hr/self-service/time-off').html.xpath(
        '//tr'
    )[2:]:
        parts = to.text.split()
        # is_leave = any('Emergency' in part for part in parts)
        is_wfh = any('Working' in part for part in parts)
        declined = any('Declined' in part for part in parts)
        if declined:
            date_status_parts = parts[-7:]
        else:
            date_status_parts = parts[-6:]

        time_off.append(
            {
                'leave_type': 'WFH' if is_wfh else 'Leave',
                'start_date': datetime.strptime(
                    date_status_parts[0], '%d/%m/%Y'
                ).strftime('%Y-%m-%d'),
                'end_date': datetime.strptime(
                    date_status_parts[1], '%d/%m/%Y'
                ).strftime('%Y-%m-%d'),
                'number_of_days': date_status_parts[2],
                'approved': date_status_parts[4] if not declined else 'Declined',
                'state': date_status_parts[5] if not declined else '',
            }
        )

    print(to_ascii_table(sorted(time_off, key=lambda t: t['start_date'])[::-1]))


@synthetic.command()
@click.argument('leave_type', type=click.Choice(['Leave', 'WFH']))
@click.argument('start_date', type=click.DateTime())
@click.argument('end_date', type=click.DateTime())
def request(leave_type, start_date, end_date):
    """Request leave or WFH"""
    session = get_session()
    emp_id = None
    for field in natural_api(
        session, f'{NATURAL_HR}/hr/self-service/time-off-add'
    ).html.xpath('//input'):
        if 'name' in field.attrs and field.attrs['name'] == 'emp_id':
            emp_id = field.attrs['value']
            break

    if not emp_id:
        log.error('No employee id field found')
        raise click.Abort

    leave_request = {
        'time_off_type': 'Home Emergency'
        if leave_type == 'Leave'
        else 'Working From Home'
        if leave_type == 'WFH'
        else None,
        'emp_id': emp_id,
        'comments': 'Annual Leave' if leave_type == 'Leave' else '',
        'start_date': start_date.strftime('%d/%m/%Y'),
        'end_date': end_date.strftime('%d/%m/%Y'),
        'duration': str(
            networkdays(
                start_date.date(),
                end_date.date(),
                holidays=holidays.SouthAfrica(years=start_date.year),
            )
        ),
        'submit': '',
    }

    natural_api_post(
        session, f'{NATURAL_HR}/hr/self-service/time-off-add', leave_request
    )


@synthetic.command()
def approve():
    """Approve timesheet and wfh requests"""
    session = get_session()

    workflow_view = natural_api(session, f'{NATURAL_HR}/hr/workflow-view').html.xpath(
        '//div[@class="content"]//div[@class="media-body"]'
    )
    to_be_approved = []
    wfh_requests = []
    for workflow_item in workflow_view:
        approval_link = workflow_item.links.pop()
        log.debug(approval_link)
        hidden_fields = natural_api(session, f'{NATURAL_HR}{approval_link}').html.xpath(
            '//input[@type="hidden"]'
        )

        log.debug(workflow_item.text)
        item_parts = workflow_item.text.split()
        log.debug(item_parts)
        if len(item_parts) == 5:
            name, surname, _, _, week = item_parts

            employee_timesheet = {
                field.attrs['name']: field.attrs['value']
                for field in hidden_fields
                if 'name' in field.attrs
            }
            for field in ['emp_comments', 'mgr_comments', 'approve']:
                employee_timesheet[field] = ''

            to_be_approved.append(
                dict(
                    name=f'{name} {surname}',
                    week=week,
                    hours=int(employee_timesheet['weekTotal']) / 60 / 60,
                    link=approval_link,
                    payload=employee_timesheet,
                )
            )
        else:
            name, surname = item_parts[:2]
            wfh_date = item_parts[6]

            all_fields = natural_api(
                session, f'{NATURAL_HR}{approval_link}'
            ).html.xpath('//input')
            wfh_request = {
                field.attrs['name']: field.attrs['value']
                for field in all_fields
                if 'name' in field.attrs
            }

            for field in all_fields:
                if field.attrs['type'] == 'radio' and 'checked' in field.attrs:
                    wfh_request[field.attrs['name']] = field.attrs['value']

            for field in ['comments', 'mgr_comments', 'approve']:
                wfh_request[field] = ''
            log.info(wfh_request)
            wfh_requests.append(
                dict(
                    name=f'{name} {surname}',
                    wfh_date=wfh_date,
                    link=approval_link,
                    payload=wfh_request,
                )
            )

    wfh_requests and print(
        to_ascii_table(
            [dict(name=wfh['name'], wfh_date=wfh['wfh_date']) for wfh in wfh_requests]
        )
    )
    for wfh in wfh_requests:
        if click.confirm(f'✅ WFH for {wfh["name"]} {wfh["wfh_date"]}️'):
            print(f'{NATURAL_HR}{wfh["link"]}')
            print(wfh['payload'])
            natural_api_post(session, f'{NATURAL_HR}{wfh["link"]}', wfh['payload'])

    if to_be_approved:
        print(
            to_ascii_table(
                [
                    dict(
                        name=timesheet['name'],
                        week=timesheet['week'],
                        hours=timesheet['hours'],
                    )
                    for timesheet in to_be_approved
                ]
            )
        )
        for timesheet in to_be_approved:
            if click.confirm(f'✅ {timesheet["name"]} {timesheet["week"]}️'):
                natural_api_post(
                    session, f'{NATURAL_HR}{timesheet["link"]}', timesheet['payload']
                )
