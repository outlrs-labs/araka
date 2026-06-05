"""FollowUp Bot — Google Contacts search via People API."""

import logging

from googleapiclient.discovery import build

from bot.services.google_auth import get_google_creds

logger = logging.getLogger(__name__)


async def search_contacts(user_db, query: str = "", limit: int = 10) -> list:
    """Search Google Contacts. Returns list of {name, emails, phones}."""
    creds = get_google_creds(user_db)
    if not creds:
        raise Exception("Google not connected")

    svc = build("people", "v1", credentials=creds)
    results = []

    if query:
        resp = svc.people().searchContacts(
            query=query,
            readMask="names,emailAddresses,phoneNumbers",
            pageSize=limit,
        ).execute()
        for item in resp.get("results", []):
            results.append(_parse(item.get("person", {})))
    else:
        resp = svc.people().connections().list(
            resourceName="people/me",
            pageSize=limit,
            personFields="names,emailAddresses,phoneNumbers",
            sortOrder="FIRST_NAME_ASCENDING",
        ).execute()
        for person in resp.get("connections", []):
            results.append(_parse(person))

    return results


def _parse(person: dict) -> dict:
    names = person.get("names", [])
    name = names[0].get("displayName") if names else "Unknown"
    emails = [e.get("value") for e in person.get("emailAddresses", [])]
    phones = [p.get("value") for p in person.get("phoneNumbers", [])]
    return {"name": name, "emails": emails, "phones": phones}
