#!/usr/bin/env python3
"""
AirTube — YouTube Downloader API (Termux)
──────────────────────────────────────────
Search YouTube, list available qualities, download best-merged mp4 (or audio),
save to phone storage, and optionally push the file to Telegram.

Endpoints:
  GET  /                          → status
  GET  /search?q=...              → search results
  GET  /formats?id=VIDEO_ID       → available resolutions for a video
  POST /download                  → start a download (json body, see below)
       body: {"id":"VIDEO_ID","mode":"video|audio","height":1080,"telegram":false}
  GET  /progress?job=JOB_ID       → download progress / status / result path
  GET  /file?job=JOB_ID           → download the finished file to the browser
  GET  /downloads                 → list finished files
  GET  /resolve?id=&mode=         → (kept) direct stream url for in-app preview

Run on Termux:
  pkg install python ffmpeg
  pip install flask yt-dlp requests
  termux-setup-storage          # grants /sdcard access (one time)
  python server.py

Telegram (optional) — set before running:
  export TG_BOT_TOKEN="123456:ABC..."
  export TG_CHAT_ID="-1001234567890"   # channel/chat to receive files
"""

import os, json, uuid, threading, subprocess
from urllib.parse import quote, unquote

from flask import Flask, request, Response, jsonify, send_file, stream_with_context
import requests

try:
    from yt_dlp import YoutubeDL
except ImportError:
    raise SystemExit("Run: pip install yt-dlp flask requests")

PORT = int(os.environ.get("PORT", 8080))
UA = ("Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")

# Save location: phone storage if available, else local folder.
SDCARD = "/sdcard/Download/AirTube"
LOCAL  = os.path.expanduser("~/airtube/downloads")
SAVE_DIR = "/tmp/downloads"
os.makedirs(SAVE_DIR, exist_ok=True)

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "")

app = Flask(__name__)
JOBS = {}   # job_id -> {status, percent, speed, eta, title, path, error}

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,HEAD,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Range,Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Length,Content-Range,Accept-Ranges"
    return resp


def ydl_base(extra=None):
    o = {
        "quiet": True, "no_warnings": True, "nocheckcertificate": True,
        "user_agent": UA,
        # tv + mweb currently expose the full resolution ladder WITHOUT needing
        # a PO token. web/tv_embedded often return nothing (token-gated) and
        # silently fall back to android's 360p — which is the bug you saw.
        "extractor_args": {"youtube": {
            "player_client": ["tv", "mweb", "android_vr", "web_safari"],
        }},
    }
    if extra: o.update(extra)
    return o


def fmt_duration(sec):
    if not sec: return ""
    sec = int(sec); h, m, s = sec//3600, (sec%3600)//60, sec%60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(b):
    if not b: return ""
    for u in ["B","KB","MB","GB"]:
        if b < 1024: return f"{b:.0f}{u}" if u=="B" else f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}TB"


# ───────────────────── search ─────────────────────
@app.route("/search")
def search():
    q = request.args.get("q","").strip()
    n = min(int(request.args.get("n",20)), 40)
    if not q: return jsonify({"status":"error","message":"Missing q"}), 400
    try:
        with YoutubeDL(ydl_base({"extract_flat": True, "skip_download": True})) as ydl:
            data = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
        out = []
        for e in (data.get("entries") or []):
            if not e: continue
            vid = e.get("id")
            out.append({
                "id": vid,
                "title": e.get("title") or "Untitled",
                "channel": e.get("channel") or e.get("uploader") or "",
                "duration": fmt_duration(e.get("duration")),
                "thumbnail": e.get("thumbnail") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None),
            })
        return jsonify({"status":"success","query":q,"count":len(out),"results":out})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


