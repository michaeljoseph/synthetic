import json
import logging
import os
import re
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
from functools import singledispatch
from pathlib import Path
from pprint import pprint
from typing import List

import attr
import click
import coloredlogs
import dateparser
import inflect
import mistune
from dateutil.relativedelta import relativedelta
from dateutil.tz import tzlocal
from durations import Duration
from plumbum.cmd import git
from requests_cache import CachedSession
from requests_toolbelt.sessions import BaseUrlSession
from slacker import Slacker
from terminaltables import AsciiTable

log = logging.getLogger(__name__)
# 4h, 15m
DURATION_REGEX = re.compile(r'(?P<duration>[0-9]+[hm]+)')
# QCO-9795
JIRA_REF_REGEX = re.compile(r'(?P<jira_ref>[A-Z]+-[0-9]+)')
# transaction-rules-engine#4
PULL_REQUEST_REGEX = re.compile(r'(?P<repo_name>[a-z-]+)#(?P<pr_id>[0-9]+)')
WORKING_HOURS = dict(start='10am', end='6pm')


def local_iso(dt: datetime):
    return dt.astimezone(tzlocal()).isoformat()


class TogglSession(BaseUrlSession, CachedSession):
    def __init__(self, token):
        super().__init__(base_url='https://www.toggl.com/api/v8/')
        self.auth = (token, 'api_token')

    def projects(self):
        workspace_id = self.get('workspaces').json()[0]['id']
        return [
            Project(id=project['id'], name=project['name'])
            for project in self.get(f'workspaces/{workspace_id}/projects').json()
        ]

    def get_project(self, project_name):
        projects = [
            project for project in self.projects() if project.name == project_name
        ]
        if projects:
            return projects[0]
        return None


class JiraSession(BaseUrlSession, CachedSession):
    def __init__(self, user, token):
        super().__init__(base_url='https://quidco.atlassian.net/rest/api/latest/')
        self.auth = (user, token)


class BitbucketSession(BaseUrlSession, CachedSession):
    def __init__(self, user, token):
        super().__init__(base_url='https://api.bitbucket.org/2.0/')
        self.auth = (user, token)


# https://hynek.me/articles/serialization
@singledispatch
def to_serializable(val):
    """Used by default."""
    return str(val)


@to_serializable.register(datetime)
def ts_datetime(val):
    """Used if *val* is an instance of datetime."""
    return (
        val.replace(tzinfo=timezone.utc).astimezone().replace(microsecond=0).isoformat()
    )


@attr.s(auto_attribs=True)
class Project:
    id: int
    name: str


@attr.s(auto_attribs=True)
class ListTimeEntry:
    at: str
    billable: bool
    description: str
    duration: int
    duronly: bool
    guid: str
    id: int
    pid: int
    start: str
    stop: str
    uid: int
    wid: int

    @property
    def payload(self):
        return dict(
            id=self.id,
            description=self.description,
            start=dateparser.parse(self.start).isoformat(),
            duration=self.duration,
        )


@attr.s(auto_attribs=True)
class CreateTimeEntry:
    pid: int
    jira_ref: str
    description: str
    duration: int
    start: str
    created_with: str = '🤖synthetic'

    @classmethod
    def from_note(cls, project_id: int, start: datetime, note):
        return cls(
            pid=project_id,
            jira_ref=note.ticket.ref if note.ticket else '',
            description=note.description,
            duration=Duration(note.duration).to_seconds(),
            start=start,
        )

    @property
    def json(self):
        return json.dumps(self.payload, default=to_serializable)

    @property
    def payload(self):
        return dict(
            time_entry=dict(
                pid=self.pid,
                description=f'{self.description}'
                if self.jira_ref
                else self.description,
                duration=self.duration,
                start=self.start.isoformat(),
                created_with=self.created_with,
            )
        )


