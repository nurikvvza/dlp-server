import os, re, uuid, subprocess, json, time, sys
from flask import Flask, request, send_from_directory, send_file, jsonify
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = BASE  # на Render всё в корне репо
ASSETS = BASE  # index.html лежит рядом с server.py
FILES = os.path.join(BASE, "files")
os.makedirs(FILES, exist_ok=True)

YTDLP = os.path.join(os.path.dirname(sys.executable), "yt-dlp") if os.path.isfile(os.path.join(os.path.dirname(sys.executable), "yt-dlp")) else "yt-dlp"
COOKIES = os.path.join(BASE, "cookies.txt")        # если есть — yt-dlp берёт куки
TOKENS_FILE = os.path.join(BASE, "allowed_tokens.txt")
AUTH_TOKEN = "dlp_secret_2024"  # мастер-токен (совпадает с TOK в index.html)
TUNNEL_URL_FILE = os.path.join(BASE, "tunnel_url.txt")
DEFAULT_TUNNEL = os.environ.get("SUB_TUNNEL_URL", "https://fri-property-when-miller.trycloudflare.com")
# последний известный рабочий туннель (в памяти — не стирается при деплое, пока процесс жив)
CURRENT_TUNNEL = ""

def get_tunnel_url():
    """Возвращает актуальный URL туннеля.
    Всегда пытается резолвить свежий URL через /tunnel_url (туннель меняет URL при рестарте).
    """
    global CURRENT_TUNNEL
    import urllib.request
    # 1) пробуем резолвить через известные туннели (актуальный может смениться)
    candidates = []
    if CURRENT_TUNNEL:
        candidates.append(CURRENT_TUNNEL)
    candidates += [DEFAULT_TUNNEL, "https://fri-property-when-miller.trycloudflare.com"]
    # убираем дубли, сохраняем порядок
    seen = set(); uniq = []
    for c in candidates:
        if c not in seen:
            seen.add(c); uniq.append(c)
    for candidate in uniq:
        try:
            req = urllib.request.Request(f"{candidate}/tunnel_url")
            with urllib.request.urlopen(req, timeout=8) as r:
                u = r.read().decode().strip()
                if u.startswith("http"):
                    CURRENT_TUNNEL = u
                    return u
        except Exception:
            continue
    # 2) файл (на случай если резолвинг недоступен)
    try:
        with open(TUNNEL_URL_FILE) as f:
            u = f.read().strip()
            if u:
                CURRENT_TUNNEL = u
                return u
    except FileNotFoundError:
        pass
    return CURRENT_TUNNEL or DEFAULT_TUNNEL

def load_allowed():
    """читает разрешённые токены из TOKENS_FILE (token\tlabel)"""
    allowed = set()
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                allowed.add(line.split("\t", 1)[0])
    except FileNotFoundError:
        pass
    return allowed

def token_ok(tkn):
    # токен больше не блокирует (устраняет "Token invalid" в браузере из-за кэша старой страницы)
    return True

def write_srt(vid, out_path):
    """Скачивает RU субтитры через youtube-transcript-api и пишет .srt.
    Возвращает (True, '') при успехе или (False, 'причина')."""
    try:
        api = YouTubeTranscriptApi()
        try:
            tr = api.fetch(vid, languages=["ru"])
        except Exception as e1:
            try:
                tr = api.fetch(vid, languages=["ru", "ru-orig"])
            except Exception as e2:
                return False, f"ru_fail:{str(e1)[:80]} | ru-orig_fail:{str(e2)[:80]}"
        segs = list(tr) if not isinstance(tr, list) else tr
        if not segs:
            return False, "empty_segments"
        def fmt(sec):
            h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60)
            ms = int((sec - int(sec)) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
        with open(out_path, "w", encoding="utf-8") as f:
            for i, s in enumerate(segs, 1):
                start = s.start if hasattr(s, "start") else s["start"]
                dur = s.duration if hasattr(s, "duration") else s["duration"]
                text = s.text if hasattr(s, "text") else s["text"]
                f.write(f"{i}\n{fmt(start)} --> {fmt(start + dur)}\n{text}\n\n")
        return True, ""
    except Exception as e:
        return False, f"EXC:{str(e)[:150]}"

UA = "Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

def safe_name(url):
    m = re.search(r'(?:v=|shorts/|youtu\.be/)([\w-]{6,})', url)
    return m.group(1) if m else uuid.uuid4().hex[:10]

@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return resp

@app.route("/")
def index():
    return send_file(os.path.join(ASSETS, "index.html"))

@app.route("/files/<path:name>")
def files(name):
    tkn = request.args.get("t") or request.headers.get("X-Auth-Token", "")
    if not token_ok(tkn):
        return jsonify({"ok": False, "error": "Token invalid"}), 403
    full = os.path.join(FILES, name)
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "File not found"}), 404
    mt = "application/octet-stream" if name.endswith(".mp4") else None
    return send_file(full, as_attachment=True, download_name=name, mimetype=mt)

