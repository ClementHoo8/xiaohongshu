# -*- coding: utf-8 -*-
"""小红书扫码登录助手（供后端以子进程方式调用）。

为什么做成独立脚本而不是直接在后端进程里 import：
`xiaohongshu-cli` 装在后端 `.venv` 之外的独立环境（依赖 playwright/camoufox/numpy 等
重量级包），直接装进后端环境会污染依赖。这里用子进程 + 状态文件通信，两边解耦。

后端调用约定：
    <xhs_cli_python> scripts/xhs_login_helper.py \
        --status-file <status.json> --qr-file <qr.png> --timeout 600

状态文件内容（原子写入，读方不会看到半截 JSON）：
    {
      "state": "starting|waiting|scanned|confirmed|error|expired",
      "message": "给人看的中文说明",
      "user_id": "登录成功后的用户 ID",
      "error": "失败原因",
      "updated_at": 1791186180.123
    }

注意：本脚本必须在装有 xiaohongshu-cli 的解释器下运行，不能在后端 .venv 下运行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import qrcode

import xhs_cli.qr_login as ql


def write_status(path: Path, **fields: Any) -> None:
    """原子写状态文件，避免读方读到写了一半的内容。"""
    payload = dict(fields)
    payload["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="小红书扫码登录助手")
    parser.add_argument("--status-file", required=True, help="状态 JSON 输出路径")
    parser.add_argument("--qr-file", required=True, help="二维码 PNG 输出路径")
    parser.add_argument("--timeout", type=int, default=600, help="等待扫码的秒数")
    args = parser.parse_args()

    status_path = Path(args.status_file)
    qr_path = Path(args.qr_file)

    write_status(status_path, state="starting", message="正在申请二维码…")

    def render_qr(data: str) -> bool:
        """替换终端的字符块二维码渲染，改为输出 PNG。

        坑：不要用 qrcode.make() 再 resize —— 它返回 PilImage 包装对象，
        .width/.height 不是真实像素尺寸，会把方图拉成细长条导致扫不出来。
        这里用 box_size 原生生成目标尺寸，不做任何缩放。
        """
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        pil = image.get_image() if hasattr(image, "get_image") else image
        pil.convert("RGB").save(qr_path)
        write_status(
            status_path,
            state="waiting",
            message="请用小红书 App 扫码，并在手机上确认登录",
        )
        return True

    ql._display_qr_in_terminal = render_qr

    def on_status(message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        if "Scanned" in text or "📲" in text:
            write_status(
                status_path,
                state="scanned",
                message="已扫码，请在手机上点击确认",
            )
        elif "confirmed" in text.lower() or "✅" in text:
            write_status(
                status_path,
                state="confirmed",
                message="登录已确认，正在保存凭证…",
            )
        # "Still waiting…" 之类的进度噪音不写盘，避免无意义的文件抖动

    try:
        cookies = ql.qrcode_login(
            on_status=on_status,
            timeout_s=args.timeout,
            prefer_browser_assisted=False,
        )
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        detail = str(exc)
        state = "expired" if "timed out" in detail.lower() else "error"
        write_status(
            status_path,
            state=state,
            message="二维码已过期，请重新获取" if state == "expired" else f"登录失败：{detail}",
            error=f"{name}: {detail}",
        )
        print(f"[FAIL] {name}: {detail}", flush=True)
        return 1

    write_status(
        status_path,
        state="confirmed",
        message="登录成功",
    )
    print(f"[OK] 已保存 cookie 字段: {sorted(cookies)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
