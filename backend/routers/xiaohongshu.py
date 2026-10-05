import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiofiles
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import get_current_user


router = APIRouter(
    prefix="/api/import/xiaohongshu",
    tags=["xiaohongshu-import"],
    dependencies=[Depends(get_current_user)],
)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
SOURCE_DOMAINS = ("xiaohongshu.com", "xhslink.com")
IMAGE_DOMAINS = ("xiaohongshu.com", "xhscdn.com")
MAX_IMAGES = 24
MAX_IMAGE_SIZE = 25 * 1024 * 1024
MAX_VIDEO_SIZE = 200 * 1024 * 1024
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
}


class XiaohongshuImportRequest(BaseModel):
    share_text: str = Field(min_length=5, max_length=5000)


def _host_allowed(host: str | None, allowed_domains: tuple[str, ...]) -> bool:
    normalized = (host or "").lower().rstrip(".")
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in allowed_domains
    )


def extract_share_url(share_text: str) -> str:
    match = URL_PATTERN.search(share_text)
    if not match:
        raise HTTPException(status_code=422, detail="没有找到有效的小红书分享链接")

    url = match.group(0).rstrip(".,;:!?)]}>，。；：！？）】》")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not _host_allowed(parsed.hostname, SOURCE_DOMAINS):
        raise HTTPException(status_code=422, detail="仅支持小红书或 xhslink.com 分享链接")
    return url


async def resolve_share_url(url: str) -> str:
    parsed = urlparse(url)
    if not _host_allowed(parsed.hostname, ("xhslink.com",)):
        return url

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=8,
            timeout=httpx.Timeout(20.0),
            headers=HTTP_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="小红书短链接解析失败，请检查链接是否有效") from error

    resolved_url = str(response.url)
    if not _host_allowed(response.url.host, ("xiaohongshu.com",)):
        raise HTTPException(status_code=422, detail="短链接没有跳转到有效的小红书页面")
    return resolved_url


def _get_cli_path() -> str:
    configured_path = os.getenv("XHS_CLI_PATH", "").strip().strip('"')
    cli_path = configured_path or shutil.which("xhs")
    if not cli_path:
        raise HTTPException(
            status_code=503,
            detail="未安装小红书导入工具，请在后端环境安装 xiaohongshu-cli",
        )
    return cli_path


def _parse_cli_json(stdout: str) -> dict[str, Any]:
    content = stdout.strip().lstrip("\ufeff")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise HTTPException(status_code=502, detail="小红书导入工具返回了无法识别的数据")
        try:
            payload = json.loads(content[start:end + 1])
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=502, detail="小红书导入工具返回了无法识别的数据") from error

    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="小红书导入工具返回格式不正确")
    return payload


def _friendly_cli_error(message: str) -> str:
    normalized = message.lower()
    if any(keyword in normalized for keyword in ("login", "cookie", "登录", "未登录", "unauthorized")):
        return "小红书登录状态无效，请在后端重新运行 xhs login --cookie-source firefox（或换成实际登录的浏览器）"
    if any(keyword in normalized for keyword in ("captcha", "risk", "verify", "验证", "风控")):
        return "小红书触发了安全验证，请稍后重试或重新登录"
    if "not found" in normalized or "不存在" in normalized:
        return "没有找到这篇小红书笔记，可能已删除或不可见"
    if "api error" in normalized:
        return "小红书接口暂时拒绝了请求，请稍后重试；若持续失败，请升级 xiaohongshu-cli 并重新登录"
    return f"小红书内容获取失败：{message or '未知错误'}"