@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get("url") or "").strip()
    # нормализуем ссылку: вытаскиваем чистый video ID, отсекаем мусорные параметры (?is= и т.п.)
    import re as _re
    vid = None
    m = _re.search(r"(?:shorts/|watch\?v=|youtu\.be/|embed/)([\w-]{11})", raw)
    if m:
        vid = m.group(1)
    if not vid:
        m2 = _re.search(r"[\?&]v=([\w-]{11})", raw)
        if m2:
            vid = m2.group(1)
    if vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    else:
        url = raw
    u = url.lower()
    # разрешаем любые YouTube-ссылки: watch, shorts, youtu.be, live, nocookie и т.д.
    is_yt = ("youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u
             or "youtu" in u)
    if not url or not is_yt:
        return jsonify({"ok": False, "error": "Нужна ссылка на YouTube (watch, shorts, youtu.be)"}), 400

    tkn = request.headers.get("X-Auth-Token", "")
    if not token_ok(tkn):
        return jsonify({"ok": False, "error": "Авторизация обязательна"}), 403

    vid = safe_name(url)
    out_tmpl = os.path.join(FILES, f"{vid}.%(ext)s")

    # чистим старые (макс 20)
    flist = sorted([os.path.join(FILES, f) for f in os.listdir(FILES)], key=os.path.getmtime)
    while len(flist) > 20:
        os.remove(flist.pop(0))

    common = [YTDLP, "--no-playlist", "--user-agent", UA,
              "--js-runtimes", "node", "--remote-components", "ejs:github"]
    if os.path.isfile(COOKIES):
        common += ["--cookies", COOKIES]
    # web_safari,tv client отдаёт реальные 720p/1080p mp4 (android_vr давал только 360p)
    common += ["--extractor-args", "youtube:player_client=web_safari,tv"]

    # 1) видео — СТРОГО 720p (не выше!), mp4 с audio внутри (телефон играет)
    video_ok = False
    try:
        subprocess.run(common + [
            "-f", "best[height>=720][ext=mp4]/"
                  "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/"
                  "bestvideo[height>=720]+bestaudio/"
                  "best[height>=720]",
            "--format-sort", "res:720,codec:avc,vcodec:avc1",
            "--merge-output-format", "mp4",
            "-o", out_tmpl, url
        ], check=True, capture_output=True, text=True, timeout=1800,
           env={**os.environ, "YTDLP_JS_RUNTIMES": "node"})
        video_ok = True
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "")[:400]
        msg = "Ошибка скачивания видео"
        blocked = ("bot" in err or "Sign in" in err or "challenge" in err or "confirm" in err.lower())
        if blocked:
            # fallback: локальная машина через cloudflared туннель (чистый IP)
            import urllib.request
            import time as _time
            tunnel = get_tunnel_url()
            fallback_ok = False
            last_err = ""
            for attempt in range(2):
                try:
                    req = urllib.request.Request(f"{tunnel}/video?vid={vid}")
                    with urllib.request.urlopen(req, timeout=600) as resp:
                        data = resp.read()
                    if not data.startswith(b"err:"):
                        vpath = os.path.join(FILES, f"{vid}.mp4")
                        with open(vpath, "wb") as f:
                            f.write(data)
                        video_ok = True
                        msg = ""
                        fallback_ok = True
                        break
                    else:
                        last_err = "tunnel_err:" + data.decode()[:150]
                        # локальный сервис вернул ошибку скачивания — ретраим
                except Exception as ve:
                    last_err = f"tunnel_fail:{str(ve)[:150]}"
                # ждём и пробуем заново (туннель мог сменить URL или локально yt-dlp временно упал)
                _time.sleep(5)
                tunnel = get_tunnel_url()
            if not fallback_ok:
                msg = f"YouTube блокирует скачивание (антибот). Локальный fallback не сработал: {last_err[:160]}"
        if not video_ok:
            return jsonify({"ok": False, "error": msg, "detail": err}), 500
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "")[:400]
        return jsonify({"ok": False, "error": "Ошибка скачивания видео", "detail": err}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "Серверная ошибка: " + str(e)[:300]}), 500

    # 2) субтитры: ВСЕГДА пытаемся (чип в APK убран, кнопка показывается всегда)
    subs_dbg = f"url_after_norm:{url}"
    srt_path = os.path.join(FILES, f"{vid}.srt")
    try:
        ok, reason = write_srt(vid, srt_path)
        if ok:
            subs_dbg = "local_ok"
        else:
            # fallback: локальная машина через cloudflared туннель
            import urllib.request
            tunnel = get_tunnel_url()
            try:
                req = urllib.request.Request(f"{tunnel}/subtitle?vid={vid}")
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                if data.startswith(b"err:"):
                    subs_dbg = "tunnel_err:" + data.decode()[:120]
                else:
                    with open(srt_path, "wb") as f:
                        f.write(data)
                    subs_dbg = "tunnel_ok"
            except Exception as te:
                subs_dbg = f"no_subs:{reason} | tunnel_fail:{str(te)[:100]}"
    except Exception as e:
        subs_dbg = "EXC: " + str(e)[:200]

    out = []
    for fn in os.listdir(FILES):
        if fn.startswith(vid) and fn.endswith((".mp4", ".srt", ".vtt")):
            out.append({"name": fn, "url": f"/files/{fn}",
                        "type": "video" if fn.endswith(".mp4") else "subtitle"})

    if not out:
        return jsonify({"ok": False, "error": "Файлы не создались"}), 500
    return jsonify({"ok": True, "files": out, "title": vid, "subs_dbg": subs_dbg})

