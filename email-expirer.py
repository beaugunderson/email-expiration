#!/usr/bin/env -S uv run

import re
from datetime import datetime, timezone
from pathlib import Path
import typer
from tqdm import tqdm
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

token_file = Path("./token.json")

if token_file.exists():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
else:
    flow = InstalledAppFlow.from_client_secrets_file("./credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json())

service = build("gmail", "v1", credentials=creds)

INBOX_DAYS = 7
LABEL_PREFIX = "⌛/"
MIN_AGE_TO_APPEND_LABEL = 21
PAGE_SIZE = 100


app = typer.Typer()


def get_label_id(name):
    labels = service.users().labels().list(userId="me").execute().get("labels", [])

    for label in labels:
        if label["name"] == name:
            return label["id"]

    return None


def create_label_if_missing(name):
    if not get_label_id(name):
        service.users().labels().create(userId="me", body={"name": name}).execute()


@app.command()
def setup():
    for i in range(INBOX_DAYS + 1):
        create_label_if_missing(f"x{i}")

    create_label_if_missing("auto-archived")


def day_labels():
    return {i: get_label_id(f"x{i}") for i in range(INBOX_DAYS + 1)}


def fetch_all_threads(query):
    threads = []
    page_token = None

    while True:
        response = (
            service.users()
            .threads()
            .list(
                userId="me",
                q=query,
                maxResults=PAGE_SIZE,
                pageToken=page_token,
            )
            .execute()
        )

        threads.extend(response.get("threads", []))

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return threads


@app.command()
def add_inbox_expiration():
    label_id = get_label_id(f"x{INBOX_DAYS}")

    filters = ["in:inbox", "-is:starred"]
    filters += [f"-label:x{i}" for i in range(INBOX_DAYS + 1)]

    threads = fetch_all_threads(" ".join(filters))

    for thread in tqdm(threads):
        service.users().threads().modify(
            userId="me",
            id=thread["id"],
            body={
                "addLabelIds": [label_id],
            },
        ).execute()


@app.command()
def strip_tags_on_archived_emails():
    labels = day_labels()

    for i, label_id in tqdm(labels.items()):
        threads = fetch_all_threads(f"-in:inbox label:x{i}")

        for thread in tqdm(threads):
            service.users().threads().modify(
                userId="me",
                id=thread["id"],
                body={
                    "removeLabelIds": [label_id],
                },
            ).execute()


@app.command()
def step_expiration():
    labels = day_labels()
    auto_label = get_label_id("auto-archived")

    for i in range(INBOX_DAYS + 1):
        label_id = labels[i]

        threads = fetch_all_threads(f"label:x{i}")

        if not threads:
            continue

        for thread in tqdm(threads):
            if i == 0:
                service.users().threads().modify(
                    userId="me",
                    id=thread["id"],
                    body={
                        "removeLabelIds": [label_id],
                        "addLabelIds": [auto_label],
                    },
                ).execute()

                service.users().threads().modify(
                    userId="me",
                    id=thread["id"],
                    body={
                        "removeLabelIds": ["INBOX"],
                    },
                ).execute()
            else:
                service.users().threads().modify(
                    userId="me",
                    id=thread["id"],
                    body={
                        "removeLabelIds": [label_id],
                        "addLabelIds": [labels[i - 1]],
                    },
                ).execute()


def datetime_to_date(d):
    return datetime(d.year, d.month, d.day)


def date_diff_in_days(d1, d2):
    return (datetime_to_date(d2) - datetime_to_date(d1)).days


def get_all_labels():
    results = service.users().labels().list(userId="me").execute()
    return results.get("labels", [])


def remove_all_age_labels():
    age_label_re = re.compile(r"^⌛[ /]\d+")

    labels = get_all_labels()
    matching_labels = [label for label in labels if age_label_re.match(label["name"])]

    for label in tqdm(matching_labels):
        service.users().labels().delete(
            userId="me",
            id=label["id"],
        ).execute()


def get_or_create_label(label_name):
    labels = get_all_labels()

    for label in labels:
        if label["name"] == label_name:
            return label["id"]

    # create label if not found
    result = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={"name": label_name},
        )
        .execute()
    )

    return result["id"]


def add_age_label(thread_id, age):
    if age < 31:
        age_string = f"{age}d"
    elif age < 365:
        age_string = f"{round(age / 31)}m"
    else:
        age_string = f"{round(age / 365)}y"

    label_name = LABEL_PREFIX + age_string
    label_id = get_or_create_label(label_name)

    service.users().threads().modify(
        userId="me",
        id=thread_id,
        body={
            "addLabelIds": [label_id],
        },
    ).execute()


@app.command()
def append_too_old_labels():
    remove_all_age_labels()

    now = datetime.now(timezone.utc)

    threads = fetch_all_threads("in:inbox")

    for thread in tqdm(threads):
        thread_meta = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=thread["id"],
            )
            .execute()
        )

        messages = thread_meta.get("messages", [])

        if not messages:
            continue

        last_msg = messages[-1]
        internal_date_ms = int(last_msg["internalDate"])
        last_msg_date = datetime.fromtimestamp(
            internal_date_ms / 1000.0, tz=timezone.utc
        )

        age = date_diff_in_days(last_msg_date, now)

        if age >= MIN_AGE_TO_APPEND_LABEL:
            add_age_label(thread["id"], age)


if __name__ == "__main__":
    app()
