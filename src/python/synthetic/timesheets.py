import json
import os
import sys
from datetime import datetime
from pathlib import Path

import attr
import click
import requests
import requests_cache

import browsercookie
from dateutil.relativedelta import relativedelta
from dateutil.rrule import DAILY, FR, MO, TH, TU, WE, rrule
from requests_html import HTMLSession
from terminaltables import AsciiTable

requests_cache.install_cache()

# FIXME: envvar
STANDUP_PATH = Path.home().joinpath('Work/standups')
NATURAL_HR = 'https://www.naturalhr.net'
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


def get_browser_session_cookie():
    natural_hr_cookie = None
    for cookie in browsercookie.chrome():
        if 'naturalhr' in cookie.domain and 'PHP' in cookie.name:
            natural_hr_cookie = cookie
    return natural_hr_cookie


def valid_session_cookie(session_cookie):
    if not session_cookie:
        return False

    # https://stackoverflow.com/a/1453013
    home_page = '{}/hr/'.format(NATURAL_HR)
    url_headers = {'Origin': home_page, 'Referer': home_page}
    session_cookie = {session_cookie.name: session_cookie.value}

    response = requests.get(
        home_page,
        headers=dict(HEADERS, **url_headers),
        cookies=dict(COOKIES, **session_cookie),
    )
    return response.status_code == 200


def get_session(cookie=None):
    session_cookie = get_browser_session_cookie() if not cookie else cookie

    if not valid_session_cookie(session_cookie):
        echo(
            'red',
            'Session cookie {} is invalid, please log in to Natural HR'.format(
                session_cookie
            ),
        )
        sys.exit(-1)

    session = HTMLSession(mock_browser=True)
    session_cookie = {session_cookie.name: session_cookie.value}
    session.cookies = requests.cookies.cookiejar_from_dict(
        dict(COOKIES, **session_cookie)
    )
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


@click.command()
def standup():
    today = datetime.now()
    echo('yellow', 'TODO: text-standup {}'.format(today))


@click.command()
def list_timesheets():
    session = get_session()

    last_months_timesheets = sorted(
        get_timesheets(session),
        key=lambda timesheet: datetime.strptime(timesheet.week, '%d/%m/%Y'),
        reverse=True,
    )[:4]

    for timesheet in last_months_timesheets:
        echo('blue', '{week} {status} {hours}'.format(**attr.asdict(timesheet)))
        timesheet_entries = get_timesheet_entries(session, timesheet)
        print(
            to_ascii_table(
                [attr.asdict(timesheet_entry) for timesheet_entry in timesheet_entries]
            )
        )


@click.command()
def confirm_draft_timesheets():
    session = get_session()
    draft_timesheets = [
        timesheet
        for timesheet in get_timesheets(session)
        if timesheet.status == 'Draft' and timesheet.hours == '40h 0m'
    ]

    for timesheet in draft_timesheets:
        echo('blue', '{week} {status} {hours}'.format(**attr.asdict(timesheet)))
        echo('yellow', timesheet)
        confirm_timesheet(session, timesheet)


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


@click.command()
def store_missing_timesheets():
    session = get_session()

    timesheets = get_timesheets(session)

    last_approved_week = sorted(
        [
            datetime.strptime(timesheet.week, '%d/%m/%Y')
            for timesheet in timesheets
            if timesheet.status == 'Approved'
        ]
    )[-1]
    unapproved_start_week = last_approved_week + relativedelta(weekday=MO, weeks=+1)
    yesterday = datetime.now() + relativedelta(days=-1)

    # https://stackoverflow.com/a/11550426
    missing_days = list(
        rrule(
            DAILY,
            dtstart=unapproved_start_week,
            until=yesterday,
            byweekday=(MO, TU, WE, TH, FR),
        )
    )

    missing_timesheet_entries = []
    for missing_day in missing_days:
        missing_timesheet_entries.extend(
            ensure_references(
                timesheet_from_standup(missing_day), get_references(session)
            )
        )

    store_timesheets(session, missing_timesheet_entries)


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


last_choice = None
DEFAULT_REFERENCES = ['Quidco BAU', 'V3 BAU', 'R5Bet7 – Emarsys sync (URT)']


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
