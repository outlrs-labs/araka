"""FollowUp Bot — Google Sheets logging.

Each user gets one spreadsheet with three tabs:
  Tasks | Meetings | Follow-ups
"""

import logging
from datetime import datetime, timezone

from googleapiclient.discovery import build
from sqlalchemy import select

from bot.services.google_auth import get_google_creds
from bot.database import async_session, User
from bot.config import config

logger = logging.getLogger(__name__)

SHEET_TITLE = "FollowUp Bot — Log"

TASKS_HEADERS = [
    "ID", "Title", "Assignee", "Date", "Time",
    "Duration", "Mode", "Status", "Meet Link", "Created At", "Updated At",
]
MEETINGS_HEADERS = ["Event ID", "Title", "Start", "End", "Mode", "Meet Link", "Created At"]
FOLLOWUPS_HEADERS = ["Task ID", "Action", "Details", "Timestamp"]
TODO_HEADERS = ["#", "Task", "Status", "Added At", "Completed At"]


def _get_sheets_service(user_db):
    creds = get_google_creds(user_db)
    return build("sheets", "v4", credentials=creds) if creds else None


async def _ensure_spreadsheet(user_db):
    """Create or return the user's logging spreadsheet ID."""
    if user_db.google_sheet_id:
        return user_db.google_sheet_id

    svc = _get_sheets_service(user_db)
    if not svc:
        return None

    try:
        body = {
            "properties": {"title": SHEET_TITLE},
            "sheets": [
                {"properties": {"title": "Tasks", "index": 0}},
                {"properties": {"title": "Meetings", "index": 1}},
                {"properties": {"title": "Follow-ups", "index": 2}},
                {"properties": {"title": "To-Do", "index": 3}},
            ],
        }
        spreadsheet = svc.spreadsheets().create(body=body).execute()
        sheet_id = spreadsheet["spreadsheetId"]

        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": "Tasks!A1", "values": [TASKS_HEADERS]},
                    {"range": "Meetings!A1", "values": [MEETINGS_HEADERS]},
                    {"range": "Follow-ups!A1", "values": [FOLLOWUPS_HEADERS]},
                    {"range": "To-Do!A1", "values": [TODO_HEADERS]},
                ],
            },
        ).execute()

        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == user_db.id))
            u = result.scalar_one_or_none()
            if u:
                u.google_sheet_id = sheet_id
                await session.commit()

        logger.info(f"Created Sheet ({sheet_id}) for user {user_db.wa_id}")
        return sheet_id

    except Exception as e:
        logger.error(f"Spreadsheet creation failed: {e}")
        return None


async def log_task_to_sheet(user_db, task):
    """Append a task row to the Tasks tab."""
    if not user_db or not user_db.google_token_json:
        return
    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return
    try:
        import pytz
        tz = pytz.timezone(config.BOT_TIMEZONE)
        date_str = time_str = ""
        if task.scheduled_at:
            local = task.scheduled_at.replace(tzinfo=pytz.utc).astimezone(tz)
            date_str = local.strftime("%Y-%m-%d")
            time_str = local.strftime("%-I:%M %p IST")
        row = [
            task.id, task.title or "", task.assignee_name or "",
            date_str, time_str, task.duration_minutes or 30,
            task.mode or "online", task.status or "draft",
            task.meeting_link or "",
            task.created_at.strftime("%Y-%m-%d %H:%M") if task.created_at else "",
            task.updated_at.strftime("%Y-%m-%d %H:%M") if task.updated_at else "",
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="Tasks!A:K",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        logger.error(f"Sheet task log failed: {e}")


async def log_meeting_to_sheet(user_db, event_info: dict, mode: str = "online"):
    """Log a calendar meeting to the Meetings tab."""
    if not user_db or not user_db.google_token_json:
        return
    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return
    try:
        row = [
            event_info.get("id", ""), event_info.get("title", ""),
            event_info.get("start", ""), event_info.get("end", ""),
            mode, event_info.get("meet", ""),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="Meetings!A:G",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        logger.error(f"Sheet meeting log failed: {e}")


async def log_followup_to_sheet(user_db, task_id: int, action: str, details: str = ""):
    """Log a follow-up action to the Follow-ups tab."""
    if not user_db or not user_db.google_token_json:
        return
    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return
    try:
        row = [task_id, action, details, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="Follow-ups!A:D",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as e:
        logger.error(f"Sheet follow-up log failed: {e}")


# ═══════════════════════════════════════════════════════════════
# To-Do List (Google Sheets backed)
# ═══════════════════════════════════════════════════════════════


async def _ensure_todo_tab(svc, sheet_id: str) -> bool:
    """Make sure the To-Do tab exists. Auto-creates it for old sheets."""
    try:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties.title").execute()
        tab_names = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if "To-Do" in tab_names:
            return True
        # Create the tab
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": "To-Do"}}}]},
        ).execute()
        # Add headers
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range="To-Do!A1",
            valueInputOption="RAW",
            body={"values": [TODO_HEADERS]},
        ).execute()
        logger.info(f"Created To-Do tab in sheet {sheet_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to ensure To-Do tab: {e}")
        return False


async def add_todo_to_sheet(user_db, items: list) -> dict:
    """Add one or more to-do items. Returns {added: int, total: int, items: [...]}."""
    if not user_db or not user_db.google_token_json:
        return {"error": "GOOGLE_NOT_CONNECTED"}

    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return {"error": "Could not access Google Sheets."}

    try:
        await _ensure_todo_tab(svc, sheet_id)

        # Read existing rows to get next index number
        existing = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="To-Do!A:E"
        ).execute()
        rows = existing.get("values", [])
        next_idx = len(rows)  # Row 1 = header, so len = next number

        import pytz
        tz = pytz.timezone(config.BOT_TIMEZONE)
        now_str = datetime.now(tz).strftime("%Y-%m-%d %I:%M %p")

        new_rows = []
        added_items = []
        for i, item in enumerate(items):
            idx = next_idx + i
            new_rows.append([str(idx), item.strip(), "Pending", now_str, ""])
            added_items.append({"index": idx, "task": item.strip(), "status": "Pending"})

        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="To-Do!A:E",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": new_rows},
        ).execute()

        return {"added": len(items), "total": next_idx + len(items) - 1, "items": added_items}

    except Exception as e:
        logger.error(f"Add todo failed: {e}")
        return {"error": str(e)}


