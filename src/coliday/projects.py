"""연결된 프로젝트 목록 (`~/.coliday/projects.json`). 새 작업을 시작할 위치를 고르는 데 쓴다.

항목은 이 컴퓨터의 디렉토리이거나 SSH 로 접속하는 서버의 디렉토리(`user@host:/경로`, `remote.py`)다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from coliday import remote


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
    """저장·비교에 쓰는 형태. 로컬은 절대 경로, 서버는 `host:/절대경로` (`host:~/app` 은 접속해서 홈을 알아낸다)."""
    text = str(directory)
    if remote.is_remote(text) and not Path(text).expanduser().exists():  # 콜론이 든 로컬 폴더가 실제로 있으면 로컬이다
        return remote.normalize(text)
    return str(Path(directory).expanduser().resolve())


def exists(location: str) -> bool:
    """목록에 '(없음)' 을 붙일지 정하는 가벼운 확인. 서버는 접속하지 않고 있다고 본다 (화면이 매 초 부른다)."""
    return remote.is_remote(location) or Path(location).is_dir()


def display(directory: str) -> str:
    home = str(Path.home())
    return "~" + directory[len(home):] if directory == home or directory.startswith(home + os.sep) else directory
