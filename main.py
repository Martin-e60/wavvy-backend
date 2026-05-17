import asyncio
import time
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*", "Range"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"],
)

# Cache resolved YouTube audio URLs for 4 minutes
_cache: dict[str, tuple[str, float]] = {}
CACHE_TTL = 240


def _resolve(video_id: str) -> str:
    now = time.time()
    if video_id in _cache:
        url, ts = _cache[video_id]
        if now - ts < CACHE_TTL:
            return url

    ydl_opts = {
        "quiet": True,
        # Force AAC/m4a — the only audio codec iOS Safari can play.
        # NOT webm/opus, which YouTube serves by default.
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/best[ext=mp4]",
        "noplaylist": True,
        "extractor_args": {
            "youtube": {"player_client": ["android", "ios", "web"]}
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
        url = info.get("url")
        if not url:
            m4a = [
                f for f in info.get("formats", [])
                if f.get("url")
                and f.get("acodec", "").startswith("mp4a")
            ]
            if m4a:
                m4a.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                url = m4a[0]["url"]

    if url:
        _cache[video_id] = (url, now)
    return url


def _search(query: str) -> list:
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "noplaylist": True,
    }
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
            results.append({
                "id": entry.get("id"),
                "title": entry.get("title"),
                "channel": entry.get("channel") or entry.get("uploader", ""),
                "duration": dur_str,
                "thumbnail": entry.get("thumbnail", ""),
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


@app.get("/stream/{video_id}")
async def stream(video_id: str, request: Request):
    loop = asyncio.get_event_loop()
    try:
        audio_url = await loop.run_in_executor(None, _resolve, video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not audio_url:
        raise HTTPException(status_code=404, detail="Audio not found")

    # Single clean proxy: forward the client's Range header, and pass
    # YouTube's response status + headers straight back to the client.
    fwd = {}
    range_header = request.headers.get("range")
    if range_header:
        fwd["Range"] = range_header

    client = httpx.AsyncClient(timeout=None, follow_redirects=True)
    upstream = await client.send(
        client.build_request("GET", audio_url, headers=fwd),
        stream=True,
    )

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": upstream.headers.get("content-type", "audio/mp4"),
        "Cache-Control": "no-cache",
    }
    for h in ("content-length", "content-range"):
        if h in upstream.headers:
            resp_headers[h.title()] = upstream.headers[h]

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=resp_headers["Content-Type"],
    )
