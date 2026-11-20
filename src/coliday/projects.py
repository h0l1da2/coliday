"""연결된 프로젝트 디렉토리 목록 (`~/.coliday/projects.json`). 새 작업을 시작할 위치를 고르는 데 쓴다."""

from __future__ import annotations

import json
import os
from pathlib import Path


def load(path: Path) -> dict[str, str | None]:
    """경로 → 사용자가 붙인 이름(없으면 None). 순서는 추가한 순서다.

    항목은 {"path": ..., "name": ...} 이고, 이름 없이 경로 문자열만 적은 항목도 읽는다.
    """
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, str | None] = {}
    for item in data if isinstance(data, list) else [None]:
        entry = {"path": item} if isinstance(item, str) else item
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"] \
                or not (name is None or isinstance(name, str)):
            raise ValueError(f'{path}: [{{"path": "디렉토리", "name": "이름"}}] 형식의 JSON 배열이어야 합니다')
        out.setdefault(entry["path"], name or None)
    return out


def save(path: Path, items: dict[str, str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # 다른 터미널이 반쯤 쓴 파일을 읽지 않게 교체한다
    data = [{"path": p, "name": n} for p, n in items.items()]
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def normalize(directory: str | Path) -> str:
    return str(Path(directory).expanduser().resolve())


def display(directory: str) -> str:
    home = str(Path.home())
    return "~" + directory[len(home):] if directory == home or directory.startswith(home + os.sep) else directory
