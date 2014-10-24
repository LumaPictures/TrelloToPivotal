#!/usr/bin/env python
#encoding: utf-8
import csv
import datetime
import json
import logging
import os
import re
import sys

import argparse

# It is assumed that the member names are equal in trello and pivotal tracker.

NAME_TAG_RES = [
    r'^(?P<tags>.+?): (?P<name>.+)',
    r'^\[(?P<tags>.+?)\]:? (?P<name>.+)'
]

# Maps a list to state of the item.
# Lists not listed here ends up in the icebox.
LIST_STATES = {
    # states: unscheduled, unstarted, started, finished, delivered, accepted, rejected
    'Epics / Triage': 'epic',
    'Backlog': 'unstarted',
    'Next Up': 'unstarted',
    'In Progress Now': 'started',
    'Blocked': 'started',
    'Feedback': 'delivered',
    'Resolved': 'accepted',
    'Unresolved': 'accepted',
    'Closed': 'accepted'

    # 'Todo':'unstarted', # Backlog
    # 'InProgess':'started', 
    # 'Testing':'finished',
    # 'Completed':'accepted',
}

# Anything that has started needs an estimate.
# The default is None, unestimated.
LIST_ESTIMATES = { 
    # 'InProgess': 2,
    # 'Testing': 2,
    # 'Completed': 2,
}

LABEL_ESTIMATES = {
    'Short': 1,
    'Medium': 2
}

# Checkbox states
CHECKBOX_STATES = {
    'incomplete': 'not completed',
    'complete': 'completed'
}

logger = logging.getLogger(__name__)


def sluggify(string):
    return re.sub("[^a-zA-Z0-9 _-]",'', string.lower()).replace(' ', '-')


def paginate(iterable, num):
    return [iterable[i * num : (i + 1) * num] \
            for i in range((len(iterable) / num) + 1) \
            if iterable[i * num : (i + 1) * num]]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Transforms an exported Trello CSV to a Pivotal Tracker formatted CSV")
    parser.add_argument('-d', '--debug', action='store_true', help="Debug mode")
    parser.add_argument('-i', '--in-file', required=True, metavar='csv_file', help="Input file (Trello CSV)")
    parser.add_argument('-o', '--out-path', required=True, metavar='directory', help="Output directory where Pivotal CSVs are placed (will be created if it doesn't exist)")
    parser.add_argument('--story-limit', type=int, default=500, metavar='limit', help="Limit number of stories output per CSV file (default: 500)")
    parser.add_argument('--default-estimate', type=int, default=0, metavar='points', help="Default estimate (0-3)")
    parser.add_argument('--ignore-archived', action='store_true', help="Ignore archived cards")
    arguments = vars(parser.parse_args())

    logging.basicConfig(level=logging.DEBUG if arguments['debug'] else logging.INFO)

    # Read input file
    input_file = arguments['in_file']
    with file(input_file) as f:
        lists = {}
        listorders = {}
        board = json.loads(f.read())
        for list in board['lists']:
            lists[list['id']] = list['name']
            listorders[list['id']] = list['pos']

    # Build member lookup
    members = {}
    for member in board['members']:
        members[member['id']] = member['fullName']

    # Create out directory
    now = datetime.datetime.now().strftime("%Y-%m-%d+%H:%M")
    output_directory = arguments['out_path']
    if not os.path.exists(output_directory):
        os.mkdir(output_directory)

    # Upper bound of all task counts (used for CSV task column count)
    max_num_tasks = 0
    for card in board['cards']:
        num_tasks = 0
        if 'checklists' in card:
            for checklist in card['checklists']:
                num_tasks += len(checklist['checkItems'])
        max_num_tasks += num_tasks

    # Upper bound of all comment counts (used for CSV comment column count)
    max_comments = 0
    for card in board['cards']:
        if 'comments' in card:
            max_comments = max(max_comments, card['comments'])

    # Sort cards
    all_cards = board['cards']
    all_cards.sort(key=lambda x: (-float(listorders[x['idList']]), float(x['pos'])))

    # Iterate cards
    story_limit = arguments['story_limit']
    for page, cards in enumerate(paginate(all_cards, story_limit)):
        filename = "%s/%s_%s_%s.csv" % (output_directory, sluggify(board['name']), now, page)
        with open(filename, 'wb') as csvfile:
            writer = csv.writer(csvfile, delimiter=',')
            writer.writerow(['Title', 'Description', 'Owned By', 'Requested By', 'Labels',
                             'Current State', 'Type', 'Estimate']
                            + ['Task', 'Task Status'] * max_num_tasks
                            + ['Comment'] * max_comments)

            for card in cards:
                list_name = lists[card['idList']]
                name = card['name'].encode("utf-8")

                # Determine state and story type
                current_state = LIST_STATES.get(list_name, 'unscheduled')
                if current_state == 'epic':
                    current_state = 'unscheduled'
                    story_type = 'epic'
                else:
                    story_type = 'feature'

                labels = [label['name'] for label in card['labels']]

                # Strip Scrum-for-Trello estimates from title (unused)
                matcher = re.search('^\((\d+?)\) (.+)', name)
                if matcher:
                    name = matcher.group(2)
                else:
                    # Find special naming in titles
                    for regex in NAME_TAG_RES:
                        matcher = re.match(regex, name)
                        if matcher:
                            groups = matcher.groupdict()
                            name = groups.get('name', name)
                            tags = groups.get('tags')
                            if tags is not None:
                                # Remove old task IDs from list of labels
                                labels += filter(lambda x: str(int(x)) != x, map(lambda x: x.strip(), tags.split('/')))
                            break

                # Determine estimate
                estimate = LIST_ESTIMATES.get(list_name, None)
                if story_type == 'epic':
                    estimate = None
                elif (story_type == 'feature' and current_state in ('unstarted', 'unscheduled')):
                    estimate = None
                else:
                    for label_name, label_estimate in LABEL_ESTIMATES.items():
                        if label_name in labels:
                            estimate = label_estimate
                            break
                    if estimate is None:
                        estimate = arguments['default_estimate']

                # Add other lists as label instead
                if not list_name in LIST_STATES:
                    labels += [list_name]

                orig_description = card.get('desc', '')
                description = orig_description + '\n' if orig_description else ''

                card_members = card['idMembers']
                owner = members[card_members[0]] if card_members else ''
                if 'checkItemStates' in card:
                    checkItemStates = {item['idCheckItem']: item['state'] for item in card['checkItemStates']}

                tasks = []
                if 'checklists' in card:
                    for checklist in card['checklists']:
                        pre = checklist['name'] + ': ' if checklist['name'] != "Checklist" else ''
                        for item in checklist['checkItems']:
                            tasks += [(pre + item['name']).encode('utf-8')]
                            tasks += [CHECKBOX_STATES[checkItemStates.get(item['id'], item['state'])]]

                row = [name,
                       (description + "Imported from %s" % card['url']).encode("utf-8"),
                       owner.encode("utf-8"),
                       owner.encode("utf-8"),
                       ','.join(labels),
                       current_state,
                       story_type,
                       estimate] + tasks

                logger.debug('Card %s "%s" imported from %s as %s %s with %d points' % (
                             card['id'], name, list_name, current_state, story_type, estimate if estimate is not None else -1))
                #logger.debug(row)
                writer.writerow(row)
