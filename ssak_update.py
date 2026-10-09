"""싹싹김치 공통 업데이트 체크 — PyInstaller 단일파일 앱용 경량 버전.

각 앱 리포 루트의 ``version.json`` 을 raw.githubusercontent.com 에서 읽어
새 버전 / 최소 지원 버전 / 킬스위치를 판정한다. 비용 0원: GitHub raw는 무료.

version.json 형식 (싹싹김치 비디오 다운로더와 동일):
    {
      "latest_version": "1.2.3",
      "min_supported_version": "1.0.0",       # 이 미만이면 강제 업데이트
      "killswitch": false,                     # true면 치명적 문제로 사용 중지 안내
      "killswitch_message": "...",
      "download_url": "https://github.com/<org>/<repo>/releases/latest",
      "release_notes": "무엇이 달라졌는지 한 줄 요약"
    }

사용:
    from ssak_update import check_for_update

    # GUI 스레드를 건드리지 않고 백그라운드에서 확인 → 결과는 콜백으로
    check_for_update(
        current="1.0.0",
        info_url="https://raw.githubusercontent.com/<org>/<repo>/main/version.json",
        on_result=lambda d: bridge.update_signal.emit(d),   # Qt 쪽은 시그널로 marshal
    )

``on_result`` 는 워커 스레드에서 호출된다 — Qt 위젯을 직접 만지지 말 것.
반환 dict:
    kind:    "killswitch" | "force_update" | "new_version" | "uptodate" | "error"
    message: 사용자에게 보여줄 한국어 문장
    latest:  최신 버전 문자열 (없으면 "")
    download_url / release_notes
"""
from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

CHECK_TIMEOUT = 4.0          # 초 — 시작 지연 방지
MAX_BYTES = 10_000           # 응답 크기 제한 (메모리 보호)


def parse_version(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for tok in str(v).strip().lstrip("vV").split("."):
        num = ""
        for ch in tok:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def compare_versions(a: str, b: str) -> int:
    """a > b → 1, a == b → 0, a < b → -1."""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta += (0,) * (n - len(ta))
    tb += (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def _is_safe_http_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


@dataclass
class UpdateDecision:
    kind: str                        # killswitch|force_update|new_version|uptodate|error
    message: str = ""
    latest: str = ""
    download_url: str = ""
    release_notes: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "latest": self.latest,
            "download_url": self.download_url,
            "release_notes": self.release_notes,
        }


def fetch_version_json(url: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """원격 version.json을 읽는다. 실패 시 예외를 던진다 — 호출자가 잡을 것."""
    req = urllib.request.Request(url, headers={"User-Agent": "ssakssak-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("version.json oversized")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("version.json is not an object")
    return data


def decide(current: str, data: dict) -> UpdateDecision:
    """version.json 내용 → UpdateDecision."""
    dl = data.get("download_url")
    download_url = dl if _is_safe_http_url(dl) else ""
    notes = str(data.get("release_notes") or "")

    if data.get("killswitch"):
        return UpdateDecision(
            kind="killswitch",
            message=str(data.get("killswitch_message")
                        or "중요한 문제가 발견되어 이 버전은 사용이 중지되었습니다."),
            download_url=download_url, release_notes=notes,
        )

    latest = str(data.get("latest_version") or "")
    if compare_versions(current, str(data.get("min_supported_version", "0"))) < 0:
        return UpdateDecision(
            kind="force_update",
            message="이 버전은 지원이 종료되었습니다. 새 버전을 설치해 주세요.",
            latest=latest, download_url=download_url, release_notes=notes,
        )

    if latest and compare_versions(latest, current) > 0:
        return UpdateDecision(
            kind="new_version",
            message=f"새 버전 v{latest} 이 있습니다.",
            latest=latest, download_url=download_url, release_notes=notes,
        )

    return UpdateDecision(kind="uptodate", latest=latest)


def check_for_update(
    current: str,
    info_url: str,
    on_result,
    timeout: float = CHECK_TIMEOUT,
) -> threading.Thread:
    """백그라운드에서 version.json을 확인하고 ``on_result(dict)`` 를 호출한다.

    ``on_result`` 는 워커 스레드에서 실행된다 — Qt 위젯 접근 금지,
    시그널/큐로 GUI 스레드에 전달할 것.
    """
    def _run() -> None:
        try:
            data = fetch_version_json(info_url, timeout=timeout)
            result = decide(current, data).as_dict()
        except Exception as exc:  # 네트워크 실패는 조용히 — 오프라인도 정상 사용
            result = UpdateDecision(kind="error", message=str(exc)).as_dict()
        try:
            on_result(result)
        except Exception:
            pass

    t = threading.Thread(target=_run, name="ssak-update-check", daemon=True)
    t.start()
    return t
