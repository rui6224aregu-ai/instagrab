"""
InstaGrab — GraphQL API版
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
import os
import tempfile
import httpx
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

def get_shortcode(url: str) -> str:
    """URLから投稿IDを取得"""
    m = re.search(r'/(?:p|reel|tv)/([A-Za-z0-9_-]+)/', url)
    return m.group(1) if m else ""

def parse_cookies_txt(content: str) -> dict:
    cookies = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies

def get_cookie_file() -> str | None:
    content = os.environ.get("INSTAGRAM_COOKIES", "")
    if not content:
        return None
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name

# ── GraphQL APIで画像取得 ─────────────────────────────
async def graphql_media(shortcode: str, cookies: dict) -> dict:
    """Instagram内部GraphQL APIを叩いてメディアURLを取得"""

    # csrftoken取得
    csrf = cookies.get("csrftoken", "")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "X-CSRFToken": csrf,
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/p/{shortcode}/",
        "Origin": "https://www.instagram.com",
    }

    params = {
        "variables": f'{{"shortcode":"{shortcode}","fetch_tagged_user_count":null,"hoisted_comment_id":null,"hoisted_reply_id":null}}',
        "doc_id": "8845758582119845",
    }

    async with httpx.AsyncClient(follow_redirects=True, timeout=15, cookies=cookies) as client:
        resp = await client.get(
            "https://www.instagram.com/graphql/query/",
            headers=headers,
            params=params,
        )
        logger.info(f"GraphQL status: {resp.status_code}")
        data = resp.json()

    images, videos = [], []
    title, description = "", ""

    # レスポンスをパース
    try:
        media = (
            data.get("data", {})
            .get("xdt_shortcode_media") or
            data.get("data", {})
            .get("shortcode_media") or {}
        )

        if not media:
            logger.warning(f"GraphQL response keys: {list(data.keys())}")
            return {"images": [], "videos": [], "title": "", "description": ""}

        description = (media.get("edge_media_to_caption", {})
                       .get("edges", [{}])[0]
                       .get("node", {})
                       .get("text", ""))[:500]

        media_type = media.get("__typename", "")
        logger.info(f"Media type: {media_type}")

        if media_type == "XDTGraphSidecar":
            # カルーセル（複数枚）
            for edge in media.get("edge_sidecar_to_children", {}).get("edges", []):
                node = edge.get("node", {})
                if node.get("is_video"):
                    vid = node.get("video_url", "")
                    if vid and is_safe_cdn(vid):
                        videos.append(vid)
                    thumb = node.get("display_url", "")
                    if thumb and is_safe_cdn(thumb):
                        images.append(thumb)
                else:
                    img = node.get("display_url", "")
                    if img and is_safe_cdn(img) and img not in images:
                        images.append(img)
        elif media.get("is_video"):
            # 動画・リール
            vid = media.get("video_url", "")
            if vid and is_safe_cdn(vid):
                videos.append(vid)
            thumb = media.get("display_url", "")
            if thumb and is_safe_cdn(thumb):
                images.append(thumb)
        else:
            # 画像
            img = media.get("display_url", "")
            if img and is_safe_cdn(img):
                images.append(img)
            # 高解像度候補
            for res in media.get("display_resources", []):
                src = res.get("src", "")
                if src and is_safe_cdn(src) and src not in images:
                    images.append(src)

    except Exception as e:
        logger.error(f"GraphQL parse error: {e}")

    return {
        "images": images[:20],
        "videos": videos[:10],
        "title": title,
        "description": description,
    }

# ── yt-dlp（リール動画用フォールバック） ─────────────
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

    cookie_content = os.environ.get("INSTAGRAM_COOKIES", "")
    cookies_dict = parse_cookies_txt(cookie_content) if cookie_content else {}
    cookie_file = get_cookie_file()
    logger.info(f"Cookies: {len(cookies_dict)} entries, csrf={'csrftoken' in cookies_dict}")

    shortcode = get_shortcode(url)
    media = {"images": [], "videos": [], "title": "", "description": ""}

    # GraphQL APIで取得（メイン）
    if cookies_dict and shortcode:
        try:
            media = await graphql_media(shortcode, cookies_dict)
            logger.info(f"GraphQL OK: {len(media['images'])}img {len(media['videos'])}vid")
        except Exception as e:
            logger.warning(f"GraphQL failed: {e}")

    # フォールバック：yt-dlp（リール用）
    if not media["images"] and not media["videos"] and "/reel/" in url:
        try:
            media = ytdlp_media(url, cookie_file)
            logger.info(f"yt-dlp OK: {len(media['images'])}img {len(media['videos'])}vid")
        except Exception as e:
            logger.warning(f"yt-dlp failed: {e}")

    if cookie_file and os.path.exists(cookie_file):
        os.unlink(cookie_file)

    if not media["images"] and not media["videos"]:
        raise HTTPException(status_code=404, detail="メディアが見つかりませんでした。非公開アカウントか、URLが正しくない可能性があります。")

    return media

# ── 画像プロキシエンドポイント ────────────────────────
@app.get("/api/proxy")
@limiter.limit("60/minute")
async def proxy_image(request: Request, url: str):
    """InstagramのCDN画像をサーバー経由で返す（CORS回避）"""
    if not is_safe_cdn(url):
        raise HTTPException(status_code=400, detail="許可されていないURLです")
    try:
        from fastapi.responses import Response
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.instagram.com/",
            })
            resp.raise_for_status()
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception as e:
        logger.error(f"Proxy error: {e}")
        raise HTTPException(status_code=502, detail="画像の取得に失敗しました")


app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
