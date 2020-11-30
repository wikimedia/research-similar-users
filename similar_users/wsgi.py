#!/usr/bin/env python3

from datetime import datetime, timedelta
from ast import literal_eval as make_tuple
import argparse
import distutils.util
import logging
import os
import pathlib

import mwapi
import yaml
from flask import Flask, request, jsonify, render_template, abort
from flask_basicauth import BasicAuth
from flask_cors import CORS
from prometheus_flask_exporter import PrometheusMetrics
from sklearn.metrics.pairwise import cosine_similarity
from models import database, UserMetadata, Coedit, Temporal

app = Flask(__name__)

metrics = PrometheusMetrics(app)
basic_auth = BasicAuth(app)
# Enable CORS for API endpoints
cors = CORS(app, resources={r"/api/*": {"origins": "*"}})

# Testing
# Local: http://127.0.0.1:5000/similarusers?usertext=Ziyingjiang
# VPS: https://spd-test.wmcloud.org/similarusers?usertext=Bttowadch&k=50

# Data dictionaries -- TODO: move to sqllitedict or something equivalent
# Currently used for both READ and WRITE though
USER_METADATA = {}  # is_anon; num_edits; num_pages; most_recent_edit; oldest_edit
COEDIT_DATA = {}
TEMPORAL_DATA = {}

# TODO: Make all of these configuration options
DEFAULT_K = 50
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
READABLE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
URL_PREFIX = "https://spd-test.wmcloud.org/similarusers"
EDITORINTERACT_URL = "https://sigma.toolforge.org/editorinteract.py?users={0}&users={1}&users=&startdate=&enddate=&ns=&server=enwiki&allusers=on"
INTERACTIONTIMELINE_URL = (
    "https://interaction-timeline.toolforge.org/?wiki=enwiki&user={0}&user={1}"
)


@app.route("/")
@basic_auth.required
def index():
    """Simple UI for querying API. Password-protected to reduce chance of accidental discovery / abuse."""
    if app.config.get("ENABLE_UI", False):
        return render_template("index.html")
    else:
        return abort(403, description="UI disabled")


@app.route("/similarusers", methods=["GET"])
@basic_auth.required
@metrics.counter('similar_users', 'Number of calls to similarusers',
                 labels={'similar_count': lambda r: len(r.get_json()["results"]) if "results" in r.get_json() else 0})
def get_similar_users():
    """For a given user, find the k-most-similar users based on edit overlap.

    Expected parameters:
    * usertext (str): username or IP address to query
    * k (int): how many similar users to return at maximum?
    * followup (bool): include additional tool links in API response for follow-up on data
    """
    user_text, num_similar, followup, error = validate_api_args()
    if error is not None:
        app.logger.error("Got error when trying to validate API arguments: %s", error)
        return jsonify({"Error": error})

    try:
        lookup_user(user_text)
    except Exception as e:
        app.logger.error(f'Unable to load data for user {user_text}.', e)
        return jsonify({'Error': str(e)})

    edits = get_additional_edits(
        user_text, last_edit_timestamp=USER_METADATA[user_text]["most_recent_edit"]
    )
    app.logger.debug("Got %d edits for user %s", len(edits) if edits else 0, user_text)
    if edits is not None:
        update_coedit_data(user_text, edits, app.config["EDIT_WINDOW"])
    overlapping_users = COEDIT_DATA.get(user_text, [])[:num_similar]

    oldest_edit = None
    last_edit = None
    app.logger.info(str(USER_METADATA[user_text]))
    if USER_METADATA[user_text]["oldest_edit"]:
        oldest_edit = USER_METADATA[user_text]["oldest_edit"].strftime(READABLE_TIME_FORMAT)
    else:
        app.logger.debug("Didn't get an oldest_edit for user %s", user_text)

    if USER_METADATA[user_text]["most_recent_edit"]:
        last_edit = USER_METADATA[user_text]["most_recent_edit"].strftime(READABLE_TIME_FORMAT)
    else:
        app.logger.debug("Didn't get an most_recent_edit for user %s", user_text)

    result = {
        "user_text": user_text,
        "num_edits_in_data": USER_METADATA[user_text]["num_edits"],
        "first_edit_in_data": oldest_edit,
        "last_edit_in_data": last_edit,
        "results": [
            build_result(user_text, u[0], u[1], num_similar, followup)
            for u in overlapping_users
        ],
    }
    app.logger.debug("Got %d similarity results for user %s", len(result["results"]), user_text)
    return jsonify(result)

@app.route("/healthz", methods=["GET"])
def healthz():
    return "similarusers is running"

