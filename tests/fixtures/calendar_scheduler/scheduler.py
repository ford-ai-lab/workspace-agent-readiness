"""Meeting scheduler: a LangChain agent finds a slot and books it. Raw REST, no client library."""
import requests
from langchain_openai import ChatOpenAI

BASE = "https://www.googleapis.com/calendar/v3"
model = ChatOpenAI(model="gpt-5")


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def calendars(token):
    return requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers=headers(token)).json()


def busy(token, emails, start, end):
    body = {"timeMin": start, "timeMax": end, "items": [{"id": e} for e in emails]}
    return requests.post("https://www.googleapis.com/calendar/v3/freeBusy", json=body, headers=headers(token)).json()


def upcoming(token, calendar_id="primary"):
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events?singleEvents=true&orderBy=startTime"
    return requests.get(url, headers=headers(token)).json()


def book(token, event, calendar_id="primary"):
    return requests.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        json=event,
        headers=headers(token),
    ).json()


def reschedule(token, event_id, patch, calendar_id="primary"):
    return requests.patch(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        json=patch,
        headers=headers(token),
    ).json()


def cancel(token, event_id, calendar_id="primary"):
    requests.delete(f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}", headers=headers(token))


def quick(token, text):
    return requests.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/quickAdd",
        params={"text": text},
        headers=headers(token),
    ).json()


def share(token, calendar_id, email):
    rule = {"role": "reader", "scope": {"type": "user", "value": email}}
    return requests.post(f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/acl", json=rule, headers=headers(token)).json()
