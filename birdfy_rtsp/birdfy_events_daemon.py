#!/usr/bin/env python3
"""
Daemon: polls Birdfy events every POLL_INTERVAL seconds and writes
/config/birdfy_events.json for Home Assistant to consume.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import aiohttp

UCID = "b3cf543b57"
UDID = "android-10aa8cf1-d060-4333-b738-f541f07b65ae"
API_BASE  = "https://eu-central-1-api2.nvts.co"
LOGIN_URL = "https://localweb.nvts.co/v1/users/login/v2"

POLL_INTERVAL = 300  # seconds
MAX_EVENTS    = 10
MOTION_MAX_AGE_MS = 5 * 60 * 1000  # ignorer les événements plus vieux (rattrapage)
HA_EVENTS_URL = "http://supervisor/core/api/events"
DOWNLOAD_LINK_URL = "https://centralapi.nvts.co/event/downloadLink"
CLIPS_DIR  = "/config/www/birdfy_clips"   # servi par HA sous /local/birdfy_clips
KEEP_CLIPS = 3
PHOTOS_DIR = "/config/www/birdfy_visits"  # une photo IA par visite, /local/birdfy_visits
KEEP_PHOTOS = 5

_last_alarm_id = None  # None = premier poll, sert de référence sans déclencher
_last_image_url = ""
_pending_clips = {}    # alarm_id -> alert_time : clips à télécharger dès qu'ils sont prêts
_photos_sent = None    # dernière liste envoyée à HA (birdfy_recent_birds)

logging.basicConfig(
    level=logging.INFO,
    format="[birdfy-events] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def hmac_sha256(key: bytes, msg: str) -> str:
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def make_signature(token: str, userid: str, ts: str) -> str:
    k = "nvs1" + token
    for msg in [UCID, UDID, userid, ts]:
        k = hmac_sha256(k.encode(), msg)
    return hmac_sha256(k.encode(), "nvs1_request")


def auth_headers(token: str, userid: str) -> dict:
    ts = str(int(time.time() * 1000))
    sig = make_signature(token, userid, ts)
    return {
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
        "x-nvs-signature": sig,
        "x-nvs-time": ts,
        "x-nvs-ucid": UCID,
        "x-nvs-udid": UDID,
        "x-nvs-userid": userid,
        "x-nvs-version": '{"signature":2}',
    }


async def android_login(email: str, password: str) -> dict:
    pwd_md5 = hashlib.md5(password.encode()).hexdigest()
    payload = {"username": email, "password": pwd_md5, "locale": "en-US"}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-nvs-ucid": UCID,
        "x-nvs-udid": UDID,
        "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(LOGIN_URL, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if data.get("ret", 0) != 0:
                raise RuntimeError(f"Login failed: {data}")
            return data


async def get_events(token: str, userid: str, device_id: str) -> list:
    url = f"{API_BASE}/devices/{device_id}/events"
    params = {"limit": MAX_EVENTS, "ignoreAiLabels": "false", "reverse": 1}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid), params=params) as r:
            if r.status == 401:
                raise PermissionError("Token expired")
            data = await r.json(content_type=None)
            return data.get("events", [])


async def get_image_url(token: str, userid: str, alarm_id: str, device_id: str) -> str:
    url = f"{API_BASE}/devices/{device_id}/events/{alarm_id}/pic"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return data.get("url", "")
            return ""


async def fire_ha_event(event_type: str, data: dict) -> None:
    """Déclenche un événement Home Assistant via l'API Supervisor (homeassistant_api)."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        log.warning(f"SUPERVISOR_TOKEN absent — événement {event_type} non envoyé")
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{HA_EVENTS_URL}/{event_type}", json=data,
                              headers={"Authorization": f"Bearer {token}"}) as r:
                log.info(f"Événement HA {event_type} → {r.status}")
    except Exception as e:
        log.warning(f"Événement HA {event_type} échoué: {e}")


def visit_species(desc: dict) -> list:
    """Espèces reconnues par l'IA Birdfy dans le clip, dans l'ordre d'apparition."""
    names = []
    for frame in (desc.get("aiLabels") or {}).values():
        for label in frame:
            for obj in label.get("objects", []):
                name = obj.get("desc") or label.get("name", "")
                if name and name not in names:
                    names.append(name)
    return names