def build_result(user_text, neighbor, num_pages_overlapped, num_similar, followup):
    """Build a single similar-user API response"""
    r = {
        "user_text": neighbor,
        "num_edits_in_data": USER_METADATA.get(neighbor, {}).get(
            "num_pages", num_pages_overlapped
        ),
        "edit-overlap": num_pages_overlapped / USER_METADATA[user_text]["num_pages"],
        "edit-overlap-inv": min(
            1,
            num_pages_overlapped / USER_METADATA.get(neighbor, {}).get("num_pages", 1),
        ),
        "day-overlap": get_temporal_overlap(user_text, neighbor, "d"),
        "hour-overlap": get_temporal_overlap(user_text, neighbor, "h"),
    }
    if followup:
        r["follow-up"] = {
            "similar": "{0}?usertext={1}&k={2}".format(
                URL_PREFIX, neighbor, num_similar
            ),
            "editorinteract": EDITORINTERACT_URL.format(user_text, neighbor),
            "interaction-timeline": INTERACTIONTIMELINE_URL.format(user_text, neighbor),
        }
    return r


def get_temporal_overlap(u1, u2, k):
    """Determine how similar two users are in terms of days and hours in which they edit."""
    # overlap in days-of-week
    if k == "d":
        cs = cosine_similarity(
            [TEMPORAL_DATA.get(u1, {}).get("d", [0] * 7)],
            [TEMPORAL_DATA.get(u2, {}).get("d", [0] * 7)],
        )[0][0]
    # overlap in hours-of-the-day
    elif k == "h":
        cs = cosine_similarity(
            [TEMPORAL_DATA.get(u1, {}).get("h", [0] * 24)],
            [TEMPORAL_DATA.get(u2, {}).get("h", [0] * 24)],
        )[0][0]
    else:
        app.logger.error(
            "Unrecognised temporal overlap key - expected 'd' or 'h' but got %s", k
        )
        raise Exception(
            "Do not recognize temporal overlap key -- must be 'd' for daily or 'h' for hourly."
        )
    # map cosine similarity values to qualitative labels
    # thresholds based on examining some examples and making judgments on how similar they seemed to be
    level = "No overlap"

    if cs == 1:
        level = "Same"
    elif cs > 0.8:
        level = "High"
    elif cs > 0.5:
        level = "Medium"
    elif cs > 0:
        level = "Low"

    return {"cos-sim": cs, "level": level}


def get_additional_edits(
    user_text, last_edit_timestamp=None, lang="en", limit=1000, session=None
):
    """Gather edits made by a user since last data dumps -- e.g., October edits if dumps end of September dumps used."""
    if last_edit_timestamp:
        arvstart = last_edit_timestamp + timedelta(
            seconds=1
        )
    else:
        # TODO move this timestamp out of configuration - either automate it
        # based on current date or query it from a datastore.
        arvstart = app.config["MOST_RECENT_REV_TS"]
    if session is None:
        session = mwapi.Session(
            "https://{0}.wikipedia.org".format(lang), user_agent=app.config["CUSTOM_UA"]
        )

    # generate list of all revisions since user's last recorded revision
    result = session.get(
        action="query",
        list="allrevisions",
        arvuser=user_text,
        arvprop="ids|timestamp|comment|user",
        arvnamespace="|".join([str(ns) for ns in app.config["NAMESPACES"]]),
        arvstart=arvstart,
        arvdir="newer",
        format="json",
        arvlimit=500,
        formatversion=2,
        continuation=True,
    )
    min_timestamp = USER_METADATA[user_text]["oldest_edit"]
    max_timestamp = USER_METADATA[user_text]["most_recent_edit"]
    new_edits = 0
    new_pages = 0
    try:
        pageids = {}
        for r in result:
            for page in r["query"]["allrevisions"]:
                pid = page["pageid"]
                if pid not in pageids:
                    pageids[pid] = []
                    new_pages += 1
                for rev in page["revisions"]:
                    ts = rev["timestamp"]
                    pageids[pid].append(ts)
                    dtts = datetime.strptime(ts, TIME_FORMAT)
                    # update TEMPORAL_DATA so future calls don't have to repeat this
                    update_temporal_data(user_text, dtts.day, dtts.hour, 1)
                    new_edits += 1
                    if min_timestamp is None:
                        min_timestamp = dtts
                        max_timestamp = dtts
                    else:
                        max_timestamp = max(max_timestamp, dtts)
                        min_timestamp = min(min_timestamp, dtts)
            if len(pageids) > limit:
                break
        # Update USER_METADATA so future calls don't need to repeat this process
        USER_METADATA[user_text]["num_edits"] += new_edits
        # this is not ideal as these might not be new pages but too expensive to check and getting it wrong isn't so bad
        USER_METADATA[user_text]["num_pages"] += new_pages
        USER_METADATA[user_text]["most_recent_edit"] = max_timestamp
        USER_METADATA[user_text]["oldest_edit"] = min_timestamp
        return pageids
    except Exception as exc:
        app.logger.error(
            "Failed to get additional edits for {user_text}, lang {lang}. {last_edit}. Exception: {exc}".format(
                user_text=user_text,
                lang=lang,
                last_edit="Last edit timestamp %s" % last_edit_timestamp
                if last_edit_timestamp
                else "",
                exc=str(exc),
            )
        )
        return None