# ───────────────────── formats ─────────────────────
@app.route("/formats")
def formats():
    vid = request.args.get("id","").strip()
    if not vid: return jsonify({"status":"error","message":"Missing id"}), 400
    try:
        with YoutubeDL(ydl_base({"skip_download": True})) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        seen = {}
        for f in info.get("formats", []):
            h = f.get("height")
            if not h or f.get("vcodec") == "none":
                continue
            size = f.get("filesize") or f.get("filesize_approx") or 0
            fps = f.get("fps") or 0
            # keep the entry with the best known size per height
            if h not in seen or size > seen[h]["_size"]:
                seen[h] = {"height": h, "label": f"{h}p" + (f"{int(fps)}" if fps and fps > 30 else ""),
                           "_size": size, "size": human_size(size) if size else ""}
        heights = sorted(seen.values(), key=lambda x: -x["height"])
        for x in heights: x.pop("_size", None)
        return jsonify({
            "status":"success","id":vid,
            "title": info.get("title"),
            "channel": info.get("channel") or info.get("uploader"),
            "duration": fmt_duration(info.get("duration")),
            "thumbnail": info.get("thumbnail"),
            "qualities": heights,   # [{height, label, size}]
        })
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


# ───────────────────── download ─────────────────────
def run_download(job_id, vid, mode, height, to_telegram):
    job = JOBS[job_id]
    def hook(d):
        if d["status"] == "downloading":
            job["status"] = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job["percent"] = round(done/total*100, 1) if total else 0
            job["speed"] = human_size(d.get("speed") or 0) + "/s"
            job["eta"] = d.get("eta")
        elif d["status"] == "finished":
            job["status"] = "processing"   # ffmpeg merge/convert step
            job["percent"] = 100

    if mode == "audio":
        fmt = "bestaudio/best"
        postproc = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"320"}]
        outtmpl = os.path.join(SAVE_DIR, "%(title).80s [%(id)s].%(ext)s")
    else:
        if height:
            fmt = (f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
                   f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best")
        else:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        postproc = [{"key":"FFmpegVideoRemuxer","preferedformat":"mp4"}]
        outtmpl = os.path.join(SAVE_DIR, "%(title).80s [%(id)s].%(ext)s")

    opts = ydl_base({
        "format": fmt,
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
        "postprocessors": postproc,
        "merge_output_format": "mp4" if mode != "audio" else None,
    })
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            path = ydl.prepare_filename(info)
            # fix extension after postprocessing
            if mode == "audio":
                path = os.path.splitext(path)[0] + ".mp3"
            else:
                base = os.path.splitext(path)[0]
                path = base + ".mp4" if os.path.exists(base + ".mp4") else path
        job["path"] = path
        job["title"] = info.get("title")
        job["status"] = "done"
        job["percent"] = 100

        if to_telegram:
            job["status"] = "uploading"
            ok, msg = send_to_telegram(path, info.get("title",""))
            job["telegram"] = msg
            job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def send_to_telegram(path, caption):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False, "TG_BOT_TOKEN / TG_CHAT_ID not set"
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendDocument"
        with open(path, "rb") as fh:
            r = requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": caption[:1000]},
                              files={"document": fh}, timeout=600)
        ok = r.ok and r.json().get("ok")
        return ok, ("sent" if ok else f"failed: {r.text[:200]}")
    except Exception as e:
        return False, f"error: {e}"


@app.route("/download", methods=["POST","OPTIONS"])
def download():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(force=True, silent=True) or {}
    vid = (body.get("id") or "").strip()
    mode = body.get("mode","video")
    height = body.get("height")  # int or None
    to_tg = bool(body.get("telegram"))
    if not vid: return jsonify({"status":"error","message":"Missing id"}), 400

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status":"queued","percent":0,"speed":"","eta":None,
                    "title":None,"path":None,"error":None}
    t = threading.Thread(target=run_download, args=(job_id, vid, mode, height, to_tg), daemon=True)
    t.start()
    return jsonify({"status":"success","job":job_id})


@app.route("/progress")
def progress():
    job = request.args.get("job","")
    if job not in JOBS: return jsonify({"status":"error","message":"Unknown job"}), 404
    j = JOBS[job]
    fname = os.path.basename(j["path"]) if j.get("path") else None
    return jsonify({"status":"success","job":job,"state":j["status"],
                    "percent":j["percent"],"speed":j["speed"],"eta":j["eta"],
                    "title":j["title"],"file":fname,"error":j["error"],
                    "telegram":j.get("telegram")})


