#!/usr/bin/env python3
"""下载文字度量所需的 Noto 字体到 backend/fonts/（不提交仓库）。

校验已存在文件；使用固定来源、有限超时和重试。默认失败退出。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"

FONTS = json.loads(Path(__file__).with_name("fonts.json").read_text(encoding="utf-8"))
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_ATTEMPTS = 3


def _checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def fetch(name: str, url: str, sha256: str) -> None:
    target = FONTS_DIR / name
    if target.is_file() and _checksum(target) == sha256:
        print(f"skip  {name}（校验通过）")
        return
    target.unlink(missing_ok=True)
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"get   {name}")
    print(f"      {url}")
    tmp = target.with_suffix(target.suffix + ".tmp")
    # urlopen's timeout bounds socket operations; the deadline also bounds
    # continuous chunk reads, without relying on urlretrieve's global timeout.
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                with tmp.open("wb") as output:
                    while chunk := response.read(64 * 1024):
                        if time.monotonic() > deadline:
                            raise TimeoutError("字体下载超过时间限制")
                        output.write(chunk)
            if _checksum(tmp) != sha256:
                raise ValueError(f"{name} SHA-256 校验失败")
            tmp.replace(target)
            print(f"ok    {name}（{target.stat().st_size} bytes）")
            return
        except Exception as error:
            tmp.unlink(missing_ok=True)
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            print(f"retry {name}（{attempt}/{DOWNLOAD_ATTEMPTS}）：{error}", file=sys.stderr)
            time.sleep(attempt)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-missing", action="store_true", help="显式允许缺字体降级")
    args = parser.parse_args(argv)
    missing = []
    for name, spec in FONTS.items():
        try:
            fetch(name, spec["url"], spec["sha256"])
        except Exception as error:
            print(f"fail  {name}: {error}", file=sys.stderr)
            if not args.allow_missing:
                return 1
            # A stale or corrupt target must not be mistaken for a usable font.
            (FONTS_DIR / name).unlink(missing_ok=True)
            missing.append(name)
    if missing:
        print(f"warning: 缺字体降级，文字度量将使用估算：{', '.join(missing)}", file=sys.stderr)
        return 0
    print(f"fonts ready → {FONTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
