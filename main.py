import asyncio
import time
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response

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
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
        url = info.get("url")
        if not url:
            for fmt in reversed(info.get("formats", [])):
                if fmt.get("url") and fmt.get("acodec") != "none":
                    url = fmt["url"]
                    break

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
    """Warm the cache so /stream responds instantly."""
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

    range_header = request.headers.get("range")
    upstream_headers = {}
    if range_header:
        upstream_headers["Range"] = range_header

    async def generator():
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", audio_url, headers=upstream_headers) as resp:
                # Send headers on first chunk
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    # Get headers quickly with a HEAD-like partial request
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        head = await client.get(
            audio_url,
            headers={**upstream_headers, "Range": range_header or "bytes=0-0"},
        )

    content_type = head.headers.get("content-type", "audio/mp4")
    resp_status = 206 if range_header else 200

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Cache-Control": "no-cache",
    }
    if "content-range" in head.headers:
        # Extract total size from content-range: bytes 0-0/TOTAL
        cr = head.headers["content-range"]
        total = cr.split("/")[-1]
        if range_header:
            resp_headers["Content-Range"] = f"{range_header.replace('bytes=', 'bytes ')}/{total}"
        resp_headers["Content-Length"] = total
    elif "content-length" in head.headers:
        resp_headers["Content-Length"] = head.headers["content-length"]

    return StreamingResponse(
        generator(),
        status_code=resp_status,
        headers=resp_headers,
        media_type=content_type,
    )
