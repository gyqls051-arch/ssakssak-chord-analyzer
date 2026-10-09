#!/usr/bin/env python3
"""
음원 코드 진행 / 조성(스케일) 분석기

librosa 기반으로 mp3/wav 파일을 분석해서 대략적인 코드 진행과 조성을 추출한다.

파이프라인 (FMP 교과서 + librosa 공식 예제의 표준 기법):
  1. 하모닉/퍼커시브 분리 (margin=8 강화 분리)
  2. CQT 크로마그램 추출
  3. nn_filter(비국소 필터) + median filter 로 크로마 노이즈 억제
  4. 비트 단위 크로마 집계
  5. 코드 템플릿 코사인 유사도 → softmax 확률화
  6. Viterbi(HMM) 디코딩으로 시퀀스 스무딩
  7. Krumhansl-Schmuckler 프로파일로 조성 추정

※ 참고용 근사치입니다. 디스토션 기타, 신스 레이어가 많은 밴드 음악은 정확도가
   떨어질 수 있습니다. 정밀한 채보를 대체할 수 없습니다.
"""

import argparse
import os
import sys

import numpy as np
import scipy.ndimage
import librosa

# Windows 콘솔(cp949 등)에서 한글이 깨지지 않도록 UTF-8로 강제 설정
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SUPPORTED_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg")

# ---------------------------------------------------------------------------
# 코드 템플릿
# ---------------------------------------------------------------------------

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# quality 접미사 → 인터벌(반음). 트라이어드가 기본, --sevenths 시 7th 3종 추가.
TRIAD_QUALITIES = {
    "": (0, 4, 7),     # major
    "m": (0, 3, 7),    # minor
}
SEVENTH_QUALITIES = {
    "maj7": (0, 4, 7, 11),
    "7": (0, 4, 7, 10),
    "m7": (0, 3, 7, 10),
}

NO_CHORD = "N.C."


def build_templates(use_sevenths: bool):
    """코드 이름 리스트와 L2 정규화된 템플릿 행렬 (n_chords, 12)을 만든다."""
    qualities = dict(TRIAD_QUALITIES)
    if use_sevenths:
        qualities.update(SEVENTH_QUALITIES)

    names = []
    rows = []
    for root in range(12):
        for suffix, intervals in qualities.items():
            vec = np.zeros(12)
            for iv in intervals:
                vec[(root + iv) % 12] = 1.0
            names.append(f"{NOTE_NAMES[root]}{suffix}")
            rows.append(vec / np.linalg.norm(vec))
    return names, np.array(rows)


def split_chord_name(chord_name: str):
    """코드 이름을 (루트, 접미사)로 분리. 'C#m' → ('C#', 'm').

    주의: 'C#'가 'C'보다 먼저 매칭되도록 반드시 긴 이름부터 검사한다.
    """
    if not chord_name or chord_name == NO_CHORD:
        return None, None
    for name in sorted(NOTE_NAMES, key=len, reverse=True):
        if chord_name.startswith(name):
            return name, chord_name[len(name):]
    return None, None


# ---------------------------------------------------------------------------
# 조성(키) 추정 — Krumhansl-Schmuckler
# ---------------------------------------------------------------------------

KS_MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
KS_MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)


def estimate_key(chroma_mean: np.ndarray):
    """곡 전체 평균 크로마와 24개 키 프로파일을 비교해 (키, confidence, 점수표) 반환."""
    major = KS_MAJOR_PROFILE / np.linalg.norm(KS_MAJOR_PROFILE)
    minor = KS_MINOR_PROFILE / np.linalg.norm(KS_MINOR_PROFILE)
    chroma_norm = chroma_mean / (np.linalg.norm(chroma_mean) + 1e-9)

    scores = {}
    for root in range(12):
        scores[f"{NOTE_NAMES[root]} Major"] = float(np.dot(chroma_norm, np.roll(major, root)))
        scores[f"{NOTE_NAMES[root]} Minor"] = float(np.dot(chroma_norm, np.roll(minor, root)))

    best_key = max(scores, key=scores.get)
    sorted_scores = sorted(scores.values(), reverse=True)
    gap = sorted_scores[0] - sorted_scores[1]
    confidence = max(0.0, min(1.0, scores[best_key] * 0.5 + gap * 2.0))
    return best_key, confidence, scores


