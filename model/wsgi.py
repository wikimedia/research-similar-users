from datetime import datetime, timedelta
import os

from flask import Flask, request, jsonify, render_template
from flask_basicauth import BasicAuth
from flask_cors import CORS
import mwapi
from sklearn.metrics.pairwise import cosine_similarity
import yaml

app = Flask(__name__)

__dir__ = os.path.dirname(__file__)

# load in app user-agent or any other app config
app.config.update(
    yaml.safe_load(open(os.path.join(__dir__, 'flask_config.yaml'))))

basic_auth = BasicAuth(app)

# Enable CORS for API endpoints
cors = CORS(app, resources={r'/api/*': {'origins': '*'}})

# Testing
# Local: http://127.0.0.1:5000/similarusers?usertext=Ziyingjiang
# VPS: https://spd-test.wmcloud.org/similarusers?usertext=Bttowadch&k=50

# Data dictionaries -- TODO: move to sqllitedict or something equivalent
# Currently used for both READ and WRITE though
USER_METADATA = {}  # is_anon; num_edits; num_pages; most_recent_edit; oldest_edit
COEDIT_DATA = {}
TEMPORAL_DATA = {}

DEFAULT_K = 50
TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
READABLE_TIME_FORMAT = '%Y-%m-%d %H:%M:%S UTC'
URL_PREFIX = 'https://spd-test.wmcloud.org/similarusers'
EDITORINTERACT_URL = 'https://sigma.toolforge.org/editorinteract.py?users={0}&users={1}&users=&startdate=&enddate=&ns=&server=enwiki&allusers=on'
INTERACTIONTIMELINE_URL = 'https://interaction-timeline.toolforge.org/?wiki=enwiki&user={0}&user={1}'

@app.route('/')
@basic_auth.required
def index():
    """Simple UI for querying API. Password-protected to reduce chance of accidental discovery / abuse."""
    return render_template('index.html')

@app.route('/similarusers', methods=['GET'])
def get_similar_users():
    """For a given user, find the k-most-similar users based on edit overlap.

    Expected parameters:
    * usertext (str): username or IP address to query
    * k (int): how many similar users to return at maximum?
    * followup (bool): include additional tool links in API response for follow-up on data
    """
    user_text, num_similar, followup, error = validate_api_args()
    if error is not None:
        return jsonify({'Error': error})
    else:
        edits = get_additional_edits(user_text, last_edit_timestamp=USER_METADATA[user_text]['most_recent_edit'])
        if edits is not None:
            update_coedit_data(user_text, edits, app.config['EDIT_WINDOW'])
        overlapping_users = COEDIT_DATA.get(user_text, [])[:num_similar]
        result = {'user_text':user_text,
                  'num_edits_in_data':USER_METADATA[user_text]['num_edits'],
                  'first_edit_in_data':datetime.strptime(USER_METADATA[user_text]['oldest_edit'], TIME_FORMAT).strftime(READABLE_TIME_FORMAT),
                  'last_edit_in_data':datetime.strptime(USER_METADATA[user_text]['most_recent_edit'], TIME_FORMAT).strftime(READABLE_TIME_FORMAT),
                  'results': [build_result(user_text, u[0], u[1], num_similar, followup) for u in overlapping_users]}
        return jsonify(result)

def build_result(user_text, neighbor, num_pages_overlapped, num_similar, followup):
    """Build a single similar-user API response"""
    r = {'user_text': neighbor,
         'num_edits_in_data': USER_METADATA.get(neighbor, {}).get('num_pages', num_pages_overlapped),
         'edit-overlap': num_pages_overlapped / USER_METADATA[user_text]['num_pages'],
         'edit-overlap-inv': min(1, num_pages_overlapped / USER_METADATA.get(neighbor, {}).get('num_pages', 1)),
         'day-overlap': get_temporal_overlap(user_text, neighbor, 'd'),
         'hour-overlap': get_temporal_overlap(user_text, neighbor, 'h')}
    if followup:
        r['follow-up'] = {
            'similar': '{0}?usertext={1}&k={2}'.format(URL_PREFIX, neighbor, num_similar),
            'editorinteract': EDITORINTERACT_URL.format(user_text, neighbor),
            'interaction-timeline': INTERACTIONTIMELINE_URL.format(user_text, neighbor)}
    return r