def update_coedit_data(user_text, new_edits, k, lang="en", session=None, limit=250):
    """Get all new edits since dump ended on pages the user edited and overlapping users.

    NOTE: this is potentially very high latency for pages w/ many edits or if the editor edited many pages
    TODO: come up with a sampling strategy -- e.g., cap at 50
    ALT TODO: only do first k -- e.g., 50 -- but rewrite how additional edits are stored so can ensure that the next API call
    will get the next 50 without missing data.
    """
    most_similar_users = COEDIT_DATA[user_text]
    if session is None:
        session = mwapi.Session(
            "https://{0}.wikipedia.org".format(lang), user_agent=app.config["CUSTOM_UA"]
        )

    overlapping_users = {}
    for pid in new_edits:
        # generate list of all revisions since user's last recorded revision
        result = session.get(
            action="query",
            prop="revisions",
            pageids=pid,
            rvprop="ids|timestamp|user",
            # TODO move this timestamp out of configuration - either automate it
            # based on current date or query it from a datastore.
            rvstart=app.config["MOST_RECENT_REV_TS"],
            rvdir="newer",
            format="json",
            rvlimit=500,
            formatversion=2,
            continuation=True,
        )
        for r in result:
            revs = r["query"]["pages"][0].get("revisions", [])
            user_edit_indices = [
                i for i, e in enumerate(revs) if e["user"] == user_text
            ]
            for idx in user_edit_indices:
                for e in revs[max(0, idx - k) : idx + k]:
                    if e["user"] == user_text:
                        continue
                    if e["user"] not in overlapping_users:
                        overlapping_users[e["user"]] = set()
                    overlapping_users[e["user"]].add(pid)

    # remove bots
    new_users = [u for u in overlapping_users if u not in USER_METADATA]
    for user_list in chunkify(new_users):
        result = session.get(
            action="query",
            list="users",
            ususers="|".join(user_list),
            usprop="groups",
            format="json",
            formatversion=2,
        )
        for u in result["query"]["users"]:
            if "bot" in u.get("groups", []):
                overlapping_users.pop(u["name"])

    # update overlap list
    for i in range(len(most_similar_users) - 1, -1, -1):
        ut = most_similar_users[i][0]
        overlap = most_similar_users[i][1]
        if ut in overlapping_users:
            new_pages = overlapping_users.pop(ut)
            overlap += len(new_pages)
            most_similar_users[i] = (ut, overlap)
    for u in overlapping_users:
        most_similar_users.append((u, len(overlapping_users[u])))

    # temporarily add in # of edits from neighbor for purpose of sorting and then remove for long-term storage
    most_similar_users_sorted = [
        (u[0], u[1], 0 - USER_METADATA.get(u[0], {}).get("num_pages", 0))
        for u in most_similar_users
    ]
    most_similar_users_sorted = sorted(
        most_similar_users_sorted, key=lambda x: (x[1], x[2]), reverse=True
    )
    most_similar_users_sorted = [(u[0], u[1]) for u in most_similar_users_sorted]
    if len(most_similar_users_sorted) > limit:
        cut_at = len(most_similar_users_sorted)
        for i, u in enumerate(most_similar_users_sorted[limit:]):
            overlap = u[1]
            if overlap == 1:
                cut_at = limit + i
                break
        most_similar_users_sorted = most_similar_users_sorted[:cut_at]
    # Update COEDIT_DATA so future calls don't need to repeat this process
    COEDIT_DATA[user_text] = most_similar_users_sorted


def chunkify(l, k=50):
    for i in range(0, len(l), k):
        yield l[i : i + k]