async def fetch_clip(token: str, userid: str, device_id: str, ev: dict) -> str:
    """Télécharge le MP4 de l'événement dans CLIPS_DIR ; renvoie son chemin /local, ou ''."""
    desc = json.loads(ev.get("description") or "{}")
    body = {"eventList": [{
        "serialnumber": device_id, "alarmId": ev["alarmId"], "dataGroup": ["video"],
        "recordDir": desc.get("recordDir", ""), "recordUrl": desc.get("recordUrl", ""),
    }]}
    async with aiohttp.ClientSession() as s:
        async with s.post(DOWNLOAD_LINK_URL, json=body, headers=auth_headers(token, userid)) as r:
            data = await r.json(content_type=None)
        files = ((data.get("eventList") or [{}])[0].get("videoFileList") or [])
        if not files:
            return ""
        async with s.get(files[0]) as r:
            if r.status != 200:
                return ""
            clip = await r.read()
    os.makedirs(CLIPS_DIR, exist_ok=True)
    name = f"{ev['alarmId']}.mp4"
    tmp = os.path.join(CLIPS_DIR, name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(clip)
    os.replace(tmp, os.path.join(CLIPS_DIR, name))
    clips = sorted((f for f in os.listdir(CLIPS_DIR) if f.endswith(".mp4")),
                   key=lambda f: os.path.getmtime(os.path.join(CLIPS_DIR, f)), reverse=True)
    for old in clips[KEEP_CLIPS:]:
        os.remove(os.path.join(CLIPS_DIR, old))
    return f"/local/birdfy_clips/{name}"


async def clip_duration(path: str) -> float:
    """Durée du clip en secondes (ffprobe) ; 20 s si illisible, la durée habituelle."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 20.0


def best_frame(desc: dict):
    """(timestamp, espèce, centre "x% y%") de l'image IA où l'oiseau est le plus grand."""
    best = None
    for ts, frame in (desc.get("aiLabels") or {}).items():
        for label in frame:
            for obj in label.get("objects", []):
                area = obj.get("width", 0) * obj.get("height", 0)
                if best is None or area > best[0]:
                    focus = (f"{round((obj.get('left', 0) + obj.get('width', 0) / 2) * 100)}% "
                             f"{round((obj.get('top', 0) + obj.get('height', 0) / 2) * 100)}%")
                    best = (area, ts, obj.get("desc") or label.get("name", ""), focus)
    return best[1:] if best else (None, "", "")


async def fetch_photo(token: str, userid: str, device_id: str, ev: dict, frame_ts: str) -> bool:
    """Télécharge l'image IA frame_ts de l'événement dans PHOTOS_DIR."""
    desc = json.loads(ev.get("description") or "{}")
    body = {"eventList": [{
        "serialnumber": device_id, "alarmId": ev["alarmId"], "dataGroup": ["pic"],
        "recordDir": desc.get("recordDir", ""), "recordUrl": desc.get("recordUrl", ""),
        "pic": ev.get("pic", ""), "aiRecordDir": desc.get("aiRecordDir", ""),
    }]}
    async with aiohttp.ClientSession() as s:
        async with s.post(DOWNLOAD_LINK_URL, json=body, headers=auth_headers(token, userid)) as r:
            data = await r.json(content_type=None)
        pics = ((data.get("eventList") or [{}])[0].get("picList") or [])
        url = next((p for p in pics if f"_{frame_ts}_" in p), pics[0] if pics else "")
        if not url:
            return False
        async with s.get(url) as r:
            if r.status != 200:
                return False
            photo = await r.read()
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    path = os.path.join(PHOTOS_DIR, f"{ev['alarmId']}.jpg")
    with open(path + ".tmp", "wb") as f:
        f.write(photo)
    os.replace(path + ".tmp", path)
    return True


async def refresh_photos(token: str, userid: str, device_id: str, events_raw: list) -> None:
    """Garde une photo des KEEP_PHOTOS dernières visites d'oiseaux et publie la liste à HA."""
    global _photos_sent
    birds = []
    for ev in events_raw:
        if len(birds) == KEEP_PHOTOS:
            break
        if not ev.get("isReady"):
            continue
        frame_ts, species, focus = best_frame(json.loads(ev.get("description") or "{}"))
        if frame_ts is None:  # mouvement sans oiseau reconnu
            continue
        name = f"{ev['alarmId']}.jpg"
        if not os.path.exists(os.path.join(PHOTOS_DIR, name)):
            try:
                if not await fetch_photo(token, userid, device_id, ev, frame_ts):
                    continue
            except Exception as e:
                log.warning(f"Photo {ev['alarmId']} échouée: {e}")
                continue
        birds.append({"key": ev["alarmId"], "file": name, "species": species, "focus": focus,
                      "time": ev.get("alertTime", 0), "category": "visit"})
    if not birds:
        return
    keep = {b["file"] for b in birds}
    for f in os.listdir(PHOTOS_DIR):
        if f.endswith(".jpg") and f not in keep:
            os.remove(os.path.join(PHOTOS_DIR, f))
    if birds != _photos_sent:
        await fire_ha_event("birdfy_recent_birds", {"birds": birds})
        _photos_sent = birds


def parse_event(ev: dict) -> dict:
    alarm_id   = ev.get("alarmId", "")
    alert_time = ev.get("alertTime", 0)
    label      = ev.get("label", "")
    desc_raw   = ev.get("description", "{}")
    record_url = ""
    try:
        desc = json.loads(desc_raw)
        record_url = desc.get("recordUrl", "")
    except Exception:
        pass
    return {
        "alarm_id":   alarm_id,
        "alert_time": alert_time,
        "label":      label,
        "record_url": record_url,
    }


async def load_session(session_file: str, android_session_file: str, email: str, password: str):
    """Return (token, userid, device_id), re-logging in if needed."""
    token = userid = ""

    if os.path.exists(android_session_file):
        with open(android_session_file) as f:
            s = json.load(f)
        if s.get("token") and s.get("userID"):
            token  = s["token"]
            userid = str(s["userID"])

    if not token and email and password:
        log.info("Android login...")
        s = await android_login(email, password)
        with open(android_session_file, "w") as f:
            json.dump(s, f, indent=2)
        token  = s["token"]
        userid = str(s["userID"])

    with open(session_file) as f:
        web = json.load(f)
    devices = web.get("devices", [])
    if not devices:
        raise RuntimeError("No devices in session file")
    device_id = (
        devices[0].get("serialNumber")
        or devices[0].get("deviceSn")
        or devices[0].get("sn")
        or devices[0].get("addxSn")
    )
    return token, userid, device_id


async def poll_once(token: str, userid: str, device_id: str, output_file: str,
                    email: str, password: str, android_session_file: str,
                    session_file: str) -> tuple[str, str]:
    """Fetch events and write JSON. Returns (token, userid) possibly refreshed."""
    try:
        events_raw = await get_events(token, userid, device_id)
    except PermissionError:
        log.warning("Token expired, re-logging in...")
        if os.path.exists(android_session_file):
            os.remove(android_session_file)
        token, userid, _ = await load_session(session_file, android_session_file, email, password)
        events_raw = await get_events(token, userid, device_id)

    events = [parse_event(e) for e in events_raw]
    last = events[0] if events else {}

    # Nouvel événement : image (une seule fois par alarme), puis clip à télécharger
    global _last_alarm_id, _last_image_url
    if last and last["alarm_id"] != _last_alarm_id:
        _last_image_url = await get_image_url(token, userid, last["alarm_id"], device_id)
        if _last_alarm_id is not None and time.time() * 1000 - last["alert_time"] < MOTION_MAX_AGE_MS:
            _pending_clips[last["alarm_id"]] = last["alert_time"]
        _last_alarm_id = last["alarm_id"]

    # Clip prêt (isReady) → téléchargé dans /config/www, puis événement HA birdfy_motion
    raw_by_id = {e.get("alarmId"): e for e in events_raw}
    for alarm_id, alert_time in list(_pending_clips.items()):
        ev = raw_by_id.get(alarm_id)
        if time.time() * 1000 - alert_time > MOTION_MAX_AGE_MS or ev is None:
            log.warning(f"Clip {alarm_id} jamais prêt — abandonné")
            del _pending_clips[alarm_id]
            continue
        if not ev.get("isReady"):
            continue
        try:
            clip = await fetch_clip(token, userid, device_id, ev)
        except Exception as e:
            log.warning(f"Téléchargement du clip {alarm_id} échoué: {e}")
            continue
        if not clip:
            continue
        del _pending_clips[alarm_id]
        species = visit_species(json.loads(ev.get("description") or "{}"))
        duration = await clip_duration(os.path.join(CLIPS_DIR, os.path.basename(clip)))
        log.info(f"Clip prêt {clip} {duration:.0f}s ({', '.join(species) or 'espèce inconnue'})")
        await fire_ha_event("birdfy_motion", {"alarm_id": alarm_id, "alert_time": alert_time,
                                              "clip": clip, "species": species,
                                              "duration": duration})

    await refresh_photos(token, userid, device_id, events_raw)
    if last:
        last["image_url"] = _last_image_url

    output = {
        "last_updated":     int(time.time()),
        "last_event":       last,
        "recent_events":    events,
    }

    tmp = output_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp, output_file)
    log.info(f"Wrote {len(events)} events → {output_file}  (last: {last.get('label','?')} @ {last.get('alert_time','')})")
    return token, userid


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--session",          default="/config/birdfy_session.json")
    p.add_argument("--android-session",  default="/config/birdfy_android_session.json")
    p.add_argument("--output",           default="/config/birdfy_events.json")
    p.add_argument("--email",            default=os.environ.get("BIRDFY_EMAIL", ""))
    p.add_argument("--password",         default=os.environ.get("BIRDFY_PASSWORD", ""))
    p.add_argument("--interval",         type=int, default=POLL_INTERVAL)
    args = p.parse_args()

    token, userid, device_id = await load_session(
        args.session, args.android_session, args.email, args.password
    )
    log.info(f"Device: {device_id}  user: {userid}")

    while True:
        try:
            token, userid = await poll_once(
                token, userid, device_id,
                args.output, args.email, args.password,
                args.android_session, args.session,
            )
        except Exception as e:
            log.error(f"Poll error: {e}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
