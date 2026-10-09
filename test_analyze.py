#!/usr/bin/env python3
"""analyze.py 테스트 스위트.

실행:  python test_analyze.py        (또는 pytest test_analyze.py)

유닛 테스트(순수 로직) + 합성 오디오 통합 테스트로 구성.
코드 수정 후 반드시 이 테스트를 돌려서 회귀가 없는지 확인할 것.
"""

import os
import subprocess
import sys
import tempfile
import wave
import struct
import math

import numpy as np

import analyze


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------------------
# 1. 템플릿
# ---------------------------------------------------------------------------

def test_templates():
    print("\n[1] 코드 템플릿")
    names, templates = analyze.build_templates(use_sevenths=False)
    check("트라이어드 템플릿 24개", len(names) == 24, f"got {len(names)}")
    check("템플릿 행렬 shape (24, 12)", templates.shape == (24, 12))
    check("모든 템플릿 L2 정규화", np.allclose(np.linalg.norm(templates, axis=1), 1.0))

    names7, templates7 = analyze.build_templates(use_sevenths=True)
    check("7th 포함 템플릿 60개", len(names7) == 60, f"got {len(names7)}")

    # 순수 템플릿 벡터를 넣으면 자기 자신이 최고 유사도여야 함
    idx_c = names.index("C")
    prob = analyze.chord_probabilities(templates[idx_c].reshape(12, 1), templates)
    check("C 템플릿 → C 코드 매칭", int(prob[:, 0].argmax()) == idx_c)

    idx_am = names.index("Am")
    prob = analyze.chord_probabilities(templates[idx_am].reshape(12, 1), templates)
    check("Am 템플릿 → Am 코드 매칭", int(prob[:, 0].argmax()) == idx_am)


# ---------------------------------------------------------------------------
# 2. 코드 이름 파싱 / 로마 숫자 변환 (샤프 루트 버그 회귀 테스트)
# ---------------------------------------------------------------------------

def test_chord_name_parsing():
    print("\n[2] 코드 이름 파싱")
    check("C → (C, '')", analyze.split_chord_name("C") == ("C", ""))
    check("C#m → (C#, m)  ※샤프 루트 버그 회귀", analyze.split_chord_name("C#m") == ("C#", "m"))
    check("A#maj7 → (A#, maj7)", analyze.split_chord_name("A#maj7") == ("A#", "maj7"))
    check("N.C. → (None, None)", analyze.split_chord_name(analyze.NO_CHORD) == (None, None))
    check("빈 문자열 → (None, None)", analyze.split_chord_name("") == (None, None))


def test_roman_conversion():
    print("\n[3] 로마 숫자 변환")
    check("C major 키에서 C → I", analyze.chord_to_roman("C", "C", True) == "I")
    check("C major 키에서 Am → vi", analyze.chord_to_roman("Am", "C", True) == "vi")
    check("C major 키에서 G → V", analyze.chord_to_roman("G", "C", True) == "V")
    check("A major 키에서 C#m → iii  ※샤프 루트 버그 회귀",
          analyze.chord_to_roman("C#m", "A", True) == "iii")
    check("A minor 키에서 Am → i", analyze.chord_to_roman("Am", "A", False) == "i")
    check("논다이어토닉 → None", analyze.chord_to_roman("C#", "C", True) is None)
    check("N.C. → None", analyze.chord_to_roman(analyze.NO_CHORD, "C", True) is None)


# ---------------------------------------------------------------------------
# 3. 조성 추정
# ---------------------------------------------------------------------------

def test_key_estimation():
    print("\n[4] 조성 추정")
    # C major 스케일 음만 있는 크로마 → C Major
    chroma = np.zeros(12)
    for pc in [0, 2, 4, 5, 7, 9, 11]:
        chroma[pc] = 1.0
    chroma[0] = 2.0  # 토닉 강조
    key, conf, _ = analyze.estimate_key(chroma)
    check("C major 스케일 → C Major", key == "C Major", f"got {key}")
    check("confidence 0~1 범위", 0.0 <= conf <= 1.0)

    # A minor 프로파일 회전 → A Minor
    chroma = np.roll(analyze.KS_MINOR_PROFILE, 9)
    key, _, _ = analyze.estimate_key(chroma)
    check("A minor 프로파일 → A Minor", key == "A Minor", f"got {key}")


# ---------------------------------------------------------------------------
# 4. 스무딩 / 세그먼트 병합
# ---------------------------------------------------------------------------