# ---------------------------------------------------------------------------
# 크로마 추출 (강화 파이프라인)
# ---------------------------------------------------------------------------

def extract_enhanced_chroma(y: np.ndarray, sr: int) -> np.ndarray:
    """librosa 공식 예제의 3단계 강화 크로마.

    1) margin=8 하모닉 분리  2) CQT 크로마  3) nn_filter + median filter
    """
    y_harm = librosa.effects.harmonic(y=y, margin=8)
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr)
    # 비국소 필터: 비슷한 프레임끼리 중앙값 집계 → 지나가는 음/희소 노이즈 제거
    chroma_filt = np.minimum(
        chroma,
        librosa.decompose.nn_filter(chroma, aggregate=np.median, metric="cosine"),
    )
    # 수평(시간축) 미디언 필터로 국소 불연속 억제
    return scipy.ndimage.median_filter(chroma_filt, size=(1, 9))


# ---------------------------------------------------------------------------
# 비트 그리드
# ---------------------------------------------------------------------------

FALLBACK_GRID_SEC = 0.5    # 비트 검출 실패 시 균등 그리드 간격
MAX_BEAT_INTERVAL = 1.5    # 비트 간격이 이보다 크면(BPM<40) 신뢰 불가로 판단


def compute_beat_frames(y: np.ndarray, sr: int, n_frames: int, hop_length: int = 512):
    """비트 경계 프레임 인덱스와 BPM을 반환. 검출 실패 시 균등 그리드로 대체."""
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop_length)
    tempo_val = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop_length)

    unreliable = (
        len(beat_times) < 4
        or tempo_val <= 0
        or (len(beat_times) >= 2 and np.median(np.diff(beat_times)) > MAX_BEAT_INTERVAL)
    )
    if unreliable:
        duration = librosa.frames_to_time(n_frames, sr=sr, hop_length=hop_length)
        grid_times = np.arange(0, duration, FALLBACK_GRID_SEC)
        beat_frames = librosa.time_to_frames(grid_times, sr=sr, hop_length=hop_length)
        tempo_val = 0.0

    boundaries = np.unique(np.clip(
        np.concatenate([[0], np.atleast_1d(beat_frames), [n_frames]]), 0, n_frames
    )).astype(int)
    return boundaries, tempo_val


def aggregate_chroma(chroma: np.ndarray, boundaries: np.ndarray) -> np.ndarray:
    """경계 프레임 사이 구간별 평균 크로마 (12, n_segments)."""
    cols = []
    for s, e in zip(boundaries[:-1], boundaries[1:]):
        cols.append(chroma[:, s:e].mean(axis=1) if e > s else np.zeros(12))
    return np.array(cols).T


# ---------------------------------------------------------------------------
# 코드 인식 — 템플릿 매칭 + Viterbi 디코딩
# ---------------------------------------------------------------------------

SOFTMAX_GAMMA = 12.0       # 유사도 → 확률 변환 시 첨예도
SELF_TRANSITION = 0.9      # Viterbi 자기유지 확률 (높을수록 코드 변화에 보수적)
ENERGY_THRESHOLD = 1e-4    # 이보다 약한 구간은 N.C.(무화음) 처리


def chord_probabilities(seg_chroma: np.ndarray, templates: np.ndarray) -> np.ndarray:
    """구간별 크로마 → 코드 사후확률 (n_chords, n_segments). 열 합 = 1."""
    norms = np.linalg.norm(seg_chroma, axis=0, keepdims=True)
    unit = seg_chroma / np.maximum(norms, 1e-9)
    sim = templates @ unit                      # (n_chords, T), 값 범위 [0, 1]
    logits = SOFTMAX_GAMMA * sim
    logits -= logits.max(axis=0, keepdims=True)  # 수치 안정화
    prob = np.exp(logits)
    return prob / prob.sum(axis=0, keepdims=True)


def decode_chords(seg_chroma: np.ndarray, names, templates: np.ndarray):
    """Viterbi 디코딩으로 구간별 코드 라벨을 결정. 무음 구간은 N.C."""
    prob = chord_probabilities(seg_chroma, templates)
    transition = librosa.sequence.transition_loop(len(names), SELF_TRANSITION)
    states = librosa.sequence.viterbi_discriminative(prob, transition)
    labels = [names[s] for s in states]

    energies = np.linalg.norm(seg_chroma, axis=0)
    return [NO_CHORD if e < ENERGY_THRESHOLD else lab for lab, e in zip(labels, energies)]


