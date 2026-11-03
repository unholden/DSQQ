"""Reconcile uncertain OneBot sends against server-sequenced image history."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

CHECK_DELAYS = (0, 2, 4, 8, 12, 16, 20)
IMAGE_NAME = re.compile(r"dsqq-image-[a-f0-9]{32}\.(png|jpg|webp)")


def md5_file(path):
    digest = hashlib.md5()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def target_for(event):
    group = str(event.get_group_id() or "")
    return {"kind": "group" if group else "private", "target_id": group or str(event.get_sender_id()),
            "self_id": str(event.get_self_id())}


def history_rows(payload):
    if not isinstance(payload, dict):
        return []
    if "messages" not in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    rows = payload.get("messages", [])
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


async def fetch_history(event, target):
    group = target["kind"] == "group"
    return await asyncio.wait_for(event.bot.call_action(
        "get_group_msg_history" if group else "get_friend_msg_history",
        **{("group_id" if group else "user_id"): target["target_id"],
           "count": 50, "disable_get_url": True, "parse_mult_msg": False}), timeout=8)


async def prepare_delivery(event, path):
    target = target_for(event)
    digest = await asyncio.to_thread(md5_file, path)
    try:
        before = {str(r.get("message_id")) for r in history_rows(await fetch_history(event, target))}
    except Exception:
        before = set()
    return {**target, "digest": digest, "size": Path(path).stat().st_size,
            "before_ids": before, "started_at": time.time()}


def matching_message(payload, ticket):
    for row in history_rows(payload):
        try:
            stamp = float(row.get("time", 0))
            if stamp > 1e12:
                stamp /= 1000
            # A local outgoing/pending record is NOT enough: require a server sequence.
            if int(row.get("real_seq", 0)) <= 0 or stamp < ticket["started_at"] - 2:
                continue
            if not row.get("message_id") or str(row["message_id"]) in ticket["before_ids"]:
                continue
            if str((row.get("sender") or {}).get("user_id", row.get("user_id"))) != ticket["self_id"]:
                continue
            if row.get("message_type") != ticket["kind"]:
                continue
            if ticket["kind"] == "group" and str(row.get("group_id")) != ticket["target_id"]:
                continue
            if ticket["kind"] == "private" and row.get("target_id") is not None and str(row["target_id"]) != ticket["target_id"]:
                continue
            if row.get("message_sent_type") != "self" and row.get("post_type") != "message_sent":
                continue
            for segment in row.get("message", []):
                if not isinstance(segment, dict) or segment.get("type") != "image":
                    continue
                data = segment.get("data") or {}
                name = str(data.get("md5") or data.get("file", "")).lower()
                match = re.fullmatch(r"([a-f0-9]{32})(?:\.[a-z0-9]+)?", name)
                if not match or match[1] != ticket["digest"]:
                    continue
                if data.get("file_size") is not None and int(data["file_size"]) != ticket["size"]:
                    continue
                return row
        except (TypeError, ValueError, AttributeError):
            continue
    return None


async def reconcile_delivery(event, ticket):
    for delay in CHECK_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            match = matching_message(await fetch_history(event, ticket), ticket)
            if match:
                return match
        except Exception:
            pass  # A history outage must never cause an automatic duplicate send.
    return None


def receipt_path(folder, event):
    identity = str(event.unified_msg_origin) + ":" + str(event.get_sender_id())
    return Path(folder) / ("receipt-" + hashlib.sha256(identity.encode()).hexdigest() + ".json")


def save_receipt(folder, event, image, status, message_id=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".receipt-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"image": Path(image).name, "status": status, "message_id": message_id,
                       "updated_at": time.time()}, stream)
        os.replace(temp, receipt_path(folder, event))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def latest_image(folder, event):
    try:
        data = json.loads(receipt_path(folder, event).read_text())
        name = data.get("image", "")
        if not isinstance(name, str) or not IMAGE_NAME.fullmatch(name):
            return None
        path = Path(folder) / name
        if path.is_file() and path.resolve().parent == Path(folder).resolve():
            return path
    except (OSError, ValueError, AttributeError):
        pass
    return None