@attr.s(auto_attribs=True)
class Standup:
    date: str
    friday: List[str] = []
    yesterday: List[str] = []
    today: List[str] = []
    blockers: List[str] = []

    @classmethod
    def from_markdown(cls, markdown):
        parsed_markdown = mistune.Markdown(renderer=mistune.AstRenderer())(markdown)
        current_heading = None
        categorised = defaultdict(list)

        for markdown_item in parsed_markdown:
            if markdown_item['type'] == 'heading':
                if markdown_item['level'] == 1:
                    categorised['date'] = markdown_item['children'][0]['text']
                    continue

                current_heading = markdown_item['children'][0]['text'].lower()
                categorised[current_heading] = []
                log.debug(current_heading)
            elif markdown_item['type'] == 'list':
                log.debug(markdown_item['children'])
                categorised[current_heading].extend(
                    [
                        ''.join([x.get('text', '') for x in item.get('children', {})])
                        for i in markdown_item['children']
                        for item in i.get('children', {})
                    ]
                )
            elif markdown_item['type'] == 'paragraph':
                categorised[current_heading].extend(
                    [y['text'] for y in markdown_item['children']]
                )
            elif markdown_item['type'] == 'thematic_break':
                break
            else:
                print('Ignoring', markdown_item)
        return Standup(**categorised)


@attr.s(auto_attribs=True)
class Ticket:
    ref: str
    link: str
    status: str
    title: str
    description: str

    @classmethod
    def from_ref(cls, jira, ref: str):
        ticket = jira.get(f'issue/{ref}').json()
        return cls(
            ref=ref,
            link=f'https://quidco.atlassian.net/browse/{ref}',
            status=ticket['fields']['status']['name'],
            title=ticket['fields']['summary'],
            description=ticket['fields'].get('description') or '',
        )


@attr.s(auto_attribs=True)
class PullRequest:
    repo_name: str
    pr_id: int
    link: str
    title: str
    state: str
    approvals: int
    comments: int

    @classmethod
    def from_ref(cls, bitbucket, repo_name: str, pr_id: int):
        old_org = ['quidco-web-app', 'quidco-packages']
        org = 'john_pervanas' if repo_name in old_org else 'maplesyrupgroup'
        # TODO: gitlab
        pull_request = bitbucket.get(
            f'repositories/{org}/{repo_name}/pullrequests/{pr_id}'
        ).json()
        return cls(
            repo_name=repo_name,
            pr_id=pr_id,
            link=pull_request['links']['html']['href'],
            title=pull_request['title'],
            state=pull_request['state'],
            approvals=sum(
                [person['approved'] for person in pull_request['participants']]
            ),
            comments=int(pull_request['comment_count']),
        )


@attr.s(auto_attribs=True)
class Note:  # => TimeEntry => ListTimeEntry 🤷‍♂️
    text: str
    duration: str = None
    ticket: Ticket = None

    @classmethod
    def from_text(cls, jira, bitbucket, entry_text: str):
        """
        - QWA Release Manager 1h
        - QCO-9452 rebuild event sourcing on kinesis 7h
        - QCO-9452 continue to rebuild event sourcing
        - TECH-548 TECH-562 TECH-561 🚀 merged kraken#9, kraken#11, kraken#12 4h
        # TODO: a note has tickets and pullrequests
        """
        ticket = None
        has_jira_ref = JIRA_REF_REGEX.search(entry_text)
        if has_jira_ref:
            jira_ref = has_jira_ref.groupdict()['jira_ref'].strip()
            ticket = Ticket.from_ref(jira, jira_ref)
            entry_text = entry_text.replace(jira_ref, '')
            log.debug(ticket)

        # TODO: gitlab
        # pull_request = None
        # has_pr_ref = PULL_REQUEST_REGEX.search(entry_text)
        # if has_pr_ref:
        #     has_pr_ref = has_pr_ref.groupdict()
        #     pull_request = PullRequest.from_ref(
        #         bitbucket, has_pr_ref['repo_name'], int(has_pr_ref['pr_id'])
        #     )

        duration = None
        has_duration = DURATION_REGEX.search(entry_text)
        if has_duration:
            duration = has_duration.groupdict()['duration']
            entry_text = entry_text.replace(duration, '')

        return cls(text=entry_text.strip(), ticket=ticket, duration=duration)

    @property
    def description(self):
        return f'{self.ticket.ref} {self.text}' if self.ticket else self.text