def test_smoothing():
    print("\n[5] 스무딩")
    seq = ["C"] * 8 + ["G"] * 1 + ["C"] * 8  # 1비트짜리 튐
    out = analyze.smooth_chords(seq, min_duration_beats=2)
    check("1비트 튐 제거", "G" not in out, f"got {out}")

    seq = ["C"] * 4 + ["G"] * 4
    out = analyze.smooth_chords(seq, min_duration_beats=2)
    check("정상 구간 유지", out == seq)

    check("빈 시퀀스 안전", analyze.smooth_chords([], 2) == [])
    check("min_duration=1이면 원본 유지", analyze.smooth_chords(["C", "G"], 1) == ["C", "G"])


def test_pattern_detection():
    print("\n[6] 패턴 감지")
    found = analyze.detect_common_patterns(["I", "V", "vi", "IV"])
    check("I-V-vi-IV 감지", "I-V-vi-IV" in found)
    found = analyze.detect_common_patterns(["ii", "V", "I"])
    check("ii-V-I 감지", "ii-V-I" in found)
    found = analyze.detect_common_patterns(["I", None, "V", None, "vi", "IV"])
    check("None 섞여도 감지", "I-V-vi-IV" in found)
    check("빈 시퀀스 안전", analyze.detect_common_patterns([]) == [])


# ---------------------------------------------------------------------------
# 5. 통합 테스트 — 합성 오디오
# ---------------------------------------------------------------------------

def make_test_wav(path, progression=("C", "Am", "F", "G"), bar_sec=2.0, sr=22050, repeats=2):
    freqs = {
        "C": [261.63, 329.63, 392.00],
        "Am": [220.00, 261.63, 329.63],
        "F": [174.61, 220.00, 261.63],
        "G": [196.00, 246.94, 293.66],
    }
    frames = []
    for name in list(progression) * repeats:
        n = int(sr * bar_sec)
        for i in range(n):
            t = i / sr
            v = sum(math.sin(2 * math.pi * f * t) for f in freqs[name]) / len(freqs[name])
            # 클릭 방지 페이드
            env = min(1.0, i / (sr * 0.02), (n - i) / (sr * 0.02))
            frames.append(int(v * env * 0.4 * 32767))
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))


def test_end_to_end():
    print("\n[7] 통합 테스트 (합성 C-Am-F-G 음원)")
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "synth.wav")
        out = os.path.join(tmp, "chords.txt")
        make_test_wav(wav)

        result = analyze.analyze(wav, out, visualize=False)

        detected = [c for c, _, _ in result["segments"] if c != analyze.NO_CHORD]
        check("조성 = C Major", result["key"] == "C Major", f"got {result['key']}")
        for expected in ["C", "Am", "F", "G"]:
            check(f"코드 {expected} 검출", expected in detected, f"detected={detected}")
        check("결과 파일 생성", os.path.isfile(out))
        with open(out, encoding="utf-8") as f:
            content = f.read()
        check("결과 파일에 조성 기록", "C Major" in content)
        check("report_lines 반환", any("C Major" in ln for ln in result["report_lines"]))

        # output_path=None 이면 파일을 만들지 않아야 함 (GUI 내보내기 방식)
        before = set(os.listdir(tmp))
        analyze.analyze(wav, output_path=None)
        after = set(os.listdir(tmp))
        check("output_path=None → 파일 자동 생성 없음", before == after,
              f"new files: {after - before}")


# ---------------------------------------------------------------------------
# 6. MIDI 내보내기
# ---------------------------------------------------------------------------