def check_user_text(user_text):
    # already in dataset -- meets valid user criteria
    if user_text in USER_METADATA:
        return None

    # wasn't in dataset
    # this could be because they have only contributed since the date of the dumps
    # but have to be careful to filter out bots still
    # unfortunately no one API call can give: is user/anon but not bot
    session = mwapi.Session(
        "https://en.wikipedia.org", user_agent=app.config["CUSTOM_UA"]
    )
    # check if user has made contributions in 2020
    result = session.get(
        action="query",
        list="usercontribs",
        ucuser=user_text,
        ucprop="timestamp",
        ucnamespace="|".join([str(ns) for ns in app.config["NAMESPACES"]]),
        # TODO move this timestamp out of configuration - either automate it
        # based on current date or query it from a datastore.
        ucstart=app.config["EARLIEST_TS"],
        ucdir="newer",
        uclimit=1,
        format="json",
        formatversion=2,
    )

    if result["query"]["usercontribs"]:
        # check if bot
        result = session.get(
            action="query",
            list="users",
            ususers=user_text,
            usprop="groups",
            format="json",
            formatversion=2,
        )
        # this condition should never be met -- valid username w/ contributions but no account info
        if "missing" in result["query"]["users"][0]:
            app.logger.error(
                "Received request for user %s when they don't appear to have an enwiki account",
                user_text,
            )
            return "User `{0}` does not appear to have an account in English Wikipedia.".format(
                user_text
            )
        # anon (has contribs but not a valid account name)
        elif "invalid" in result["query"]["users"][0]:
            USER_METADATA[user_text] = {
                "is_anon": True,
                "num_edits": 0,
                "num_pages": 0,
                "most_recent_edit": None,
                "oldest_edit": None,
            }
            TEMPORAL_DATA[user_text] = {"d": [0] * 7, "h": [0] * 24}
            COEDIT_DATA[user_text] = []
            return None
        elif "groups" in result["query"]["users"][0]:
            # bot
            if "bot" in result["query"]["users"][0]["groups"]:
                app.logger.warning(
                    "Received request for user %s which is a bot account - out of scope",
                    user_text,
                )
                return "User `{0}` is a bot and therefore out of scope.".format(
                    user_text
                )
            # exists and is user but wasn't in original dataset
            else:
                USER_METADATA[user_text] = {
                    "is_anon": False,
                    "num_edits": 0,
                    "num_pages": 0,
                    "most_recent_edit": None,
                    "oldest_edit": None,
                }
                TEMPORAL_DATA[user_text] = {"d": [0] * 7, "h": [0] * 24}
                COEDIT_DATA[user_text] = []
                app.logger.debug(
                    "Received request for user %s but user is not in dataset", user_text
                )
                return None

    # account has no contributions in enwiki in namespaces
    app.logger.warning(
        "Received request for user %s but user does not have an account or edits in scope on enwiki",
        user_text,
    )
    return "User `{0}` does not appear to have an account (or edits in scope) in English Wikipedia.".format(
        user_text
    )


def validate_api_args():
    """Validate API arguments for model. Return error if missing or user-text does not exist or not relevant."""
    user_text = request.args.get("usertext")
    num_similar = request.args.get("k", DEFAULT_K)  # must be between 1 and 250
    followup = "followup" in request.args

    if not user_text:
        abort(422, "No usertext provided")
    if not num_similar:
        abort(422, "No k specified")

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


def load_coedit_data(resource_dir):
    """Load preprocessed data about edit overlap between users."""
    app.logger.info("Loading co-edit data")
    expected_header = ["user_text", "user_neighbor", "num_pages_overlapped"]
    with open(os.path.join(resource_dir, "coedit_counts.tsv"), "r") as fin:
        assert next(fin).strip().split("\t") == expected_header
        for line_str in fin:
            try:
                line = line_str.strip().split("\t")
                user_text = line[0]
                neighbor = line[1]
                overlap_count = int(line[2])

                coedit = Coedit(user_text=user_text, neighbor=neighbor, overlap_count=overlap_count)
            except Exception as e:
                app.logger.error(f'Failed to parse record {line} - {line_str}', e)
            else:
                database.session.add(coedit)
        database.session.commit()


def load_temporal_data(resource_dir):
    """Load preprocessed temporal information about when an account has edited."""
    app.logger.info("Loading temporal data")
    expected_header = ["user_text", "day_of_week", "hour_of_day", "num_edits"]
    with open(os.path.join(resource_dir, "temporal.tsv"), "r") as fin:
        assert next(fin).strip().split("\t") == expected_header
        for line_str in fin:
            try:
                line = line_str.strip().split("\t")
                user_text = line[0]
                day_of_week = int(line[1]) - 1  # 0 Sunday - 6 Saturday
                hour_of_day = int(line[2])  # 0 - 23
                num_edits = int(line[3])

                temporal = Temporal(user_text=user_text, d=day_of_week, h=hour_of_day, num_edits=num_edits)

            except Exception as e:
                app.logger.error(f'Failed to parse record {line_str}.', e)
            else:
                database.session.add(temporal)
        database.session.commit()


