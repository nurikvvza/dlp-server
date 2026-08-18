import os, re, uuid, subprocess, json, time, sys
from flask import Flask, request, send_from_directory, send_file, jsonify

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = BASE  # на Render всё в корне репо
ASSETS = BASE  # index.html лежит рядом с server.py
FILES = os.path.join(BASE, "files")
os.makedirs(FILES, exist_ok=True)

YTDLP = os.path.join(os.path.dirname(sys.executable), "yt-dlp") if os.path.isfile(os.path.join(os.path.dirname(sys.executable), "yt-dlp")) else "yt-dlp"
COOKIES = os.path.join(BASE, "cookies.txt")        # если есть — yt-dlp берёт куки
TOKENS_FILE = os.path.join(BASE, "allowed_tokens.txt")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "dlp_secret_2024")  # запасной мастер-токен

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
    if not tkn:
        return False
    if tkn == AUTH_TOKEN:
        return True
    return tkn in load_allowed()

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
    return send_file(full, as_attachment=True, download_name=name)

@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
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

    # 1) видео — СТРОГО 720p HD, mp4 (H.264/AAC)
    try:
        subprocess.run(common + [
            "-f", "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/"
                  "bestvideo[height>=720]+bestaudio/"
                  "best[height>=720][ext=mp4]/"
                  "best[height>=720]",
            "--format-sort", "res:720,codec:avc,vcodec:avc1",
            "--merge-output-format", "mp4",
            "-o", out_tmpl, url
        ], check=True, capture_output=True, text=True, timeout=600,
           env={**os.environ, "YTDLP_JS_RUNTIMES": "node"})
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "")[:400]
        msg = "Ошибка скачивания видео"
        if "bot" in err or "Sign in" in err or "challenge" in err:
            msg = "YouTube требует куки (cookies.txt). Без них видео заблокировано антиботом."
        return jsonify({"ok": False, "error": msg, "detail": err}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": "Серверная ошибка: " + str(e)[:300]}), 500

    # 2) субтитры (ОТДЕЛЬНЫЕ аргументы — БЕЗ -f, БЕЗ кук, client android_vr качает auto-subs без impersonation)
    subs_dbg = ""
    try:
        sub_args = [YTDLP, "--no-playlist", "--user-agent", UA,
                    "--js-runtimes", "node"]
        sub_args += ["--extractor-args", "youtube:player_client=web_safari"]
        sub_args += ["--skip-download", "--write-subs", "--write-auto-subs",
                     "--sub-langs", "ru", "--convert-subs", "srt", "-o", out_tmpl, url]
        rs = subprocess.run(sub_args, capture_output=True, text=True, timeout=120,
                       env={**os.environ, "YTDLP_JS_RUNTIMES": "node"})
        subs_dbg = (rs.stderr or "")[-600:]
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)