@click.option('--debug', help='Enables debug logging.', is_flag=True, default=False)
@click.option('-c', '--no-cache', help='Ignore the cache.', is_flag=True, default=False)
@click.group(context_settings=dict(help_option_names=[u'-h', u'--help']))
@click.pass_context
def cli(ctx, debug: bool, no_cache: bool):
    """Synthetic timesheets and approvals for toggl.com"""
    coloredlogs.install(
        fmt='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        level=logging.DEBUG if debug else logging.INFO,
    )
    if not ctx.obj:
        ctx.obj = namedtuple(
            'Settings', ['toggl', 'slack', 'jira', 'bitbucket', 'standup_home']
        )(
            standup_home=os.environ.get(
                'STANDUP_HOME', Path.home().joinpath('Work/standups')
            ),
            toggl=TogglSession(os.environ['TOGGL_TOKEN']),
            slack=Slacker(os.environ['SLACK_TOKEN']),
            jira=JiraSession(os.environ['JIRA_USER'], os.environ['JIRA_TOKEN']),
            bitbucket=BitbucketSession(
                os.environ['BITBUCKET_USER'], os.environ['BITBUCKET_TOKEN']
            ),
        )
        if no_cache:
            ctx.obj.jira._is_cache_disabled = (
                ctx.obj.bitbucket._is_cache_disabled
            ) = True


def to_ascii_table(data, fields=None):
    print(len(data))
    first_element = data[0]
    if hasattr(first_element, 'keys'):
        headings = [key for key in first_element.keys()]
    else:
        headings = list(attr.asdict(first_element).keys())

    log.debug(headings)
    if hasattr(first_element, 'items'):
        data = [value for timesheet in data for key, value in timesheet.items()]
    else:
        data = [
            value for timesheet in data for value in attr.asdict(timesheet).values()
        ]
    log.debug(data)
    return AsciiTable([headings] + [data]).table


@cli.command('list')
@click.pass_obj
def list_timesheets(settings):
    # TODO: start and end args?
    query_params = {
        'start_date': local_iso(datetime.now() + relativedelta(days=-14)),
        'end_date': local_iso(datetime.now()),
    }
    entries = settings.toggl.get('time_entries', params=query_params).json()

    time_entries = [ListTimeEntry(**time_entry) for time_entry in entries]

    print(to_ascii_table([time_entry.payload for time_entry in time_entries]))
    print(to_ascii_table(time_entries))

    # TODO: figure out when no date provided and then loop through missing?
    # print('; '.join([
    #     'synthetic store {:%Y-%m-%d}'.format(x + relativedelta(days=1))
    #     for x in get_missing_days(time_entries)
    # ]))


# TODO: wrap slacker
def slack_user_id_by_email(slack, email):
    users = slack.users.list().body['members']
    users = [u for u in users if u['profile'].get('email') == email]

    user_id = users[0]['id'] if users else None
    # slack_avatar_url=users[0]['profile']['image_72']
    return user_id