def update_temporal_data(user_text, day, hour, num_edits):
    """Update data on hours / days in which a user has edited."""
    if user_text not in TEMPORAL_DATA:
        TEMPORAL_DATA[user_text] = {"d": [0] * 7, "h": [0] * 24}
    # potentially smear data so edits in nearby hours also overlap (not just direct matches)
    offset_tup = make_tuple(app.config["TEMPORAL_OFFSET"])
    for offset in offset_tup:
        h = hour + offset  # -1 to 24
        d = (day + (h // 24)) % 7
        h = h % 24
        TEMPORAL_DATA[user_text]["d"][d] += num_edits
        TEMPORAL_DATA[user_text]["h"][h] += num_edits


def load_metadata(resource_dir):
    """Load some basic statistics about coverage of each account in the data."""
    app.logger.info("Loading metadata")
    expected_header = [
        "user_text",
        "is_anon",
        "num_edits",
        "num_pages",
        "most_recent_edit",
        "oldest_edit",
    ]
    with open(os.path.join(resource_dir, "metadata.tsv"), "r") as fin:

        assert next(fin).strip().split("\t") == expected_header
        for line_str in fin:
            # TODO use csv library here?
            try:
                line = line_str.strip().split("\t")
                user_text = line[0]
                user = UserMetadata(
                    user_text=user_text,
                    is_anon=bool(distutils.util.strtobool(line[1])),
                    num_edits=int(line[2]),
                    num_pages=int(line[3]),
                    most_recent_edit=datetime.strptime(line[4], TIME_FORMAT),
                    oldest_edit=datetime.strptime(line[5], TIME_FORMAT))
            except Exception as e:
                app.looger.error(f'Failed to parse record {line_str}: ', str(e))
            else:
                database.session.add(user)
        database.session.commit()


def lookup_user(user_text):
    """
    Lookup user data from the database, and populate session globals.

    :param user_text: the username we want to analyze.
    :return:
    """
    metadata = UserMetadata.query.filter_by(user_text=user_text).first()

    USER_METADATA[user_text] = metadata.__dict__ if metadata else {}
    COEDIT_DATA[user_text] = [(row.neighbor, row.overlap_count)
                              for row in Coedit.query.filter_by(user_text=user_text).all()]
    TEMPORAL_DATA[user_text] = {'d': [0] * 7, 'h': [0] * 24}

    temporal = Temporal.query.filter_by(user_text=user_text).first()
    if temporal:
        update_temporal_data(user_text, temporal.d, temporal.h, temporal.num_edits)


def load_data(resourcedir):
    load_metadata(resourcedir)
    load_coedit_data(resourcedir)
    load_temporal_data(resourcedir)


def parse_args():
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="A webservice to determine the degree of similarity between users"
    )
    parser.add_argument(
        "--config",
        "-c",
        action="store",
        help="Path to the service configuration file.",
        type=pathlib.Path,
        default=os.path.join(os.path.dirname(__file__), "flask_config.yaml"),
    )
    parser.add_argument(
        "--resourcedir",
        "-r",
        action="store",
        help="Path to the service input files. When specified, data will be loaded "
             "into a database (default: in memory sqlite) ",
        type=pathlib.Path,
        default=os.path.join(os.path.dirname(__file__), "resources"),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        help="Verbose output.",
        default=False,
    )
    return parser.parse_args()


def main():
    args = parse_args()
    # TODO move app creation to its own function rather than using it as a
    # global
    config_yaml = yaml.safe_load(open(args.config))
    app.config.update(config_yaml)

    logging.basicConfig(level=logging.getLevelName(config_yaml["LOG_LEVEL"]))

    database.init_app(app)
    if 'resourcedir' in args:
        with app.test_request_context():
            # TODO(gmodena, 2020-11-27): create (if not exists) a database and populate it at startup.
            # Used for development; this logic will be moved to a migration/manager module.
            # To do it properly, we should refactor app creation to a factory,
            # rather than using a global object.
            try:
                database.create_all()
                load_data(args.resourcedir)
            except Exception as e:
                app.logger.error('Failed to load input data.', str(e))
    # Only use LISTEN_IP to configure docker port exposure - not for serving elsewhere.
    app.run(app.config["LISTEN_IP"] if "LISTEN_IP" in app.config else "127.0.0.1")


if __name__ == "__main__":
    main()