def get_temporal_overlap(u1, u2, k):
    """Determine how similar two users are in terms of days and hours in which they edit."""
    # overlap in days-of-week
    if k == 'd':
        cs = cosine_similarity([TEMPORAL_DATA.get(u1, {}).get('d', [0] * 7)],
                               [TEMPORAL_DATA.get(u2, {}).get('d', [0] * 7)])[0][0]
    # overlap in hours-of-the-day
    elif k == 'h':
        cs = cosine_similarity([TEMPORAL_DATA.get(u1, {}).get('h', [0] * 24)],
                               [TEMPORAL_DATA.get(u2, {}).get('h', [0] * 24)])[0][0]
    else:
        raise Exception("Do not recognize temporal overlap key -- must be 'd' for daily or 'h' for hourly.")
    # map cosine similarity values to qualitative labels
    # thresholds based on examining some examples and making judgments on how similar they seemed to be
    if cs == 1:
        return {'cos-sim':cs, 'level':'Same'}
    elif cs > 0.8:
        return {'cos-sim':cs, 'level':'High'}
    elif cs > 0.5:
        return {'cos-sim':cs, 'level':'Medium'}
    elif cs > 0:
        return {'cos-sim':cs, 'level':'Low'}
    else:
        return {'cos-sim':cs, 'level':'No overlap'}

def get_additional_edits(user_text, last_edit_timestamp=None, lang='en', limit=1000, session=None):
    """Gather edits made by a user since last data dumps -- e.g., October edits if dumps end of September dumps used."""
    if last_edit_timestamp:
        arvstart = datetime.strptime(last_edit_timestamp, TIME_FORMAT) + timedelta(seconds=1)
    else:
        arvstart = app.config['MOST_RECENT_REV_TS']
    if session is None:
        session = mwapi.Session('https://{0}.wikipedia.org'.format(lang), user_agent=app.config['CUSTOM_UA'])

    # generate list of all revisions since user's last recorded revision
    result = session.get(
        action="query",
        list="allrevisions",
        arvuser=user_text,
        arvprop='ids|timestamp|comment|user',
        arvnamespace="|".join([str(ns) for ns in app.config['NAMESPACES']]),
        arvstart=arvstart,
        arvdir='newer',
        format='json',
        arvlimit=500,
        formatversion=2,
        continuation=True
    )
    min_timestamp = USER_METADATA[user_text]['oldest_edit']
    max_timestamp = USER_METADATA[user_text]['most_recent_edit']
    new_edits = 0
    new_pages = 0
    try:
        pageids = {}
        for r in result:
            for page in r['query']['allrevisions']:
                pid = page['pageid']
                if pid not in pageids:
                    pageids[pid] = []
                    new_pages += 1
                for rev in page['revisions']:
                    ts = rev['timestamp']
                    pageids[pid].append(ts)
                    dtts = datetime.strptime(ts, TIME_FORMAT)
                    # update TEMPORAL_DATA so future calls don't have to repeat this
                    update_temporal_data(user_text, dtts.day, dtts.hour, 1)
                    new_edits += 1
                    if min_timestamp is None:
                        min_timestamp = ts
                        max_timestamp = ts
                    else:
                       max_timestamp = max(max_timestamp, ts)
                       min_timestamp = min(min_timestamp, ts)
            if len(pageids) > limit:
                break
        # Update USER_METADATA so future calls don't need to repeat this process
        USER_METADATA[user_text]['num_edits'] += new_edits
        # this is not ideal as these might not be new pages but too expensive to check and getting it wrong isn't so bad
        USER_METADATA[user_text]['num_pages'] += new_pages
        USER_METADATA[user_text]['most_recent_edit'] = max_timestamp
        USER_METADATA[user_text]['oldest_edit'] = min_timestamp
        return pageids
    except Exception:
        return None

