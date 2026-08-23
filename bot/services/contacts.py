"""FollowUp Bot — Google Contacts with a local cache-aside layer.

Architecture:
    search_contacts → 1) contacts_cache table (SQLite, <1ms, encrypted)
                    → 2) People API on miss/stale (~500ms) → upsert cache

Source of truth stays Google — the cache is a 7-day-TTL copy so repeat
lookups ("meet akshay at 5") are instant and don't burn People API quota.
A stale row is still served when the API call FAILS (resilience beats
freshness for names/emails that rarely change).

The LLM never sees this table; it still goes through the contacts_search
tool, which now answers from here first.
"""

import asyncio
import logging
import re
from datetime import timedelta

from googleapiclient.discovery import build
from sqlalchemy import select, delete as sql_delete

from bot.database import async_session, ContactCache, User
from bot.services.google_auth import get_google_creds, _get_token_cipher
from bot.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

CACHE_TTL_DAYS = 7
_ENC_PREFIX = "enc::"


# ─── Field encryption (same Fernet key as Google tokens) ─────

def _enc(value: str) -> str:
    if not value:
        return ""
    cipher = _get_token_cipher()
    if not cipher:
        return value  # plaintext mode mirrors token behaviour (warned at boot)
    return _ENC_PREFIX + cipher.encrypt(value.encode("utf-8")).decode("utf-8")