def smooth_chords(chord_seq, min_duration_beats=2):
    """Viterbi 후에도 남은, 너무 짧게 튀는 코드를 이웃 구간에 흡수시킨다."""
    if not chord_seq or min_duration_beats <= 1:
        return list(chord_seq)

    segments = []
    cur, start = chord_seq[0], 0
    for i in range(1, len(chord_seq)):
        if chord_seq[i] != cur:
            segments.append([cur, start, i - 1])
            cur, start = chord_seq[i], i
    segments.append([cur, start, len(chord_seq) - 1])

    changed = True
    while changed and len(segments) > 1:
        changed = False
        for i, (chord, s, e) in enumerate(segments):
            if e - s + 1 < min_duration_beats:
                left = segments[i - 1][2] - segments[i - 1][1] + 1 if i > 0 else -1
                right = segments[i + 1][2] - segments[i + 1][1] + 1 if i < len(segments) - 1 else -1
                if left == -1 and right == -1:
                    continue
                if right >= left:
                    segments[i + 1][1] = s
                else:
                    segments[i - 1][2] = e
                del segments[i]
                changed = True
                break

    out = [None] * len(chord_seq)
    for chord, s, e in segments:
        for i in range(s, e + 1):
            out[i] = chord
    return out


# ---------------------------------------------------------------------------
# 흔한 코드 진행 패턴 감지
# ---------------------------------------------------------------------------

ROMAN_MAJOR = ["I", "ii", "iii", "IV", "V", "vi", "vii°"]
ROMAN_MINOR = ["i", "ii°", "III", "iv", "v", "VI", "VII"]

SCALE_DEGREES_MAJOR = {0: 0, 2: 1, 4: 2, 5: 3, 7: 4, 9: 5, 11: 6}
SCALE_DEGREES_MINOR = {0: 0, 2: 1, 3: 2, 5: 3, 7: 4, 8: 5, 10: 6}

COMMON_PATTERNS = {
    "I-V-vi-IV": ["I", "V", "vi", "IV"],
    "vi-IV-I-V": ["vi", "IV", "I", "V"],
    "I-IV-V": ["I", "IV", "V"],
    "ii-V-I": ["ii", "V", "I"],
    "I-vi-IV-V": ["I", "vi", "IV", "V"],
}


def chord_to_roman(chord_name, key_root, is_major_key):
    """코드 이름을 키 기준 로마 숫자 디그리로 변환. 다이어토닉이 아니면 None."""
    root_name, _suffix = split_chord_name(chord_name)
    if root_name is None:
        return None

    degree = (NOTE_NAMES.index(root_name) - NOTE_NAMES.index(key_root)) % 12
    table = SCALE_DEGREES_MAJOR if is_major_key else SCALE_DEGREES_MINOR
    roman = ROMAN_MAJOR if is_major_key else ROMAN_MINOR
    return roman[table[degree]] if degree in table else None


def detect_common_patterns(roman_seq):
    cleaned = [r for r in roman_seq if r is not None]
    dedup = []
    for r in cleaned:
        if not dedup or dedup[-1] != r:
            dedup.append(r)

    found = []
    for pattern_name, pattern in COMMON_PATTERNS.items():
        plen = len(pattern)
        if any(dedup[i:i + plen] == pattern for i in range(len(dedup) - plen + 1)):
            found.append(pattern_name)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------

def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def format_bpm(tempo_val: float) -> str:
    return "감지 실패" if tempo_val <= 0 else f"{tempo_val:.1f}"


def merge_labels_to_segments(labels, boundary_times):
    """구간별 라벨 → (코드, 시작초, 끝초) 세그먼트 목록으로 병합."""
    segments = []
    cur, start_idx = labels[0], 0
    for i in range(1, len(labels)):
        if labels[i] != cur:
            segments.append((cur, boundary_times[start_idx], boundary_times[i]))
            cur, start_idx = labels[i], i
    segments.append((cur, boundary_times[start_idx], boundary_times[len(labels)]))
    return segments


# ---------------------------------------------------------------------------
# 메인 분석 루틴
# ---------------------------------------------------------------------------