def update_coedit_data(user_text, new_edits, k, lang='en', session=None, limit=250):
    """Get all new edits since dump ended on pages the user edited and overlapping users.

    NOTE: this is potentially very high latency for pages w/ many edits or if the editor edited many pages
    TODO: come up with a sampling strategy -- e.g., cap at 50
    ALT TODO: only do first k -- e.g., 50 -- but rewrite how additional edits are stored so can ensure that the next API call
    will get the next 50 without missing data.
    """
    most_similar_users = COEDIT_DATA[user_text]
    if session is None:
        session = mwapi.Session('https://{0}.wikipedia.org'.format(lang), user_agent=app.config['CUSTOM_UA'])

    overlapping_users = {}
    for pid in new_edits:
        # generate list of all revisions since user's last recorded revision
        result = session.get(
            action="query",
            prop="revisions",
            pageids=pid,
            rvprop='ids|timestamp|user',
            rvstart=app.config['MOST_RECENT_REV_TS'],
            rvdir='newer',
            format='json',
            rvlimit=500,
            formatversion=2,
            continuation=True
        )
        for r in result:
            revs = r['query']['pages'][0].get('revisions', [])
            user_edit_indices = [i for i,e in enumerate(revs) if e['user'] == user_text]
            for idx in user_edit_indices:
                for e in revs[max(0, idx-k):idx+k]:
                    if e['user'] == user_text:
                        continue
                    if e['user'] not in overlapping_users:
                        overlapping_users[e['user']] = set()
                    overlapping_users[e['user']].add(pid)

    # remove bots
    new_users = [u for u in overlapping_users if u not in USER_METADATA]
    for user_list in chunkify(new_users):
        result = session.get(
            action="query",
            list="users",
            ususers='|'.join(user_list),
            usprop='groups',
            format='json',
            formatversion=2
        )
        for u in result['query']['users']:
            if 'bot' in u.get('groups', []):
                overlapping_users.pop(u['name'])

    # update overlap list
    for i in range(len(most_similar_users)-1, -1, -1):
        ut = most_similar_users[i][0]
        overlap = most_similar_users[i][1]
        if ut in overlapping_users:
            new_pages = overlapping_users.pop(ut)
            overlap += len(new_pages)
            most_similar_users[i] = (ut, overlap)
    for u in overlapping_users:
        most_similar_users.append((u, len(overlapping_users[u])))

    # temporarily add in # of edits from neighbor for purpose of sorting and then remove for long-term storage
    most_similar_users_sorted = [(u[0], u[1], 0 - USER_METADATA.get(u[0], {}).get('num_pages', 0)) for u in most_similar_users]
    most_similar_users_sorted = sorted(most_similar_users_sorted, key=lambda x: (x[1], x[2]), reverse=True)
    most_similar_users_sorted = [(u[0], u[1]) for u in most_similar_users_sorted]
    if len(most_similar_users_sorted) > limit:
        cut_at = len(most_similar_users_sorted)
        for i,u in enumerate(most_similar_users_sorted[limit:]):
            overlap = u[1]
            if overlap == 1:
                cut_at = limit + i
                break
        most_similar_users_sorted = most_similar_users_sorted[:cut_at]
    # Update COEDIT_DATA so future calls don't need to repeat this process
    COEDIT_DATA[user_text] = most_similar_users_sorted

def chunkify(l, k=50):
    for i in range(0, len(l), k):
        yield l[i:i+k]

def check_user_text(user_text):
    # already in dataset -- meets valid user criteria
    if user_text in USER_METADATA:
        return None
    # wasn't in dataset
    # this could be because they have only contributed since the date of the dumps
    # but have to be careful to filter out bots still
    # unfortunately no one API call can give: is user/anon but not bot
    else:
        session = mwapi.Session('https://en.wikipedia.org', user_agent=app.config['CUSTOM_UA'])
        # check if user has made contributions in 2020
        result = session.get(
            action="query",
            list="usercontribs",
            ucuser=user_text,
            ucprop='timestamp',
            ucnamespace="|".join([str(ns) for ns in app.config['NAMESPACES']]),
            ucstart=app.config['EARLIEST_TS'],
            ucdir='newer',
            uclimit=1,
            format='json',
            formatversion=2
        )

        if result['query']['usercontribs']:
            # check if bot
            result = session.get(
                action="query",
                list="users",
                ususers=user_text,
                usprop='groups',
                format='json',
                formatversion=2
            )
            # this condition should never be met -- valid username w/ contributions but no account info
            if 'missing' in result['query']['users'][0]:
                return "User `{0}` does not appear to have an account in English Wikipedia.".format(user_text)
            # anon (has contribs but not a valid account name)
            elif 'invalid' in result['query']['users'][0]:
                USER_METADATA[user_text] = {'is_anon':True,
                                            'num_edits':0,
                                            'num_pages':0,
                                            'most_recent_edit':None,
                                            'oldest_edit':None}
                TEMPORAL_DATA[user_text] = {'d': [0] * 7, 'h': [0] * 24}
                COEDIT_DATA[user_text] = []
                return None
            elif 'groups' in result['query']['users'][0]:
                # bot
                if 'bot' in result['query']['users'][0]['groups']:
                    return "User `{0}` is a bot and therefore out of scope.".format(user_text)
                # exists and is user but wasn't in original dataset
                else:
                    USER_METADATA[user_text] = {'is_anon':False,
                                                'num_edits':0,
                                                'num_pages':0,
                                                'most_recent_edit':None,
                                                'oldest_edit':None}
                    TEMPORAL_DATA[user_text] = {'d': [0] * 7, 'h': [0] * 24}
                    COEDIT_DATA[user_text] = []
                    return None
        # account has no contributions in enwiki in namespaces
        else:
            return "User `{0}` does not appear to have an account (or edits in scope) in English Wikipedia.".format(user_text)