@app.route("/file")
def file_dl():
    job = request.args.get("job","")
    if job not in JOBS or not JOBS[job].get("path"):
        return jsonify({"status":"error","message":"File not ready"}), 404
    path = JOBS[job]["path"]
    if not os.path.exists(path):
        return jsonify({"status":"error","message":"File missing"}), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route("/downloads")
def list_downloads():
    files = []
    for f in sorted(os.listdir(SAVE_DIR), key=lambda x: -os.path.getmtime(os.path.join(SAVE_DIR,x))):
        p = os.path.join(SAVE_DIR, f)
        if os.path.isfile(p):
            files.append({"name": f, "size": human_size(os.path.getsize(p))})
    return jsonify({"status":"success","dir":SAVE_DIR,"count":len(files),"files":files})


# ───────────────── resolve (preview streaming, optional) ─────────────────
@app.route("/resolve")
def resolve():
    vid = request.args.get("id","").strip()
    mode = request.args.get("mode","video")
    if not vid: return jsonify({"status":"error","message":"Missing id"}), 400
    fmt = ("bestaudio[ext=m4a]/bestaudio" if mode=="audio"
           else "best[ext=mp4][acodec!=none][vcodec!=none][height<=720]/best[ext=mp4]/best")
    try:
        with YoutubeDL(ydl_base({"format":fmt,"skip_download":True})) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        u = info.get("url")
        if not u and info.get("requested_formats"):
            u = info["requested_formats"][0].get("url")
        origin = request.host_url.rstrip("/")
        return jsonify({"status":"success","id":vid,"mode":mode,
                        "title":info.get("title"),
                        "thumbnail":info.get("thumbnail"),
                        "direct_url":u,
                        "proxy_url":f"{origin}/proxy?url={quote(u,safe='')}" if u else None})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


@app.route("/proxy")
def proxy():
    target = request.args.get("url","")
    if not target: return jsonify({"status":"error","message":"Missing url"}), 400
    target = unquote(target)
    hdr = {"User-Agent": UA}
    if request.headers.get("Range"): hdr["Range"] = request.headers["Range"]
    try:
        up = requests.get(target, headers=hdr, stream=True, timeout=30)
    except Exception as e:
        return jsonify({"status":"error","message":f"Proxy failed: {e}"}), 502
    excl = {"content-encoding","transfer-encoding","connection","keep-alive"}
    h = {k:v for k,v in up.headers.items() if k.lower() not in excl}
    h["Accept-Ranges"] = "bytes"
    h.setdefault("Content-Type","video/mp4")
    def gen():
        for c in up.iter_content(262144):
            if c: yield c
    return Response(stream_with_context(gen()), status=up.status_code, headers=h)


@app.route("/debug")
def debug_formats():
    vid = request.args.get("id","").strip()
    if not vid: return jsonify({"status":"error","message":"Missing id"}), 400
    try:
        with YoutubeDL(ydl_base({"skip_download": True})) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
        raw = []
        for f in info.get("formats", []):
            raw.append({
                "id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "vcodec": (f.get("vcodec") or "")[:12],
                "acodec": (f.get("acodec") or "")[:12],
                "note": f.get("format_note"),
            })
        return jsonify({"status":"success","total":len(raw),"formats":raw})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


@app.route("/")
def root():
    try:
        ver = subprocess.check_output(["yt-dlp","--version"], text=True).strip()
    except Exception:
        from yt_dlp.version import __version__ as ver
    return jsonify({
        "status":"ok","service":"AirTube YouTube Downloader",
        "yt_dlp_version":ver,"save_dir":SAVE_DIR,
        "telegram_ready": bool(TG_BOT_TOKEN and TG_CHAT_ID),
        "endpoints":{
            "search":"/search?q=QUERY",
            "formats":"/formats?id=VIDEO_ID",
            "download":"POST /download {id,mode,height,telegram}",
            "progress":"/progress?job=JOB_ID",
            "file":"/file?job=JOB_ID",
            "downloads":"/downloads",
        }})


if __name__ == "__main__":
    print(f"AirTube Downloader on http://localhost:{PORT}")
    print(f"Saving to: {SAVE_DIR}")
    print(f"Telegram: {'ready' if (TG_BOT_TOKEN and TG_CHAT_ID) else 'not configured'}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