async def get_todos_from_sheet(user_db) -> dict:
    """Read all to-do items. Returns {items: [{index, task, status, added_at, completed_at}]}."""
    if not user_db or not user_db.google_token_json:
        return {"error": "GOOGLE_NOT_CONNECTED"}

    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return {"error": "Could not access Google Sheets."}

    try:
        await _ensure_todo_tab(svc, sheet_id)

        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="To-Do!A:E"
        ).execute()
        rows = result.get("values", [])

        if len(rows) <= 1:  # Only header
            return {"items": [], "message": "To-do list is empty."}

        items = []
        for row in rows[1:]:  # Skip header
            if len(row) < 3:
                continue
            items.append({
                "index": row[0] if len(row) > 0 else "",
                "task": row[1] if len(row) > 1 else "",
                "status": row[2] if len(row) > 2 else "Pending",
                "added_at": row[3] if len(row) > 3 else "",
                "completed_at": row[4] if len(row) > 4 else "",
            })

        return {"items": items, "total": len(items)}

    except Exception as e:
        logger.error(f"Get todos failed: {e}")
        return {"error": str(e)}


async def update_todo_status(user_db, item_index: int, done: bool = True) -> dict:
    """Mark a to-do item as done or pending. item_index is the # shown to user."""
    if not user_db or not user_db.google_token_json:
        return {"error": "GOOGLE_NOT_CONNECTED"}

    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return {"error": "Could not access Google Sheets."}

    try:
        await _ensure_todo_tab(svc, sheet_id)

        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="To-Do!A:E"
        ).execute()
        rows = result.get("values", [])

        # Find the row with matching index
        target_row = None
        for i, row in enumerate(rows[1:], start=2):  # Sheet rows are 1-indexed, skip header
            if len(row) > 0 and str(row[0]) == str(item_index):
                target_row = i
                break

        if not target_row:
            return {"error": f"To-do item #{item_index} not found."}

        import pytz
        tz = pytz.timezone(config.BOT_TIMEZONE)

        if done:
            new_status = "Done"
            completed_at = datetime.now(tz).strftime("%Y-%m-%d %I:%M %p")
        else:
            new_status = "Pending"
            completed_at = ""

        # Update status (col C) and completed_at (col E)
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"To-Do!C{target_row}", "values": [[new_status]]},
                    {"range": f"To-Do!E{target_row}", "values": [[completed_at]]},
                ],
            },
        ).execute()

        task_name = rows[target_row - 1][1] if len(rows[target_row - 1]) > 1 else "Unknown"
        return {"item_index": item_index, "task": task_name, "new_status": new_status}

    except Exception as e:
        logger.error(f"Update todo failed: {e}")
        return {"error": str(e)}


async def delete_todo_from_sheet(user_db, item_index: int) -> dict:
    """Delete a to-do item by its index number."""
    if not user_db or not user_db.google_token_json:
        return {"error": "GOOGLE_NOT_CONNECTED"}

    sheet_id = await _ensure_spreadsheet(user_db)
    svc = _get_sheets_service(user_db)
    if not sheet_id or not svc:
        return {"error": "Could not access Google Sheets."}

    try:
        await _ensure_todo_tab(svc, sheet_id)

        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="To-Do!A:E"
        ).execute()
        rows = result.get("values", [])

        # Find and clear the row (we clear content rather than delete to preserve indices)
        target_row = None
        task_name = ""
        for i, row in enumerate(rows[1:], start=2):
            if len(row) > 0 and str(row[0]) == str(item_index):
                target_row = i
                task_name = row[1] if len(row) > 1 else ""
                break

        if not target_row:
            return {"error": f"To-do item #{item_index} not found."}

        # Get the sheet ID (numeric) for the To-Do tab
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties").execute()
        todo_sheet_id = None
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == "To-Do":
                todo_sheet_id = s["properties"]["sheetId"]
                break

        if todo_sheet_id is not None:
            # Delete the entire row
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": todo_sheet_id,
                            "dimension": "ROWS",
                            "startIndex": target_row - 1,  # 0-indexed
                            "endIndex": target_row,
                        }
                    }
                }]},
            ).execute()

        return {"deleted": True, "item_index": item_index, "task": task_name}

    except Exception as e:
        logger.error(f"Delete todo failed: {e}")
        return {"error": str(e)}