def validate_api_args():
    """Validate API arguments for model. Return error if missing or user-text does not exist or not relevant."""
    user_text = request.args.get('usertext')
    num_similar = request.args.get('k', DEFAULT_K)  # must be between 1 and 250
    if 'followup' in request.args:
        followup = True
    else:
        followup = False
    error = None
    try:
        num_similar = max(1, int(num_similar))
        num_similar = min(num_similar, 250)
    except ValueError:
        num_similar = DEFAULT_K
    # standardize usertext
    if user_text.lower().startswith("user:"):
        user_text = user_text[5:]
    if user_text:
        user_text = user_text.replace(" ", "_")
        user_text = user_text[0].upper() + user_text[1:]
        error = check_user_text(user_text)
    else:
        error = 'missing user_text -- e.g., "Isaac (WMF)" for https://en.wikipedia.org/wiki/User:Isaac_(WMF)'
    return user_text, num_similar, followup, error

def load_data():
    """Load in necessary data for tool."""
    load_coedit_data()
    load_temporal_data()
    load_metadata()

def load_coedit_data():
    """Load preprocessed data about edit overlap between users."""
    print("Loading co-edit data")
    expected_header = ['user_text', 'user_neighbor', 'num_pages_overlapped']
    with open(os.path.join(__dir__, 'resources/coedit_counts.tsv'), 'r') as fin:
        assert next(fin).strip().split('\t') == expected_header
        for line_str in fin:
            line = line_str.strip().split('\t')
            user = line[0]
            neighbor = line[1]
            overlap_count = int(line[2])
            if user not in COEDIT_DATA:
                COEDIT_DATA[user] = []
            COEDIT_DATA[user].append((neighbor, overlap_count))
    return COEDIT_DATA

def load_temporal_data():
    """Load preprocessed temporal information about when an account has edited."""
    print("Loading temporal data")
    expected_header = ['user_text', 'day_of_week', 'hour_of_day', 'num_edits']
    with open(os.path.join(__dir__, 'resources/temporal.tsv'), 'r') as fin:
        assert next(fin).strip().split('\t') == expected_header
        for line_str in fin:
            line = line_str.strip().split('\t')
            user_text = line[0]
            day_of_week = int(line[1]) - 1  # 0 Sunday - 6 Saturday
            hour_of_day = int(line[2])  # 0 - 23
            num_edits = int(line[3])
            if user_text not in TEMPORAL_DATA:
                TEMPORAL_DATA[user_text] = {'d':[0] * 7, 'h':[0] * 24}
            update_temporal_data(user_text, day_of_week, hour_of_day, num_edits)

def update_temporal_data(user_text, day, hour, num_edits):
    """Update data on hours / days in which a user has edited."""
    if user_text not in TEMPORAL_DATA:
        TEMPORAL_DATA[user_text] = {'d':[0] * 7, 'h':[0] * 24}
    # potentially smear data so edits in nearby hours also overlap (not just direct matches)
    for offset in app.config['TEMPORAL_OFFSET']:
        h = hour + offset  # -1 to 24
        d = (day + (h // 24)) % 7
        h = h % 24
        TEMPORAL_DATA[user_text]['d'][d] += num_edits
        TEMPORAL_DATA[user_text]['h'][h] += num_edits

def load_metadata():
    """Load some basic statistics about coverage of each account in the data."""
    print("Loading metadata")
    expected_header = ['user_text', 'is_anon', 'num_edits', 'num_pages', 'most_recent_edit', 'oldest_edit']
    with open(os.path.join(__dir__, 'resources/metadata.tsv'), 'r') as fin:
        assert next(fin).strip().split('\t') == expected_header
        for line_str in fin:
            line = line_str.strip().split('\t')
            user = line[0]
            USER_METADATA[user] = {'is_anon':eval(line[1]),
                                   'num_edits':int(line[2]),
                                   'num_pages':int(line[3]),
                                   'most_recent_edit':line[4],
                                   'oldest_edit':line[5]
                                   }

application = app
load_data()

if __name__ == '__main__':
    application.run()
