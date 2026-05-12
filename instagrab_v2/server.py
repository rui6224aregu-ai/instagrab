"""
InstaGrab — ハイブリッド版（画像:スクレイピング / 動画:yt-dlp）
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import uvicorn
import logging
import re
import json
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse
import yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

ALLOWED_PATHS = re.compile(
    r'^/p/[A-Za-z0-9_-]+/?$'
    r'|^/reel/[A-Za-z0-9_-]+/?$'
    r'|^/tv/[A-Za-z0-9_-]+/?$'
    r'|^/[A-Za-z0-9_.]+/p/[A-Za-z0-9_-]+/?$'
)

ALLOWED_CDN = ("cdninstagram.com", "fbcdn.net")

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

def clean_instagram_url(url: str) -> str:
    url = url.strip()
    if len(url) > 2000:
        raise ValueError("URLが長すぎます")
    parsed = urlparse(url)
    netloc = "www.instagram.com" if parsed.netloc == "instagram.com" else parsed.netloc
    path = parsed.path.rstrip("/") + "/"
    return urlunparse(("https", netloc, path, "", "", ""))

def validate_instagram_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "www.instagram.com"
            and bool(ALLOWED_PATHS.match(parsed.path))
        )
    except Exception:
        return False

def is_safe_cdn(url: str) -> bool:
    try:
        return any(urlparse(url).netloc.endswith(d) for d in ALLOWED_CDN)
    except Exception:
        return False

# ── 方法①：スクレイピングで画像取得 ──────────────────
async def scrape_media(url: str) -> dict:
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, headers=SCRAPE_HEADERS)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    images, videos = [], []
    title, description = "", ""

    og_title = soup.find("meta", property="og:title")
    og_desc  = soup.find("meta", property="og:description")
    if og_title: title = og_title.get("content", "")[:200]
    if og_desc:  description = og_desc.get("content", "")[:500]

    for tag in soup.find_all("meta", property="og:image"):
        src = tag.get("content", "")
        if src and is_safe_cdn(src) and src not in images:
            images.append(src)

    for prop in ("og:video", "og:video:secure_url"):
        for tag in soup.find_all("meta", property=prop):
            src = tag.get("content", "")
            if src and is_safe_cdn(src) and src not in videos:
                videos.append(src)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                for k in ("image", "thumbnailUrl"):
                    v = data.get(k)
                    if isinstance(v, str) and is_safe_cdn(v) and v not in images:
                        images.append(v)
                for k in ("contentUrl",):
                    v = data.get(k)
                    if isinstance(v, str) and is_safe_cdn(v) and v not in videos:
                        videos.append(v)
        except Exception:
            pass

    for script in soup.find_all("script"):
        text = script.string or ""
        for img in re.findall(r'https://[^"\'\s]+\.(?:jpg|jpeg|png|webp)[^"\'\s]*', text):
            img = img.split("\\")[0]
            if is_safe_cdn(img) and img not in images and len(img) < 500:
                images.append(img)
        for vid in re.findall(r'https://[^"\'\s]+\.mp4[^"\'\s]*', text):
            vid = vid.split("\\")[0]
            if is_safe_cdn(vid) and vid not in videos and len(vid) < 500:
                videos.append(vid)

    return {"images": images[:20], "videos": videos[:10], "title": title, "description": description}

# ── 方法②：yt-dlp で動画取得（リール専用） ───────────
def ytdlp_media(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return {"images": [], "videos": [], "title": "", "description": ""}

    images, videos = [], []
    title = (info.get("title") or "")[:200]
    description = (info.get("description") or "")[:500]
    thumbnail = info.get("thumbnail", "")
    thumbnails = info.get("thumbnails", [])
    url_direct = info.get("url", "")
    vcodec = info.get("vcodec", "none")

    if vcodec and vcodec != "none" and url_direct:
        videos.append(url_direct)

    if thumbnails:
        best = sorted(thumbnails, key=lambda t: t.get("width", 0) or 0, reverse=True)
        src = best[0].get("url", "") if best else ""
        if src:
            images.append(src)
    elif thumbnail:
        images.append(thumbnail)

    return {"images": images, "videos": videos, "title": title, "description": description}

# ── APIエンドポイント ──────────────────────────────────
@app.get("/api/extract")
@limiter.limit("10/minute")
async def extract(request: Request, url: str):
    try:
        url = clean_instagram_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not validate_instagram_url(url):
        logger.warning(f"Invalid URL: {url[:100]}")
        raise HTTPException(status_code=400, detail="有効な Instagram の投稿URL（/p/ /reel/ /tv/）を入力してください")

    is_reel = "/reel/" in url
    media = {"images": [], "videos": [], "title": "", "description": ""}

    # リールはyt-dlpで試みる
    if is_reel:
        try:
            media = ytdlp_media(url)
            logger.info(f"yt-dlp OK: {len(media['images'])}img {len(media['videos'])}vid")
        except Exception as e:
            logger.warning(f"yt-dlp failed, fallback to scrape: {e}")

    # 画像投稿 or yt-dlp失敗時はスクレイピング
    if not media["images"] and not media["videos"]:
        try:
            media = await scrape_media(url)
            logger.info(f"scrape OK: {len(media['images'])}img {len(media['videos'])}vid")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 404:
                raise HTTPException(status_code=404, detail="投稿が見つかりません")
            raise HTTPException(status_code=502, detail="Instagram へのアクセスに失敗しました")
        except Exception as e:
            logger.error(f"scrape error: {e}")
            raise HTTPException(status_code=500, detail="サーバーエラーが発生しました")

    if not media["images"] and not media["videos"]:
        raise HTTPException(status_code=404, detail="メディアが見つかりませんでした。非公開アカウントか、URLが正しくない可能性があります。")

    return media

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