def test_midi_export():
    print("\n[7.5] MIDI 내보내기")
    check("C 코드 → 60,64,67 (C4/E4/G4)", analyze.chord_to_midi_notes("C") == [60, 64, 67])
    check("Am 코드 → 69,72,76", analyze.chord_to_midi_notes("Am") == [69, 72, 76])
    check("C#m 코드 루트 = 61  ※샤프 루트", analyze.chord_to_midi_notes("C#m")[0] == 61)
    check("N.C. → 빈 리스트", analyze.chord_to_midi_notes(analyze.NO_CHORD) == [])

    with tempfile.TemporaryDirectory() as tmp:
        mid = os.path.join(tmp, "out.mid")
        segments = [("C", 0.0, 2.0), ("Am", 2.0, 4.0), ("F", 4.0, 6.0), ("G", 6.0, 8.0)]
        ok = analyze.save_midi(segments, mid)
        check("MIDI 저장 성공 반환", ok is True)
        check("MIDI 파일 생성", os.path.isfile(mid))
        with open(mid, "rb") as f:
            header = f.read(4)
        check("유효한 MIDI 헤더(MThd)", header == b"MThd", f"got {header}")

        # 전부 N.C.면 저장하지 않음
        mid2 = os.path.join(tmp, "empty.mid")
        ok = analyze.save_midi([(analyze.NO_CHORD, 0.0, 4.0)], mid2)
        check("전부 N.C. → 저장 안 함", ok is False and not os.path.isfile(mid2))

        # 심층 검증 (mido 있을 때만): 템포/노트/타이밍이 정확히 기록됐는지
        try:
            import mido
        except ImportError:
            print("  [SKIP] mido 미설치 — 심층 MIDI 검증 생략")
            return

        mid3 = os.path.join(tmp, "deep.mid")
        # 120BPM 곡에서 0~2초 C, 2~4초 Am 이라고 가정
        analyze.save_midi([("C", 0.0, 2.0), ("Am", 2.0, 4.0)], mid3, tempo_bpm=120)
        mf = mido.MidiFile(mid3)

        tempos = [msg.tempo for track in mf.tracks for msg in track if msg.type == "set_tempo"]
        check("템포 이벤트 = 120 BPM", tempos and round(mido.tempo2bpm(tempos[0])) == 120,
              f"got {tempos}")

        # 절대 시간(초) 기준 노트온 수집
        events = []
        for track in mf.tracks:
            t_ticks = 0
            for msg in track:
                t_ticks += msg.time
                if msg.type == "note_on" and msg.velocity > 0:
                    sec = mido.tick2second(t_ticks, mf.ticks_per_beat, tempos[0])
                    events.append((round(sec, 2), msg.note))
        c_notes = sorted(n for s, n in events if s < 0.1)
        am_notes = sorted(n for s, n in events if 1.9 < s < 2.1)
        check("0초에 C 코드 + 베이스(48,60,64,67)", c_notes == [48, 60, 64, 67],
              f"got {c_notes}")
        check("2초에 Am 코드 + 베이스(57,69,72,76)", am_notes == [57, 69, 72, 76],
              f"got {am_notes}")
        check("총 노트 수 = 8 (코드 3음 + 베이스 1음 x 2)", len(events) == 8,
              f"got {len(events)}")

        # 퀀타이즈: 어중간한 시간(0.05초, 1.98초)이 정수 비트로 스냅되는지
        mid4 = os.path.join(tmp, "quant.mid")
        analyze.save_midi([("C", 0.05, 1.98), ("G", 1.98, 4.03)], mid4, tempo_bpm=120)
        mf = mido.MidiFile(mid4)
        tempos = [msg.tempo for tr in mf.tracks for msg in tr if msg.type == "set_tempo"]
        starts = set()
        for track in mf.tracks:
            t_ticks = 0
            for msg in track:
                t_ticks += msg.time
                if msg.type == "note_on" and msg.velocity > 0:
                    starts.add(round(t_ticks / mf.ticks_per_beat, 3))
        check("코드 시작이 정수 비트로 스냅 (0, 4비트)", starts == {0.0, 4.0}, f"got {starts}")
        timesigs = [msg for tr in mf.tracks for msg in tr if msg.type == "time_signature"]
        check("4/4 박자 기록", timesigs and timesigs[0].numerator == 4
              and timesigs[0].denominator == 4)


# ---------------------------------------------------------------------------
# 7. 에러 처리
# ---------------------------------------------------------------------------

def test_error_handling():
    print("\n[8] 에러 처리")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyze.py")

    r = subprocess.run([sys.executable, script, "--input", "no_such_file.mp3"],
                       capture_output=True, text=True, encoding="utf-8")
    check("없는 파일 → exit 1", r.returncode == 1, f"rc={r.returncode}")

    with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as tf:
        bad = tf.name
    try:
        r = subprocess.run([sys.executable, script, "--input", bad],
                           capture_output=True, text=True, encoding="utf-8")
        check("미지원 포맷 → exit 1", r.returncode == 1, f"rc={r.returncode}")
    finally:
        os.unlink(bad)

    # 손상된(빈) wav 파일
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(b"not a wav file")
        corrupt = tf.name
    try:
        r = subprocess.run([sys.executable, script, "--input", corrupt],
                           capture_output=True, text=True, encoding="utf-8")
        check("손상 파일 → exit 1", r.returncode == 1, f"rc={r.returncode}")
    finally:
        os.unlink(corrupt)


# ---------------------------------------------------------------------------

def main():
    test_templates()
    test_chord_name_parsing()
    test_roman_conversion()
    test_key_estimation()
    test_smoothing()
    test_pattern_detection()
    test_end_to_end()
    test_midi_export()
    test_error_handling()

    print("\n" + "=" * 60)
    print(f"결과: {PASS} PASS / {FAIL} FAIL")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
