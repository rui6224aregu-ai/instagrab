"""
InstaGrab — Cookie対応ハイブリッド版 v2
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
import os
import tempfile
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

def get_cookie_file():
    """環境変数のCookieを一時ファイルに書き出す"""
    cookie_content = os.environ.get("INSTAGRAM_COOKIES", "")
    if not cookie_content:
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(cookie_content)
    tmp.close()
    return tmp.name

def parse_cookies_txt(cookie_content: str) -> dict:
    """cookies.txt形式をdictに変換してhttpxで使えるようにする"""
    cookies = {}
    for line in cookie_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name = parts[5]
            value = parts[6]
            cookies[name] = value
    return cookies

# ── yt-dlp（動画・リール用） ──────────────────────────
def ytdlp_media(url: str, cookie_file: str = None) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": False,
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return {"images": [], "videos": [], "title": "", "description": ""}

    images, videos = [], []
    title = (info.get("title") or "")[:200]
    description = (info.get("description") or "")[:500]
    entries = info.get("entries") or [info]

    for entry in entries:
        if not entry:
            continue
        url_direct = entry.get("url", "")
        thumbnail = entry.get("thumbnail", "")
        thumbnails = entry.get("thumbnails", [])
        vcodec = entry.get("vcodec", "none")
        is_video = bool(vcodec and vcodec != "none")

        if is_video and url_direct:
            if url_direct not in videos:
                videos.append(url_direct)
            if thumbnail and thumbnail not in images:
                images.append(thumbnail)
        else:
            if thumbnails:
                best = sorted(thumbnails, key=lambda t: t.get("width", 0) or 0, reverse=True)
                src = best[0].get("url", "") if best else ""
                if src and src not in images:
                    images.append(src)
            elif thumbnail and thumbnail not in images:
                images.append(thumbnail)

    return {"images": images[:20], "videos": videos[:10], "title": title, "description": description}

# ── スクレイピング（Cookie付きhttpx） ─────────────────
async def scrape_media(url: str, cookies: dict = None) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15, cookies=cookies or {}) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

    html = resp.text
    logger.info(f"HTML length: {len(html)}, has og:image: {'og:image' in html}")

    soup = BeautifulSoup(html, "html.parser")
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

    cookie_file = get_cookie_file()
    cookie_content = os.environ.get("INSTAGRAM_COOKIES", "")
    cookies_dict = parse_cookies_txt(cookie_content) if cookie_content else {}
    logger.info(f"Cookies loaded: {len(cookies_dict)} entries")

    media = {"images": [], "videos": [], "title": "", "description": ""}
    is_reel = "/reel/" in url

    # リールはyt-dlp（Cookie付き）で試みる
    if is_reel:
        try:
            media = ytdlp_media(url, cookie_file)
            logger.info(f"yt-dlp OK: {len(media['images'])}img {len(media['videos'])}vid")
        except Exception as e:
            logger.warning(f"yt-dlp failed: {e}")

    # 画像投稿 or yt-dlp失敗 → Cookie付きスクレイピング
    if not media["images"] and not media["videos"]:
        try:
            media = await scrape_media(url, cookies=cookies_dict)
            logger.info(f"scrape OK: {len(media['images'])}img {len(media['videos'])}vid")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 404:
                raise HTTPException(status_code=404, detail="投稿が見つかりません")
            raise HTTPException(status_code=502, detail="Instagram へのアクセスに失敗しました")
        except Exception as e:
            logger.error(f"scrape error: {e}")

    if cookie_file and os.path.exists(cookie_file):
        os.unlink(cookie_file)

    if not media["images"] and not media["videos"]:
        raise HTTPException(status_code=404, detail="メディアが見つかりませんでした。非公開アカウントか、URLが正しくない可能性があります。")

    return media

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