def _dec(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_ENC_PREFIX):
        return value
    cipher = _get_token_cipher()
    if not cipher:
        return ""  # encrypted rows are unreadable without the key
    try:
        return cipher.decrypt(value[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


# ─── Cache layer ─────────────────────────────────────────────

def _row_to_contact(r: ContactCache) -> dict:
    return {
        "name": r.name,
        "emails": [e for e in _dec(r.email or "").split(", ") if e],
        "phones": [p for p in _dec(r.phone or "").split(", ") if p],
    }


async def _cache_lookup(user_id: int, query: str, limit: int):
    """Return (contacts, all_fresh). contacts is [] on a total miss."""
    pattern = f"%{query.strip().lower()}%"
    cutoff = utcnow_naive() - timedelta(days=CACHE_TTL_DAYS)
    async with async_session() as session:
        rows = (await session.execute(
            select(ContactCache).where(
                ContactCache.user_id == user_id,
                ContactCache.name_lower.like(pattern),
            # Without an explicit order the LIMIT truncated an arbitrary subset,
            # so which of two same-named contacts survived varied per query.
            ).order_by(ContactCache.name_lower, ContactCache.id).limit(limit)
        )).scalars().all()
    if not rows:
        return [], False
    fresh = all(r.synced_at and r.synced_at >= cutoff for r in rows)
    return [_row_to_contact(r) for r in rows], fresh


async def _cache_upsert(user_id: int, contacts: list, source: str = "contacts") -> None:
    """Insert/update cache rows, keyed on user_id + lowercase name + email.

    Keying on the name alone collapsed two different people who share one:
    the second "Akshay" overwrote the first, so `resolve_attendee` then saw a
    single strong match and booked the wrong address without ever showing a
    picker. Email is Fernet-encrypted with a non-deterministic cipher, so it
    cannot be matched in SQL — the candidate rows for a name are decrypted and
    compared here instead.
    """
    now = utcnow_naive()
    async with async_session() as session:
        for c in contacts:
            name = (c.get("name") or "").strip()
            if not name or name == "Unknown":
                continue
            emails = ", ".join(e for e in (c.get("emails") or []) if e)
            candidates = (await session.execute(
                select(ContactCache).where(
                    ContactCache.user_id == user_id,
                    ContactCache.name_lower == name.lower(),
                )
            )).scalars().all()
            # Same name AND same address = same person, refresh in place.
            # Same name, different address = a second person, insert a new row.
            row = next(
                (r for r in candidates if _dec(r.email or "") == emails), None
            )
            if row is None:
                row = ContactCache(
                    user_id=user_id, name=name, name_lower=name.lower(),
                )
                session.add(row)
            row.name = name
            row.email = _enc(emails)
            row.phone = _enc(", ".join(p for p in (c.get("phones") or []) if p))
            # A saved contact always outranks a gmail-derived one.
            if row.source != "contacts":
                row.source = source
            row.synced_at = now
        await session.commit()


async def clear_contact_cache(user_id: int) -> None:
    """Wipe a user's cached contacts (data deletion / disconnect / refresh)."""
    async with async_session() as session:
        await session.execute(
            sql_delete(ContactCache).where(ContactCache.user_id == user_id)
        )
        await session.commit()


# ─── Google People API ───────────────────────────────────────

def _search_other_contacts_sync(svc, query: str, limit: int) -> list:
    """Search Gmail-derived 'Other contacts' — people emailed but never saved.

    Kept separate and non-fatal: this needs `contacts.other.readonly`, and a
    user who consented before that scope was requested still has a valid token
    without it. Their saved contacts must keep working, so a 403 here degrades
    to "no extra results" rather than failing the whole lookup.
    """
    try:
        resp = svc.otherContacts().search(
            query=query,
            readMask="names,emailAddresses,phoneNumbers",
            pageSize=min(limit, 30),
        ).execute()
    except Exception as e:
        logger.info(f"otherContacts search skipped: {str(e)[:120]}")
        return []
    return [_parse(item.get("person", {})) for item in resp.get("results", [])]


def _api_search_sync(creds, query: str, limit: int) -> list:
    """Blocking People API call — run via asyncio.to_thread."""
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

        # Saved contacts alone used to decide this lookup, so anyone the user
        # had only ever emailed was invisible here — findable only in the
        # one-time bootstrap right after connecting, and never again. Search
        # them live too, so "meet with Priyanshu" resolves on demand.
        seen = {e.lower() for c in results for e in (c.get("emails") or [])}
        for other in _search_other_contacts_sync(svc, query, limit):
            emails = [e for e in (other.get("emails") or []) if e]
            if not emails or emails[0].lower() in seen:
                continue          # saved contacts win — richer, user-curated
            seen.add(emails[0].lower())
            results.append(other)
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


async def search_contacts(user_db, query: str = "", limit: int = 10) -> list:
    """Search contacts. Cache first; People API on miss/stale; upsert back."""
    # 1. Cache hit + fresh → done, no API call.
    cached: list = []
    if query and user_db is not None:
        cached, fresh = await _cache_lookup(user_db.id, query, limit)
        if cached and fresh:
            return cached

    # Off-loop too: get_google_creds can trigger a synchronous OAuth token
    # refresh, which is a full HTTPS round trip to Google.
    creds = await asyncio.to_thread(get_google_creds, user_db)
    if not creds:
        raise Exception("Google not connected")

    # 2. Both sources, CONCURRENTLY.
    #    People API covers saved contacts and "Other contacts"; Gmail headers
    #    cover everyone else the user has actually corresponded with. Neither
    #    is a superset — Google builds Other contacts from people the user
    #    EMAILED, so a sender they never replied to appears only in the
    #    headers. Running them in parallel means two sources for the latency
    #    of one.
    async def _people():
        return await asyncio.to_thread(_api_search_sync, creds, query, limit)

    async def _mail():
        if not query:
            return []
        from bot.services.gmail import find_people_in_mail
        return await find_people_in_mail(user_db, query)

    people_res, mail_res = await asyncio.gather(
        _people(), _mail(), return_exceptions=True,
    )

    if isinstance(people_res, Exception):
        if cached:
            # Google is down/ratelimited — stale contact info beats an error.
            logger.warning(f"People API failed ({people_res}); serving stale cache")
            return cached
        if isinstance(mail_res, Exception) or not mail_res:
            raise people_res
        people_res = []
    if isinstance(mail_res, Exception):
        logger.info(f"Gmail header lookup skipped for {query!r}: {str(mail_res)[:100]}")
        mail_res = []

    # Saved contacts win a duplicate address: they are user-curated and carry
    # phone numbers the headers never have.
    results = list(people_res or [])
    seen = {e.lower() for c in results for e in (c.get("emails") or []) if e}
    added = 0
    for person in (mail_res or []):
        addr = next((e for e in (person.get("emails") or []) if e), "")
        if not addr or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        results.append(person)
        added += 1

    source = "contacts" if people_res else "gmail"
    if added:
        logger.info("Merged %d Gmail-header contact(s) for %r", added, query)

    # 3. Write-back. Empty API result but non-empty cache → keep serving cache
    #    (searchContacts is flaky on cold caches; names rarely vanish).
    if results:
        await _cache_upsert(user_db.id, results, source=source)
        return results
    return cached if cached else results


# ─── One-time warm after Google connect ──────────────────────

async def bootstrap_contacts(wa_id: str) -> int:
    """Warm the cache right after Google connect (fire-and-forget).

    Pulls saved contacts (connections). Gmail-derived 'otherContacts' are
    attempted too but skipped gracefully — they need the
    contacts.other.readonly scope, which is not requested yet.
    """
    async with async_session() as session:
        db_user = (await session.execute(
            select(User).where(User.wa_id == wa_id)
        )).scalar_one_or_none()
    if not db_user or not db_user.google_token_json:
        return 0
    # Off-loop: may trigger a synchronous OAuth token refresh.
    creds = await asyncio.to_thread(get_google_creds, db_user)
    if not creds:
        return 0

    def _fetch():
        svc = build("people", "v1", credentials=creds)
        saved, gmail_derived = [], []
        resp = svc.people().connections().list(
            resourceName="people/me", pageSize=200,
            personFields="names,emailAddresses,phoneNumbers",
        ).execute()
        for p in resp.get("connections", []):
            saved.append(_parse(p))
        try:
            resp2 = svc.otherContacts().list(
                pageSize=200, readMask="names,emailAddresses,phoneNumbers",
            ).execute()
            for p in resp2.get("otherContacts", []):
                gmail_derived.append(_parse(p))
        except Exception as e:
            logger.info(f"otherContacts skipped (scope not granted): {e}")
        return saved, gmail_derived

    try:
        saved, gmail_derived = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning(f"Contact bootstrap failed for {wa_id}: {e}")
        return 0

    if saved:
        await _cache_upsert(db_user.id, saved, source="contacts")
    if gmail_derived:
        await _cache_upsert(db_user.id, gmail_derived, source="gmail")
    total = len(saved) + len(gmail_derived)
    logger.info(f"Contact cache warmed for {wa_id}: {total} contact(s)")
    return total


def name_from_email(address: str) -> str:
    """"muskaan.jain@unstop.com" -> "Muskaan Jain".

    Google frequently returns an address with NO displayName, especially for
    Gmail-derived contacts. Falling back to the literal string "Unknown" put
    that word in the contact picker and then straight into an event title
    ("Meeting with Unknown"), so a readable name is derived instead.
    """
    local = (address or "").split("@", 1)[0]
    parts = [p for p in re.split(r"[._+-]+", local) if p and not p.isdigit()]
    return " ".join(p.capitalize() for p in parts)


def _parse(person: dict) -> dict:
    names = person.get("names", [])
    emails = [e.get("value") for e in person.get("emailAddresses", []) if e.get("value")]
    phones = [p.get("value") for p in person.get("phoneNumbers", []) if p.get("value")]
    name = (names[0].get("displayName") if names else "") or ""
    if not name:
        name = name_from_email(emails[0]) if emails else ""
    return {"name": name or "Unknown", "emails": emails, "phones": phones}