def analyze(input_path: str, output_path: str = None, use_sevenths: bool = False,
            min_duration_beats: int = 2, visualize: bool = False):
    """음원을 분석해서 결과 dict를 반환.

    output_path 가 None 이면 파일 저장 없이 분석만 한다 (GUI 내보내기 버튼용).
    반환: {"segments", "key", "confidence", "duration", "tempo", "report_lines"}
    """
    if not os.path.isfile(input_path):
        print(f"[오류] 파일을 찾을 수 없습니다: {input_path}", file=sys.stderr)
        sys.exit(1)

    ext = os.path.splitext(input_path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        print(f"[오류] 지원하지 않는 포맷입니다: {ext} ({'/'.join(e[1:] for e in SUPPORTED_EXTS)} 지원)",
              file=sys.stderr)
        sys.exit(1)

    print(f"[1/6] 오디오 로드 중: {input_path}")
    try:
        y, sr = librosa.load(input_path, sr=None, mono=True)
    except Exception as e:
        print(f"[오류] 오디오 로드 실패: {e}", file=sys.stderr)
        sys.exit(1)

    if y is None or len(y) == 0:
        print("[오류] 오디오 데이터가 비어 있습니다.", file=sys.stderr)
        sys.exit(1)

    duration = librosa.get_duration(y=y, sr=sr)
    print(f"       길이: {format_time(duration)} / 샘플레이트: {sr}Hz")

    print("[2/6] 강화 크로마 추출 중 (하모닉 분리 + 노이즈 필터링)...")
    chroma = extract_enhanced_chroma(y, sr)

    print("[3/6] 비트/템포 감지 중...")
    boundaries, tempo_val = compute_beat_frames(y, sr, chroma.shape[1])
    boundary_times = librosa.frames_to_time(boundaries, sr=sr)
    if tempo_val <= 0:
        print(f"       비트 검출 실패 → {FALLBACK_GRID_SEC}초 간격 균등 그리드로 대체")
    print(f"       추정 BPM: {format_bpm(tempo_val)}, 분석 구간 수: {len(boundaries) - 1}")

    print("[4/6] 비트 단위 크로마 집계 중...")
    seg_chroma = aggregate_chroma(chroma, boundaries)

    print("[5/6] 코드 인식(Viterbi) 및 조성 추정 중...")
    names, templates = build_templates(use_sevenths)
    labels = decode_chords(seg_chroma, names, templates)
    labels = smooth_chords(labels, min_duration_beats=min_duration_beats)

    chroma_mean = chroma.mean(axis=1)
    best_key, confidence, _ = estimate_key(chroma_mean)

    print("[6/6] 결과 정리 중...")
    segments = merge_labels_to_segments(labels, boundary_times)

    # --- 출력 ---
    print()
    print("=" * 60)
    print("코드 진행 분석 결과 (참고용 근사치입니다)")
    print("=" * 60)
    print(f"파일: {input_path}")
    print(f"길이: {format_time(duration)}   추정 BPM: {format_bpm(tempo_val)}")
    print(f"추정 조성: {best_key}  (confidence: {confidence:.2f})")
    print("-" * 60)

    lines = [
        "코드 진행 분석 결과 (참고용 근사치)",
        f"파일: {input_path}",
        f"길이: {format_time(duration)}   추정 BPM: {format_bpm(tempo_val)}",
        f"추정 조성: {best_key}  (confidence: {confidence:.2f})",
        "",
        "코드 진행:",
    ]

    display = ""
    for chord, t0, t1 in segments:
        display += f"[{format_time(t0)}-{format_time(t1)}] {chord}  "
        lines.append(f"[{format_time(t0)}-{format_time(t1)}] {chord}")
    print(display.strip())
    print("-" * 60)

    key_root, key_quality = best_key.split(" ")
    roman_seq = [chord_to_roman(c, key_root, key_quality == "Major") for c, _, _ in segments]
    patterns = detect_common_patterns(roman_seq)

    pattern_msg = f"감지된 흔한 코드 패턴: {', '.join(patterns) if patterns else '없음'}"
    print(pattern_msg)
    lines += ["", pattern_msg, "",
              "※ 본 결과는 자동 분석에 의한 참고용 근사치이며, 실제 코드와 다를 수 있습니다."]

    if output_path:
        write_report(lines, output_path)
        if visualize:
            img_path = os.path.splitext(output_path)[0] + "_timeline.png"
            save_visualization(segments, duration, input_path, img_path)

    print("\n(주의) 이 결과는 참고용 근사치입니다. 디스토션 기타나 신스 레이어가 많은")
    print("       밴드 음악은 정확도가 떨어질 수 있습니다.")
    return {
        "segments": segments,
        "key": best_key,
        "confidence": confidence,
        "duration": duration,
        "tempo": tempo_val,
        "report_lines": lines,
    }


def write_report(lines, output_path: str) -> bool:
    """결과 텍스트를 파일로 저장."""
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"결과가 저장되었습니다: {output_path}")
        return True
    except OSError as e:
        print(f"[오류] 결과 저장 실패: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# MIDI 내보내기
# ---------------------------------------------------------------------------

MIDI_VELOCITY = 80
MIDI_BASE_OCTAVE = 4  # 코드 루트를 C4(=MIDI 60) 옥타브에 배치


def chord_to_midi_notes(chord_name: str):
    """코드 이름 → MIDI 노트 번호 리스트. N.C./알 수 없는 코드는 빈 리스트."""
    root, suffix = split_chord_name(chord_name)
    if root is None:
        return []
    qualities = {**TRIAD_QUALITIES, **SEVENTH_QUALITIES}
    intervals = qualities.get(suffix)
    if intervals is None:
        return []
    root_midi = 12 * (MIDI_BASE_OCTAVE + 1) + NOTE_NAMES.index(root)  # C4 = 60
    return [root_midi + iv for iv in intervals]


def save_midi(segments, midi_path: str, tempo_bpm: float = 0):
    """감지된 코드 세그먼트를 MIDI 파일로 저장.

    tempo_bpm(곡의 실제 BPM)을 MIDI 템포로 기록하고, 코드 경계를
    정수 비트로 스냅(퀀타이즈)한다 → DAW에서 마디/그리드에 딱 맞게 정렬된다.
    비트 검출값은 완벽한 격자가 아니므로 스냅하지 않으면 코드가 4.3비트
    같은 어중간한 위치에 찍혀 마디가 안 맞아 보인다.
    BPM을 모르면(감지 실패) 60 BPM(1비트=1초)으로 기록하고 스냅하지 않는다.
    """
    from midiutil import MIDIFile

    quantize = bool(tempo_bpm and tempo_bpm > 0)
    bpm = tempo_bpm if quantize else 60
    beats_per_sec = bpm / 60.0

    mf = MIDIFile(1)
    mf.addTempo(track=0, time=0, tempo=bpm)
    mf.addTimeSignature(track=0, time=0, numerator=4, denominator=2,
                        clocks_per_tick=24)  # 4/4
    mf.addProgramChange(0, 0, 0, 0)  # Acoustic Grand Piano

    written = 0
    for chord, t0, t1 in segments:
        if quantize:
            start_beat = round(t0 * beats_per_sec)
            end_beat = max(round(t1 * beats_per_sec), start_beat + 1)
        else:
            start_beat = t0 * beats_per_sec
            end_beat = start_beat + max((t1 - t0) * beats_per_sec, 0.1)
        notes = chord_to_midi_notes(chord)
        if notes:
            # 루트음을 한 옥타브 아래 베이스로 깔아 원곡처럼 들리게 한다
            notes = [notes[0] - 12] + notes
        for note in notes:
            mf.addNote(track=0, channel=0, pitch=note, time=start_beat,
                       duration=end_beat - start_beat, volume=MIDI_VELOCITY)
            written += 1

    if written == 0:
        print("[안내] MIDI로 내보낼 코드가 없습니다 (전부 N.C.).")
        return False

    with open(midi_path, "wb") as f:
        mf.writeFile(f)
    print(f"MIDI 파일이 저장되었습니다: {midi_path} (템포 {bpm:.0f} BPM, 비트 단위 정렬)")
    return True


# ---------------------------------------------------------------------------
# 시각화
# ---------------------------------------------------------------------------

def save_visualization(segments, duration, input_path, img_path):
    """코드 타임라인을 PNG 이미지로 저장. 성공 시 True."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[안내] matplotlib이 설치되어 있지 않아 시각화를 건너뜁니다.")
        return False

    # Windows 한글 폰트 (없으면 기본 폰트로 진행, 경고만 억제)
    if sys.platform == "win32":
        plt.rcParams["font.family"] = ["Malgun Gothic", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(max(8, duration / 5), 2.5))
    unique_chords = sorted(set(c for c, _, _ in segments))
    cmap = plt.get_cmap("tab20")
    colors = {c: cmap(i % 20) for i, c in enumerate(unique_chords)}

    for chord, t0, t1 in segments:
        ax.barh(0, t1 - t0, left=t0, height=0.8, color=colors[chord], edgecolor="white")
        if t1 - t0 > duration * 0.01:
            ax.text((t0 + t1) / 2, 0, chord, ha="center", va="center", fontsize=8)

    ax.set_xlim(0, duration)
    ax.set_yticks([])
    ax.set_xlabel("시간 (초)")
    ax.set_title(f"코드 타임라인 — {os.path.basename(input_path)}")

    plt.tight_layout()
    try:
        plt.savefig(img_path, dpi=150)
        print(f"코드 타임라인 이미지가 저장되었습니다: {img_path}")
        return True
    except OSError as e:
        print(f"[오류] 이미지 저장 실패: {e}", file=sys.stderr)
        return False
    finally:
        plt.close(fig)


# ---------------------------------------------------------------------------
# 진입점 (CLI / 더블클릭 / 드래그 앤 드롭)
# ---------------------------------------------------------------------------

def _run_and_pause(input_path: str):
    """더블클릭/드롭 실행용: 분석 후 Enter 대기 (창이 바로 닫히지 않게)."""
    input_path = input_path.strip('"').strip("'").strip()
    base = os.path.splitext(input_path)[0]
    try:
        analyze(
            input_path=input_path,
            output_path=base + "_chords.txt",
            use_sevenths=False,
            min_duration_beats=2,
            visualize=True,
        )
    except SystemExit:
        pass
    except Exception as e:
        print(f"\n[오류] 분석 중 문제가 발생했습니다: {e}")
    print()
    try:
        input("Enter 키를 누르면 창이 닫힙니다...")
    except (EOFError, KeyboardInterrupt):
        pass


def interactive_main():
    """더블클릭(인자 없이 실행): 파일 경로를 물어본 뒤 분석."""
    print("=" * 60)
    print("  음원 코드/조성 분석기")
    print("=" * 60)
    print()
    print("분석할 mp3/wav 파일을 이 창으로 드래그 앤 드롭 한 뒤 Enter를 누르세요.")
    print("(또는 파일 경로를 직접 입력)")
    print()

    while True:
        try:
            raw = input("파일 경로: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        path = raw.strip('"').strip("'").strip()
        if not path:
            continue
        if not os.path.isfile(path):
            print(f"[오류] 파일을 찾을 수 없습니다: {path}")
            print("다시 입력해주세요.\n")
            continue
        break

    _run_and_pause(path)


def main():
    # 인자 없이 실행(더블클릭 포함) → 대화형 모드
    if len(sys.argv) == 1:
        interactive_main()
        return

    # exe 아이콘 위에 파일을 드래그 앤 드롭 → 경로가 그대로 첫 인자로 들어옴
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        _run_and_pause(sys.argv[1])
        return

    parser = argparse.ArgumentParser(
        description="오디오 파일의 코드 진행과 조성을 분석합니다 (참고용 근사치).",
    )
    parser.add_argument("--input", required=True, help="입력 오디오 파일 경로 (mp3/wav/flac/m4a/ogg)")
    parser.add_argument("--output", default="chords.txt", help="결과를 저장할 텍스트 파일 경로 (기본: chords.txt)")
    parser.add_argument("--sevenths", action="store_true", help="7th 코드까지 확장해서 인식 (기본은 트라이어드만)")
    parser.add_argument("--min-duration", type=int, default=2, help="스무딩 최소 지속 비트 수 (기본: 2)")
    parser.add_argument("--visualize", action="store_true", help="코드 타임라인 이미지(PNG)도 함께 저장")
    parser.add_argument("--midi", action="store_true", help="코드 진행을 MIDI 파일로도 저장")
    args = parser.parse_args()

    result = analyze(
        input_path=args.input,
        output_path=args.output,
        use_sevenths=args.sevenths,
        min_duration_beats=args.min_duration,
        visualize=args.visualize,
    )
    if args.midi:
        save_midi(result["segments"], os.path.splitext(args.output)[0] + ".mid",
                  tempo_bpm=result["tempo"])


if __name__ == "__main__":
    main()