async def run_xhs_command(
    command: str,
    target: str,
    timeout_seconds: int,
    *,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    cli_path = _get_cli_path()
    args = [cli_path, command]
    if target:
        args.append(target)
    if command == "comments":
        args.append("--all")
    args.extend(extra_args or [])
    args.append("--json")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as error:
        raise HTTPException(status_code=503, detail="小红书导入工具无法启动") from error

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="小红书内容获取超时，请稍后重试") from error

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    payload = _parse_cli_json(stdout) if stdout.strip() else {}

    if process.returncode != 0 or payload.get("ok") is False:
        error_data = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error_data.get("message") if isinstance(error_data, dict) else ""
        raise HTTPException(status_code=502, detail=_friendly_cli_error(message or stderr))

    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="小红书导入结果缺少有效内容")
    return data


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return 0

    normalized = value.strip().replace(",", "")
    multiplier = 1
    if normalized.endswith("万"):
        multiplier = 10_000
        normalized = normalized[:-1]
    elif normalized.endswith("亿"):
        multiplier = 100_000_000
        normalized = normalized[:-1]
    try:
        return int(float(normalized) * multiplier)
    except ValueError:
        return 0


def _normalize_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url if url.startswith(("http://", "https://")) else None


def _image_url(image: Any) -> str | None:
    if isinstance(image, str):
        return _normalize_url(image)
    if not isinstance(image, dict):
        return None

    for key in ("url_default", "url_pre", "url", "original_url"):
        candidate = _normalize_url(image.get(key))
        if candidate:
            return candidate

    info_list = image.get("info_list")
    if isinstance(info_list, list):
        preferred = sorted(
            (item for item in info_list if isinstance(item, dict)),
            key=lambda item: "WB_DFT" not in str(item.get("image_scene", "")),
        )
        for item in preferred:
            candidate = _normalize_url(item.get("url"))
            if candidate:
                return candidate
    return None


def _video_info(note: dict[str, Any]) -> dict[str, Any]:
    video = note.get("video")
    if not isinstance(video, dict):
        return {"url": None, "duration_seconds": 0}

    media = video.get("media")
    if not isinstance(media, dict):
        media_v2 = video.get("media_v2")
        if isinstance(media_v2, str):
            try:
                parsed_media = json.loads(media_v2)
            except json.JSONDecodeError:
                parsed_media = None
            media = parsed_media if isinstance(parsed_media, dict) else {}
        else:
            media = {}

    stream = media.get("stream") if isinstance(media.get("stream"), dict) else {}
    selected_stream: dict[str, Any] | None = None
    for codec in ("h264", "h265"):
        candidates = stream.get(codec)
        if not isinstance(candidates, list):
            continue
        compatible = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and _number(candidate.get("size")) <= MAX_VIDEO_SIZE
            and (_normalize_url(candidate.get("master_url")) or candidate.get("backup_urls"))
        ]
        if not compatible:
            continue
        preferred = [candidate for candidate in compatible if _number(candidate.get("height")) <= 1080]
        selected_stream = max(
            preferred or compatible,
            key=lambda candidate: _number(candidate.get("width")) * _number(candidate.get("height")),
        )
        break

    if not selected_stream:
        return {"url": None, "duration_seconds": 0}

    video_url = _normalize_url(selected_stream.get("master_url"))
    if not video_url:
        backup_urls = selected_stream.get("backup_urls")
        if isinstance(backup_urls, list):
            video_url = next(
                (_normalize_url(candidate) for candidate in backup_urls if _normalize_url(candidate)),
                None,
            )

    capa = video.get("capa") if isinstance(video.get("capa"), dict) else {}
    media_video = media.get("video") if isinstance(media.get("video"), dict) else {}
    duration_seconds = _number(capa.get("duration") or media_video.get("duration"))
    if not duration_seconds:
        duration_ms = _number(selected_stream.get("duration"))
        duration_seconds = round(duration_ms / 1000) if duration_ms else 0

    return {"url": video_url, "duration_seconds": duration_seconds}


