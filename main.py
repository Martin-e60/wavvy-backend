import asyncio
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _get_audio_url(video_id: str) -> str:
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
                    return fmt["url"]
        return url


@app.get("/search")
async def search(q: str):
    if not q.strip():
        return []
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _search, q)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stream/{video_id}")
async def stream(video_id: str):
    loop = asyncio.get_event_loop()
    try:
        audio_url = await loop.run_in_executor(None, _get_audio_url, video_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not audio_url:
        raise HTTPException(status_code=404, detail="Audio not found")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=audio_url, status_code=302)
