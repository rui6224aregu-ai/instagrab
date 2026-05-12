"""
InstaGrab — セキュリティ強化版サーバー
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import httpx
from bs4 import BeautifulSoup
import re
import json
import uvicorn
import logging
from urllib.parse import urlparse

# ── ログ設定 ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── レート制限（1IPあたり1分に10回まで） ──────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    docs_url=None,   # Swagger UIを無効化（不要な情報露出を防ぐ）
    redoc_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS（自サイトからのアクセスのみ許可） ────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Renderデプロイ後は自ドメインに変更可
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── セキュリティヘッダーをすべてのレスポンスに付与 ────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response

# ── Instagram リクエスト用ヘッダー ────────────────────
INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# 許可するInstagramのURLパターン
ALLOWED_INSTAGRAM_PATHS = re.compile(
    r'^/p/[A-Za-z0-9_-]+/?$'          # フィード投稿
    r'|^/reel/[A-Za-z0-9_-]+/?$'      # リール
    r'|^/tv/[A-Za-z0-9_-]+/?$'        # IGTV
)

MAX_RESPONSE_SIZE = 5 * 1024 * 1024   # 5MB上限（巨大レスポンスを拒否）


def validate_instagram_url(url: str) -> bool:
    """URLがInstagramの正規投稿URLかチェック"""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        if parsed.netloc not in ("www.instagram.com", "instagram.com"):
            return False
        if not ALLOWED_INSTAGRAM_PATHS.match(parsed.path):
            return False
        return True
    except Exception:
        return False


def sanitize_url(url: str) -> str:
    """URLから危険な文字を除去"""
    url = url.strip()
    # 2000文字以上は拒否
    if len(url) > 2000:
        raise ValueError("URLが長すぎます")
    return url


def extract_media_from_html(html: str) -> dict:
    """HTMLからメディアURLを抽出（安全なドメインのみ）"""
    soup = BeautifulSoup(html, "html.parser")
    media: dict = {"images": [], "videos": [], "title": "", "description": ""}

    # 許可するCDNドメイン（Instagram公式のみ）
    ALLOWED_CDN = (
        "cdninstagram.com",
        "fbcdn.net",
        "instagram.com",
    )

    def is_safe_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            return any(parsed.netloc.endswith(d) for d in ALLOWED_CDN)
        except Exception:
            return False

    # OGPタグからタイトル・説明文
    og_title = soup.find("meta", property="og:title")
    og_desc = soup.find("meta", property="og:description")
    if og_title:
        media["title"] = og_title.get("content", "")[:200]   # 200文字上限
    if og_desc:
        media["description"] = og_desc.get("content", "")[:500]

    # OGP 画像
    for tag in soup.find_all("meta", property="og:image"):
        src = tag.get("content", "")
        if src and is_safe_url(src) and src not in media["images"]:
            media["images"].append(src)

    # OGP 動画
    for prop in ("og:video", "og:video:secure_url"):
        for tag in soup.find_all("meta", property=prop):
            src = tag.get("content", "")
            if src and is_safe_url(src) and src not in media["videos"]:
                media["videos"].append(src)

    # JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, dict):
                for key in ("image", "thumbnailUrl"):
                    val = data.get(key)
                    if isinstance(val, str) and is_safe_url(val) and val not in media["images"]:
                        media["images"].append(val)
                for key in ("contentUrl", "video"):
                    val = data.get(key)
                    if isinstance(val, str) and is_safe_url(val) and val not in media["videos"]:
                        media["videos"].append(val)
        except Exception:
            pass

    # インラインスクリプトからCDN URL抽出（安全なドメインのみ）
    for script in soup.find_all("script"):
        text = script.string or ""
        for img in re.findall(r'https://[^"\'\s]+\.(?:jpg|jpeg|png|webp)[^"\'\s]*', text):
            img = img.split("\\")[0]
            if is_safe_url(img) and img not in media["images"] and len(img) < 500:
                media["images"].append(img)
        for vid in re.findall(r'https://[^"\'\s]+\.mp4[^"\'\s]*', text):
            vid = vid.split("\\")[0]
            if is_safe_url(vid) and vid not in media["videos"] and len(vid) < 500:
                media["videos"].append(vid)

    # 上限を設ける（念のため）
    media["images"] = media["images"][:20]
    media["videos"] = media["videos"][:10]

    return media


# ── APIエンドポイント ──────────────────────────────────
@app.get("/api/extract")
@limiter.limit("10/minute")   # 1IPあたり1分に10回まで
async def extract(request: Request, url: str):
    # 入力バリデーション
    try:
        url = sanitize_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not validate_instagram_url(url):
        logger.warning(f"Invalid URL rejected: {url[:100]}")
        raise HTTPException(
            status_code=400,
            detail="有効な Instagram の投稿URL（/p/ /reel/ /tv/）を入力してください"
        )

    # Instagramへリクエスト
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
            limits=httpx.Limits(max_connections=10),
        ) as client:
            resp = await client.get(url, headers=INSTAGRAM_HEADERS)
            resp.raise_for_status()

            # レスポンスサイズチェック
            if len(resp.content) > MAX_RESPONSE_SIZE:
                raise HTTPException(status_code=400, detail="レスポンスが大きすぎます")

    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code == 404:
            raise HTTPException(status_code=404, detail="投稿が見つかりません")
        elif code == 429:
            raise HTTPException(status_code=429, detail="リクエストが多すぎます。しばらくしてから再試行してください")
        else:
            logger.error(f"Instagram HTTP error {code} for {url[:80]}")
            raise HTTPException(status_code=502, detail=f"Instagram へのアクセスに失敗しました")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="タイムアウトしました。再度お試しください")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="サーバーエラーが発生しました")

    media = extract_media_from_html(resp.text)

    if not media["images"] and not media["videos"]:
        raise HTTPException(
            status_code=404,
            detail="メディアが見つかりませんでした。非公開アカウントか、URLが正しくない可能性があります。"
        )

    logger.info(f"OK: {len(media['images'])}img {len(media['videos'])}vid")
    return media


# ── 静的ファイル配信 ───────────────────────────────────
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
