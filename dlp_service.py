import os, re, json, urllib.parse, http.server, socketserver, subprocess, sys, time

CACHE = "/home/administrator/dlp_cache"
os.makedirs(CACHE, exist_ok=True)
YTDLP = "/home/administrator/.local/bin/yt-dlp"
COOKIES = "/home/administrator/dlp-server/cookies.txt"
AUTH_TOKEN = "dlp_secret_2024"
UA = "Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

def safe_vid(v):
    return re.match(r"^[\w-]{6,}$", v) is not None

def normalize_url(raw):
    raw = (raw or "").strip()
    m = re.search(r"(?:v=|shorts/|youtu\.be/)([\w-]{11})", raw)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}", m.group(1)
    m2 = re.search(r"([\w-]{11})", raw)
    if m2:
        return f"https://www.youtube.com/watch?v={m2.group(1)}", m2.group(1)
    return None, None

def download_video(vid):
    out_tmpl = os.path.join(CACHE, f"{vid}.%(ext)s")
    cmd = [YTDLP, "--no-playlist", "--user-agent", UA,
           "--js-runtimes", "node", "--remote-components", "ejs:github"]
    if os.path.isfile(COOKIES):
        cmd += ["--cookies", COOKIES]
    cmd += ["--extractor-args", "youtube:player_client=web_safari,tv",
            "-f", "best[height>=720][ext=mp4]/bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio/best[height>=720]",
            "--format-sort", "res:720,codec:avc,vcodec:avc1",
            "--merge-output-format", "mp4", "-o", out_tmpl,
            f"https://www.youtube.com/watch?v={vid}"]
    env = {**os.environ, "YTDLP_JS_RUNTIMES": "node"}
    last_err = ""
    for attempt in range(2):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if r.returncode == 0 and any(f.startswith(vid) and f.endswith(".mp4") for f in os.listdir(CACHE)):
            for f in os.listdir(CACHE):
                if f.startswith(vid) and f.endswith(".mp4"):
                    return os.path.join(CACHE, f), vid
        last_err = (r.stderr or r.stdout)[-400:]
        time.sleep(5)
    raise Exception(last_err)

def write_srt(vid, path):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    try:
        tr = api.fetch(vid, languages=["ru"])
    except Exception:
        tr = api.fetch(vid, languages=["ru", "ru-orig"])
    segs = list(tr)
    def fmt(sec):
        h=int(sec//3600); m=int((sec%3600)//60); s=int(sec%60); ms=int((sec-int(sec))*1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            st = s.start if hasattr(s,"start") else s["start"]
            du = s.duration if hasattr(s,"duration") else s["duration"]
            tx = s.text if hasattr(s,"text") else s["text"]
            f.write(f"{i}\n{fmt(st)} --> {fmt(st+du)}\n{tx}\n\n")
    return True, ""

class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, data, ctype="application/octet-stream", name=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path in ("/", "/index.html", ""):
            # отдаём UI (тот же index.html, что в APK)
            idx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            if not os.path.isfile(idx):
                idx = "/home/administrator/projects/yt-dlp-app/android/assets/index.html"
            try:
                with open(idx, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(404, b"no ui")
            return
        if p.path == "/tunnel_url":
            try:
                with open("/tmp/tunnel_url.txt") as f:
                    self._send(200, f.read().strip().encode())
            except Exception:
                self._send(404, b"no url")
            return
        if p.path == "/video":
            qs = urllib.parse.parse_qs(p.query)
            vid = (qs.get("vid") or [""])[0]
            if not safe_vid(vid):
                self._send(400, b"bad vid"); return
            try:
                path, _ = download_video(vid)
                with open(path, "rb") as f:
                    self._send(200, f.read(), "video/mp4", f"{vid}.mp4")
            except Exception as e:
                self._send(500, ("err:" + str(e)[:400]).encode())
            return
        if p.path == "/subtitle":
            qs = urllib.parse.parse_qs(p.query)
            vid = (qs.get("vid") or [""])[0]
            if not safe_vid(vid):
                self._send(400, b"bad vid"); return
            try:
                out = os.path.join(CACHE, f"{vid}.srt")
                ok, reason = write_srt(vid, out)
                if ok:
                    with open(out, "rb") as f:
                        self._send(200, f.read(), "application/x-subrip", f"{vid}.srt")
                else:
                    self._send(500, ("err:" + reason).encode())
            except Exception as e:
                self._send(500, ("err:" + str(e)[:300]).encode())
            return
        self._send(404, b"not found")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/download":
            try:
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                body = {}
            tkn = self.headers.get("X-Auth-Token", "")
            if tkn != AUTH_TOKEN:
                self._send(403, json.dumps({"ok": False, "error": "Token invalid"}).encode(), "application/json")
                return
            raw = body.get("url", "")
            url, vid = normalize_url(raw)
            if not vid:
                self._send(400, json.dumps({"ok": False, "error": "bad url"}).encode(), "application/json")
                return
            files = []
            vpath = os.path.join(CACHE, f"{vid}.mp4")
            try:
                path, _ = download_video(vid)
                files.append({"name": f"{vid}.mp4", "url": f"/files/{vid}.mp4", "type": "video"})
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": "Ошибка скачивания видео", "detail": str(e)[:400]}).encode(), "application/json")
                return
            # субтитры
            srt = os.path.join(CACHE, f"{vid}.srt")
            try:
                write_srt(vid, srt)
                files.append({"name": f"{vid}.srt", "url": f"/files/{vid}.srt", "type": "subtitle"})
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True, "files": files}).encode(), "application/json")
            return
        if p.path == "/files" or p.path.startswith("/files/"):
            fn = p.path.split("/")[-1]
            fp = os.path.join(CACHE, fn)
            if os.path.isfile(fp) and safe_vid(fn.split(".")[0]):
                with open(fp, "rb") as f:
                    ctype = "video/mp4" if fn.endswith(".mp4") else "application/x-subrip"
                    self._send(200, f.read(), ctype, fn)
            else:
                self._send(404, b"not found")
            return
        self._send(404, b"not found")

    def log_message(self, *a): pass

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("127.0.0.1", 8911), H) as httpd:
    httpd.serve_forever()
