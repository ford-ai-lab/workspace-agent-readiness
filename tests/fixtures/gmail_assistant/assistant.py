"""Inbox assistant: an LLM triages unread mail, drafts replies and sends the easy ones."""
import base64
from email.message import EmailMessage

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI

SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]
llm = OpenAI()


def gmail():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    return build("gmail", "v1", credentials=creds)


def unread(service, n=25):
    resp = service.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=n).execute()
    out = []
    for ref in resp.get("messages", []):
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        out.append(msg)
    return out


def thread_context(service, thread_id):
    return (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata")
        .execute()
    )


def triage(service, msg):
    verdict = llm.chat.completions.create(
        model="gpt-5", messages=[{"role": "user", "content": "Label this email: " + msg["snippet"]}]
    ).choices[0].message.content
    labels = {l["name"]: l["id"] for l in service.users().labels().list(userId="me").execute()["labels"]}
    if verdict not in labels:
        labels[verdict] = service.users().labels().create(userId="me", body={"name": verdict}).execute()["id"]
    service.users().messages().modify(
        userId="me", id=msg["id"], body={"addLabelIds": [labels[verdict]], "removeLabelIds": ["UNREAD"]}
    ).execute()
    return verdict


def reply(service, msg, text, send=False):
    em = EmailMessage()
    em.set_content(text)
    em["To"] = "someone@example.com"
    em["Subject"] = "Re: hello"
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode()
    body = {"raw": raw, "threadId": msg["threadId"]}
    if send:
        return service.users().messages().send(userId="me", body=body).execute()
    draft = service.users().drafts().create(userId="me", body={"message": body}).execute()
    return draft


def polish_draft(service, draft_id, raw):
    return service.users().drafts().update(userId="me", id=draft_id, body={"message": {"raw": raw}}).execute()


def save_attachment(service, msg_id, att_id):
    att = service.users().messages().attachments().get(userId="me", messageId=msg_id, id=att_id).execute()
    return base64.urlsafe_b64decode(att["data"])


def start_push(service):
    return service.users().watch(userId="me", body={"topicName": "projects/demo/topics/gmail"}).execute()


def catch_up(service, history_id):
    return service.users().history().list(userId="me", startHistoryId=history_id).execute()


def junk(service, msg_id):
    service.users().messages().trash(userId="me", id=msg_id).execute()
