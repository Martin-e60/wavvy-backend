import asyncio
import os
import tempfile
import time
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "Range"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"],
)

_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 240

# Optional cookies via env var (Railway secret). Harmless when unset.
_cookie_path: str | None = None


def _cookie_file() -> str | None:
    global _cookie_path
    if _cookie_path:
        return _cookie_path
    data = os.environ.get("YT_COOKIES")
    if not data:
        return None
    data = data.strip()
    if "# Netscape" not in data and "\t" not in data:
        try:
            import base64
            decoded = base64.b64decode(data, validate=True).decode("utf-8")
            if "# Netscape" in decoded or "\t" in decoded:
                data = decoded
        except Exception:
            pass
    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if data.endswith("\n") else data + "\n")
    _cookie_path = path
    return path


# ── The known-good resolver: android-first client + this exact format
# string + yt-dlp's own selected URL. This is the configuration that
# played on iPhone. Do not "improve" it.
def _ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/best[ext=mp4]",
        "extractor_args": {
            "youtube": {"player_client": ["android", "ios", "web"]}
        },
    }
    cf = _cookie_file()
    if cf:
        opts["cookiefile"] = cf
    return opts


def _resolve(video_id: str) -> str | None:
    now = time.time()
    if video_id in _cache:
        url, ts = _cache[video_id]
        if now - ts < CACHE_TTL:
            return url

    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )

    url = info.get("url")
    if not url:
        m4a = [
            f for f in info.get("formats", [])
            if f.get("url") and (f.get("acodec") or "").startswith("mp4a")
        ]
        if m4a:
            m4a.sort(key=lambda f: f.get("abr") or 0, reverse=True)
            url = m4a[0]["url"]

    if url:
        _cache[video_id] = (url, now)
    return url


def _debug(video_id: str) -> dict:
    # process=False skips yt-dlp's format SELECTION, so it never raises
    # "Requested format is not available" — we get the raw inventory of
    # every format YouTube actually returned (with cookies applied).
    opts = _ydl_opts()
    opts.pop("format", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=False,
            process=False,
        )
    out = []
    for f in info.get("formats", []):
        out.append({
            "id": f.get("format_id"),
            "ext": f.get("ext"),
            "acodec": f.get("acodec"),
            "vcodec": f.get("vcodec"),
            "abr": f.get("abr"),
        })
    return {"count": len(out), "formats": out}


def _search(query: str) -> list:
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "noplaylist": True,
    }
    cf = _cookie_file()
    if cf:
        ydl_opts["cookiefile"] = cf
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        results = []
        for entry in info.get("entries", []):
            duration = entry.get("duration")
            if duration:
                m, s = divmod(int(duration), 60)
                dur_str = f"{m}:{s:02d}"
            else:
                dur_str = ""
            vid = entry.get("id")
            results.append({
                "id": vid,
                "title": entry.get("title"),
                "channel": entry.get("channel") or entry.get("uploader", ""),
                "duration": dur_str,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            })
        return results


@app.get("/search")
async def search(q: str):
    if not q.strip():
        return []
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _search, q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/preload/{video_id}")
async def preload(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _resolve, video_id)
        return {"ok": True}
    except Exception:
        return {"ok": False}


@app.get("/debug/{video_id}")
async def debug(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _debug, video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── The known-good proxy: simple single-request stream, force the
# iOS-friendly MIME type, pass Range through. No self-healing layer
# Redirect the client straight to Google's CDN instead of proxying
# bytes. iOS Safari + Google's media servers is the exact combo
# YouTube itself uses — range requests, content-length and seeking
# all handled by Google, not our fragile proxy. This removes every
# proxy behavior that iOS was rejecting with "src not supported".
@app.get("/stream/{video_id}")
async def stream(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        audio_url = await loop.run_in_executor(None, _resolve, video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not audio_url:
        raise HTTPException(status_code=404, detail="Audio not found")

    return RedirectResponse(url=audio_url, status_code=302)