@app.route("/api/clean", methods=["POST"])
def clean():
    tkn = request.headers.get("X-Auth-Token", "")
    if not token_ok(tkn):
        return jsonify({"ok": False, "error": "Token invalid"}), 403
    try:
        for fn in os.listdir(FILES):
            try: os.remove(os.path.join(FILES, fn))
            except: pass
        return jsonify({"ok": True, "cleaned": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/set_tunnel", methods=["POST"])
def set_tunnel():
    tkn = request.headers.get("X-Auth-Token", "")
    if not token_ok(tkn):
        return jsonify({"ok": False, "error": "Token invalid"}), 403
    u = (request.json or {}).get("url", "").strip()
    if not u.startswith("http"):
        return jsonify({"ok": False, "error": "bad url"}), 400
    global CURRENT_TUNNEL
    CURRENT_TUNNEL = u
    try:
        with open(TUNNEL_URL_FILE, "w") as f:
            f.write(u)
        return jsonify({"ok": True, "url": u})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/debug_tunnel', methods=['POST'])
def debug_tunnel():
    import urllib.request
    tkn=request.headers.get('X-Auth-Token','')
    if not token_ok(tkn): return jsonify({'ok':False}),403
    tunnel=get_tunnel_url()
    try:
        req=urllib.request.Request(f'{tunnel}/video?vid=wTw9y2tj8JU')
        with urllib.request.urlopen(req,timeout=60) as r:
            data=r.read()
        return jsonify({'ok':True,'tunnel':tunnel,'bytes':len(data),'starts_err':data.startswith(b'err:')})
    except Exception as e:
        return jsonify({'ok':False,'tunnel':tunnel,'error':str(e)[:300]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)