@cli.command('slack')
@click.argument(
    'standup-date',
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=f'{datetime.now():%Y-%m-%d}',
)
@click.option(
    '--channel-name', default=git['config', 'user.email']().strip(), show_default=True
)
@click.pass_obj
def slack_post(settings, standup_date, channel_name):
    settings.jira._is_cache_disabled = settings.bitbucket._is_cache_disabled = True

    markdown_standup_path = Path(settings.standup_home).joinpath(
        f'{standup_date:%Y-%m-%d}.md'
    )
    standup = Standup.from_markdown(markdown_standup_path.read_text())
    log.info(standup)

    target = (
        slack_user_id_by_email(settings.slack, channel_name)
        if '@' in channel_name
        else channel_name
    )

    # TODO: giphy api
    # images = dict(
    #     cat_stand='https://media.giphy.com/media/ACVoiOEjbA6nC/giphy.gif',
    #     standup='https://media.giphy.com/media/RJVUqXW7x4YGHye0Fk/giphy.gif',
    # )

    blocks = [
        # dict(
        #     type='image',
        #     title=dict(type='plain_text', text=f':calendar: {standup_date:%Y-%m-%d} :loudspeaker:', emoji=True),
        #     image_url=random.choice(list(images.values())),
        #     alt_text='standup'
        # ),
        dict(
            type='section',
            text=dict(
                type='mrkdwn', text=f':calendar: {standup_date:%Y-%m-%d} :loudspeaker:'
            ),
        )
    ]
    emoji_map = {
        'Yesterday': ':newspaper:',
        'Today': ':male-technologist:',
        'Blockers': ':man-raising-hand:',
    }
    blocks.append(dict(type='divider'))
    for section, items in zip(
        ['Yesterday', 'Today', 'Blockers'],  # , 'Reviews'
        [standup.yesterday, standup.today, standup.blockers],
    ):
        blocks.append(
            dict(
                type='section',
                text=dict(type='mrkdwn', text=f'{emoji_map[section]} *{section}*'),
            )
        )

        notes = [
            Note.from_text(settings.jira, settings.bitbucket, item) for item in items
        ]
        log.debug(f'Notes: {notes}')

        if not notes:
            continue

        notes_as_list = '\n'.join(
            [
                f':{inflect.engine().number_to_words(idx+1)}: `{note.description}`'
                for idx, note in enumerate(notes)
                if note.description
            ]
        )
        log.debug(notes_as_list)

        blocks.append(
            dict(type='section', text=dict(type='mrkdwn', text=notes_as_list))
        )

        tickets = [
            Ticket.from_ref(settings.jira, jira_ref)
            for note in notes
            for jira_ref in JIRA_REF_REGEX.findall(note.description)
        ]
        context = [
            dict(
                type='mrkdwn',
                text=f':ticket: {ticket.link} {ticket.title} [*{ticket.status}*] ',
            )
            for ticket in tickets
        ]

        # TODO: PRs
        pull_requests = [
            PullRequest.from_ref(settings.bitbucket, repo_name, int(pr_id))
            for note in notes
            for repo_name, pr_id in PULL_REQUEST_REGEX.findall(note.description)
        ]
        context.extend(
            [
                dict(
                    type='mrkdwn',
                    # text=f':construction: {pr.link} {pr.title} [*{pr.state}*] :thumbsup: {pr.approvals} :speaking_head_in_silhouette: {pr.comments}',
                    text=f':construction: {pr.link} :thumbsup: {pr.approvals} :speaking_head_in_silhouette: {pr.comments}\n{pr.title} [*{pr.state}*]',
                )
                for pr in pull_requests
            ]
        )

        if context:
            blocks.append(dict(type='context', elements=context))

        if not notes:
            blocks.append(dict(type='section', text=dict(type='mrkdwn', text='*None*')))

        blocks.append(dict(type='divider'))

    # TODO: consolemd.Renderer().render()
    log.debug(blocks)

    if click.confirm(f'Post this standup note to {target}'):
        response = settings.slack.chat.post_message(
            target,
            text=f'Standup Post {standup_date:%Y-%m-%d}',
            # https://api.slack.com/methods/chat.postMessage#arg_blocks
            blocks=json.dumps(blocks),
            as_user=True,
        )
        log.debug(response)


@cli.command('store')
@click.argument(
    'standup_date',
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=f'{datetime.now():%Y-%m-%d}',
)
@click.pass_obj
def store_timesheets(settings, standup_date):
    # standup contains entries for the day before standup_date
    standup = Standup.from_markdown(
        Path(settings.standup_home).joinpath(f'{standup_date:%Y-%m-%d}.md').read_text()
    )
    log.info(standup)

    timesheet_date = standup_date + relativedelta(
        # monday has lasts friday's times
        days=-3 if standup_date.weekday() == 0 else -1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    params = dict(
        start_date=local_iso(timesheet_date),
        end_date=local_iso(timesheet_date + timedelta(days=1)),
    )
    time_entries = [
        ListTimeEntry(**time_entry)
        for time_entry in settings.toggl.get('time_entries', params=params).json()
    ]

    start = dateparser.parse(
        f'{timesheet_date:%Y-%m-%d} {WORKING_HOURS["start"]}'
    ).astimezone(tzlocal())
    log.debug(f'{timesheet_date} => {start}')
    # TODO: prompt? OR list projects ?? how to reference in standup report?

    for entry_text in standup.yesterday:
        if not entry_text:
            continue
        note = Note.from_text(settings.jira, settings.bitbucket, entry_text)
        if not note.duration:
            raise Exception(f'Missing duration in "{entry_text}"')

        project_name = (
            'Holiday'
            if note.description in ['Annual Leave', 'Public Holiday']
            else 'BAU - Q Platform'
        )

        entry = CreateTimeEntry.from_note(
            project_id=settings.toggl.get_project(project_name).id,
            start=start,
            note=note,
        )

        log.info(entry)
        description = entry.payload['time_entry']['description']
        if description in [t.description for t in time_entries]:
            log.info('Duplicate entry, skipping')
            continue

        if click.confirm('Add this time entry'):
            response = settings.toggl.post('time_entries', json=entry.payload).json()
            pprint(response)
            start += relativedelta(seconds=+entry.duration)