def _fallback_title(content: str, author: str, note_type: str) -> str:
    first_line = next(
        (line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "",
    )
    if first_line:
        return first_line if len(first_line) <= 36 else f"{first_line[:36]}..."
    if note_type == "video":
        return f"{author or '小红书博主'}的视频笔记"
    return "小红书笔记"


def normalize_note(data: dict[str, Any]) -> dict[str, Any]:
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise HTTPException(status_code=502, detail="没有读取到小红书笔记内容")

    item = items[0]
    note = item.get("note_card")
    if not isinstance(note, dict):
        raise HTTPException(status_code=502, detail="小红书笔记内容格式不正确")

    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    interact = note.get("interact_info") if isinstance(note.get("interact_info"), dict) else {}
    tag_list = note.get("tag_list") if isinstance(note.get("tag_list"), list) else []
    image_list = note.get("image_list") if isinstance(note.get("image_list"), list) else []

    tags = []
    for tag in tag_list:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if isinstance(name, str) and name.strip() and name.strip() not in tags:
            tags.append(name.strip())

    image_urls = []
    for image in image_list[:MAX_IMAGES]:
        url = _image_url(image)
        if url and url not in image_urls:
            image_urls.append(url)

    content = str(note.get("desc") or "").strip()
    author = str(user.get("nickname") or "").strip()
    note_type = str(note.get("type") or "normal").strip()
    title = str(note.get("title") or note.get("display_title") or "").strip()
    if not title:
        title = _fallback_title(content, author, note_type)
    video_info = _video_info(note)
    return {
        "note_id": str(item.get("id") or note.get("note_id") or ""),
        "title": title,
        "content": content,
        "author": author,
        "author_id": str(user.get("user_id") or "").strip(),
        "tags": tags,
        "image_urls": image_urls,
        "note_type": note_type,
        "video_url": video_info["url"],
        "video_duration_seconds": video_info["duration_seconds"],
        "metrics": {
            "likes": _number(interact.get("liked_count")),
            "collections": _number(interact.get("collected_count")),
            "comments": _number(interact.get("comment_count")),
            "shares": _number(interact.get("share_count")),
        },
    }


def normalize_comments(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_comments = data.get("comments")
    if not isinstance(raw_comments, list):
        return []

    comments = []
    seen_ids = set()
    for index, raw_comment in enumerate(raw_comments):
        if not isinstance(raw_comment, dict):
            continue
        content = str(raw_comment.get("content") or "").strip()
        if not content:
            continue
        user = raw_comment.get("user_info") if isinstance(raw_comment.get("user_info"), dict) else {}
        comment_id = str(raw_comment.get("id") or f"comment-{index}")
        if comment_id in seen_ids:
            continue
        seen_ids.add(comment_id)
        comments.append({
            "id": comment_id,
            "author": str(user.get("nickname") or "小红书用户").strip(),
            "content": content,
            "likes": _number(raw_comment.get("like_count")),
            "reply_count": _number(raw_comment.get("sub_comment_count")),
        })

    comments.sort(key=lambda comment: comment["likes"], reverse=True)
    return comments[:10]


def _image_extension(content_type: str, url: str) -> str:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    mapped = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/avif": ".avif",
    }.get(normalized_type)
    if mapped:
        return mapped
    extension = Path(urlparse(url).path).suffix.lower()
    return extension if extension in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"} else ".jpg"


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    index: int,
    filename_prefix: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    parsed = urlparse(url)
    if not _host_allowed(parsed.hostname, IMAGE_DOMAINS):
        return None, f"第 {index + 1} 张图片来源域名不受支持"

    filename = ""
    filepath: Path | None = None
    temporary_path: Path | None = None
    try:
        safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", filename_prefix or "").strip("-")[:160]
        if safe_prefix:
            existing = next((
                candidate
                for candidate in UPLOAD_DIR.glob(f"{safe_prefix}-image-{index + 1}.*")
                if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
                and candidate.is_file()
                and candidate.stat().st_size > 0
            ), None)
            if existing and existing.is_file():
                content_type = mimetypes.guess_type(existing.name)[0] or "image/jpeg"
                return ({
                    "name": f"小红书图片-{index + 1}{existing.suffix.lower()}",
                    "path": f"/uploads/{existing.name}",
                    "type": content_type,
                    "size": existing.stat().st_size,
                }, None)
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            if not _host_allowed(response.url.host, IMAGE_DOMAINS):
                return None, f"第 {index + 1} 张图片跳转到了非小红书域名"

            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            extension = _image_extension(content_type, str(response.url))
            if content_type and not content_type.startswith("image/"):
                return None, f"第 {index + 1} 个附件不是图片"

            filename = (
                f"{safe_prefix}-image-{index + 1}{extension}"
                if safe_prefix
                else f"xhs-{uuid.uuid4().hex}{extension}"
            )
            filepath = UPLOAD_DIR / filename
            temporary_path = UPLOAD_DIR / f".{filename}.{uuid.uuid4().hex}.tmp"
            total_size = 0
            async with aiofiles.open(temporary_path, "wb") as output:
                async for chunk in response.aiter_bytes(1024 * 256):
                    total_size += len(chunk)
                    if total_size > MAX_IMAGE_SIZE:
                        raise ValueError("图片超过 25MB")
                    await output.write(chunk)
            if total_size == 0:
                raise ValueError("图片内容为空")
            temporary_path.replace(filepath)

        guessed_type = content_type or mimetypes.guess_type(filename)[0] or "image/jpeg"
        return ({
            "name": f"小红书图片-{index + 1}{extension}",
            "path": f"/uploads/{filename}",
            "type": guessed_type,
            "size": total_size,
        }, None)
    except (httpx.HTTPError, OSError, ValueError) as error:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        return None, f"第 {index + 1} 张图片下载失败：{error}"


async def download_images(
    image_urls: list[str],
    *,
    filename_prefix: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not image_urls:
        return [], []

    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(30.0),
        headers=HTTP_HEADERS,
    ) as client:
        async def download(url: str, index: int):
            async with semaphore:
                return await _download_image(client, url, index, filename_prefix)

        results = await asyncio.gather(*(
            download(url, index) for index, url in enumerate(image_urls)
        ))

    attachments = [attachment for attachment, _ in results if attachment]
    warnings = [warning for _, warning in results if warning]
    return attachments, warnings


async def download_video(
    video_url: str | None,
    *,
    filename_prefix: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if not video_url:
        return None, None

    parsed = urlparse(video_url)
    if not _host_allowed(parsed.hostname, IMAGE_DOMAINS):
        return None, "视频来源域名不受支持"

    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", filename_prefix or "").strip("-")[:160]
    filename = f"{safe_prefix}-video.mp4" if safe_prefix else f"xhs-{uuid.uuid4().hex}.mp4"
    filepath = UPLOAD_DIR / filename
    temporary_path = UPLOAD_DIR / f".{filename}.{uuid.uuid4().hex}.tmp"
    try:
        if filepath.is_file() and filepath.stat().st_size > 0:
            return ({
                "name": "小红书视频-1.mp4",
                "path": f"/uploads/{filename}",
                "type": "video/mp4",
                "size": filepath.stat().st_size,
            }, None)
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=5,
            timeout=httpx.Timeout(90.0),
            headers=HTTP_HEADERS,
        ) as client:
            async with client.stream("GET", video_url) as response:
                response.raise_for_status()
                if not _host_allowed(response.url.host, IMAGE_DOMAINS):
                    return None, "视频跳转到了非小红书域名"

                content_length = _number(response.headers.get("content-length"))
                if content_length > MAX_VIDEO_SIZE:
                    return None, "视频超过 200MB，未自动下载"

                total_size = 0
                async with aiofiles.open(temporary_path, "wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 512):
                        total_size += len(chunk)
                        if total_size > MAX_VIDEO_SIZE:
                            raise ValueError("视频超过 200MB")
                        await output.write(chunk)
                if total_size == 0:
                    raise ValueError("视频内容为空")
                temporary_path.replace(filepath)

        return ({
            "name": "小红书视频-1.mp4",
            "path": f"/uploads/{filename}",
            "type": "video/mp4",
            "size": total_size,
        }, None)
    except (httpx.HTTPError, OSError, ValueError) as error:
        if temporary_path.exists():
            temporary_path.unlink()
        return None, f"视频下载失败：{error}"


@router.get("/status")
async def get_import_status():
    try:
        cli_path = _get_cli_path()
    except HTTPException:
        return {
            "installed": False,
            "cli_path": None,
            "setup_hint": "python -m pip install --upgrade 'xiaohongshu-cli>=0.6.4,<0.7.0'",
        }
    return {
        "installed": True,
        "cli_path": cli_path,
        "setup_hint": "登录失效时到「账号情报」页点「登录小红书」重新扫码",
    }


@router.post("")
async def import_xiaohongshu_material(request: XiaohongshuImportRequest):
    share_url = extract_share_url(request.share_text)
    resolved_url = await resolve_share_url(share_url)
    note_data = await run_xhs_command("read", resolved_url, timeout_seconds=90)
    normalized_note = normalize_note(note_data)

    warnings = []
    comment_targets = []
    original_host = urlparse(share_url).hostname
    if _host_allowed(original_host, ("xiaohongshu.com",)):
        comment_targets.append(share_url)
    comment_targets.append(resolved_url)
    if normalized_note["note_id"]:
        comment_targets.append(normalized_note["note_id"])

    top_comments = []
    comment_error = ""
    tried_targets = set()
    for comment_target in comment_targets:
        if comment_target in tried_targets:
            continue
        tried_targets.add(comment_target)
        try:
            comments_data = await run_xhs_command("comments", comment_target, timeout_seconds=45)
            top_comments = normalize_comments(comments_data)
            if top_comments:
                break
        except HTTPException as error:
            comment_error = str(error.detail)

    if not top_comments:
        warnings.append(
            f"热门评论暂未获取：{comment_error}"
            if comment_error
            else "这篇笔记暂未获取到公开评论，或小红书暂时限制了评论接口"
        )

    attachments, image_warnings = await download_images(normalized_note["image_urls"])
    warnings.extend(image_warnings)
    video_attachment, video_warning = await download_video(normalized_note["video_url"])
    if video_attachment:
        attachments.append(video_attachment)
    if video_warning:
        warnings.append(video_warning)

    content = normalized_note["content"]
    summary = content[:180] + ("..." if len(content) > 180 else "")
    source_metadata = {
        "platform": "xiaohongshu",
        "note_id": normalized_note["note_id"],
        "author_id": normalized_note["author_id"],
        "share_text": request.share_text.strip(),
        "resolved_url": resolved_url,
        "metrics": normalized_note["metrics"],
        "top_comments": top_comments,
        "image_count": len(normalized_note["image_urls"]),
        "video_count": 1 if video_attachment else 0,
        "note_type": normalized_note["note_type"],
        "video_duration_seconds": normalized_note["video_duration_seconds"],
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "title": normalized_note["title"],
        "content": content,
        "summary": summary,
        "author": normalized_note["author"],
        "source_url": resolved_url,
        "tags": normalized_note["tags"],
        "attachments": attachments,
        "source_metadata": source_metadata,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 扫码登录会话
#
# 为什么由后端托管子进程而不是前端直接调 CLI：
# 扫码要等用户在手机上确认，属于长耗时交互，而且 CLI 装在独立解释器里（依赖
# playwright / camoufox 等重量级包），不能塞进后端 .venv。这里用子进程 + 状态文件
# 通信，前端只轮询后端，刷新页面也不会把等待中的登录进程弄丢。
# ---------------------------------------------------------------------------

LOGIN_SESSION_TTL_SECONDS = 900
LOGIN_QR_WAIT_SECONDS = 25
_LOGIN_SESSIONS: dict[str, dict[str, Any]] = {}


def _get_cli_python() -> str:
    """解析运行 xiaohongshu-cli 的解释器路径。

    CLI 装在独立 venv 里，和后端自己的解释器不是同一个，所以单独解析一次。
    """
    configured = os.getenv("XHS_CLI_PYTHON", "").strip().strip('"')
    if configured and Path(configured).exists():
        return configured

    cli = Path(_get_cli_path())
    for candidate in (
        cli.parent / "python.exe",
        cli.parent / "python",
        cli.parent.parent / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)

    raise HTTPException(
        status_code=503,
        detail="未找到小红书导入工具的解释器，请在 .env 中设置 XHS_CLI_PYTHON",
    )


def _login_helper_path() -> Path:
    helper = Path(__file__).resolve().parents[2] / "scripts" / "xhs_login_helper.py"
    if not helper.exists():
        raise HTTPException(status_code=503, detail="扫码登录助手脚本缺失：scripts/xhs_login_helper.py")
    return helper


def _dispose_login_session(session_id: str) -> None:
    session = _LOGIN_SESSIONS.pop(session_id, None)
    if not session:
        return

    process = session.get("process")
    if process is not None and process.returncode is None:
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            pass

    directory = session.get("directory")
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def _prune_login_sessions() -> None:
    now = time.time()
    for session_id, session in list(_LOGIN_SESSIONS.items()):
        if now - float(session.get("created_at", now)) > LOGIN_SESSION_TTL_SECONDS:
            _dispose_login_session(session_id)


def _read_login_status(session: dict[str, Any]) -> dict[str, Any]:
    """读助手写的状态文件；文件还没落盘时按「正在申请」处理。"""
    fallback = {"state": "starting", "message": "正在申请二维码…"}
    status_file: Path = session["status_file"]
    if not status_file.exists():
        return fallback
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    return payload if isinstance(payload, dict) else fallback


def _login_qr_data_url(session: dict[str, Any]) -> str | None:
    """二维码转 base64 data URL，前端直接塞进 <img> 即可，省一次二进制请求。"""
    cached = session.get("qr_data_url")
    if cached:
        return cached

    qr_file: Path = session["qr_file"]
    if not qr_file.exists():
        return None
    try:
        raw = qr_file.read_bytes()
    except OSError:
        return None
    if not raw:
        return None

    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    session["qr_data_url"] = data_url
    return data_url


async def _login_session_payload(session_id: str, session: dict[str, Any]) -> dict[str, Any]:
    status = _read_login_status(session)
    state = str(status.get("state") or "starting")

    # 登录确认后顺手把 CLI 的账号信息带回来，前端状态条可以直接刷新
    if state == "confirmed" and not session.get("user"):
        try:
            data = await run_xhs_command("status", "", timeout_seconds=25)
            raw_user = data.get("user") if isinstance(data.get("user"), dict) else None
            if raw_user:
                session["user"] = {
                    "id": str(raw_user.get("id") or raw_user.get("user_id") or ""),
                    "name": str(raw_user.get("name") or raw_user.get("nickname") or ""),
                    "red_id": str(raw_user.get("red_id") or raw_user.get("username") or ""),
                }
        except HTTPException:
            pass

    return {
        "session_id": session_id,
        "state": state,
        "message": str(status.get("message") or ""),
        "error": str(status.get("error") or ""),
        "qr_data_url": _login_qr_data_url(session),
        "expires_at": session["expires_at"],
        "user": session.get("user"),
    }


@router.post("/login/session")
async def create_login_session():
    _prune_login_sessions()

    cli_python = _get_cli_python()
    helper = _login_helper_path()

    directory = Path(tempfile.mkdtemp(prefix="xhs-login-"))
    status_file = directory / "status.json"
    qr_file = directory / "qr.png"

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        process = await asyncio.create_subprocess_exec(
            cli_python,
            str(helper),
            "--status-file",
            str(status_file),
            "--qr-file",
            str(qr_file),
            "--timeout",
            "600",
            # 助手会持续打印进度，走管道而没人读会把缓冲区塞满卡死，直接丢弃
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=503, detail="扫码登录进程无法启动") from error

    session_id = uuid.uuid4().hex
    _LOGIN_SESSIONS[session_id] = {
        "process": process,
        "directory": directory,
        "status_file": status_file,
        "qr_file": qr_file,
        "created_at": time.time(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(),
        "qr_data_url": None,
        "user": None,
    }

    # 二维码由 CLI 内部请求生成，通常几秒内就绪；等一小会儿能让前端少一轮空轮询
    deadline = time.time() + LOGIN_QR_WAIT_SECONDS
    while time.time() < deadline:
        if qr_file.exists() and qr_file.stat().st_size > 0:
            break
        if process.returncode is not None:
            break
        await asyncio.sleep(0.4)

    return await _login_session_payload(session_id, _LOGIN_SESSIONS[session_id])


@router.get("/login/session/{session_id}")
async def get_login_session(session_id: str):
    _prune_login_sessions()
    session = _LOGIN_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期，请重新获取二维码")
    return await _login_session_payload(session_id, session)


@router.delete("/login/session/{session_id}")
async def cancel_login_session(session_id: str):
    _dispose_login_session(session_id)
    return {"ok": True}
