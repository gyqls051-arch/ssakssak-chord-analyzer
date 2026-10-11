#!/usr/bin/env python3
"""음원 코드/조성 분석기 — GUI 버전.

"파일 열기" 버튼으로 음원을 불러오면 분석이 자동으로 시작되고,
결과(조성/BPM/코드 진행)가 창 안에 표시된다.
분석 로직은 analyze.py 를 그대로 사용한다 (GUI는 껍데기).
"""

import contextlib
import io
import os
import queue
import re
import sys
import threading
import traceback
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import analyze
import ssak_update
from version import APP_VERSION

# 리포 루트의 version.json (raw.githubusercontent — 무료, CI가 태그 릴리스 때 갱신)
VERSION_INFO_URL = (
    "https://raw.githubusercontent.com/gyqls051-arch/ssakssak-chord-analyzer/main/version.json"
)

AUDIO_FILETYPES = [
    ("오디오 파일", "*.mp3 *.wav *.flac *.m4a *.ogg"),
    ("모든 파일", "*.*"),
]

DONE = object()  # 작업 완료 신호


class QueueWriter(io.TextIOBase):
    """print 출력을 GUI 큐로 보내는 스트림."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)
        return len(s)


def run_analysis(path: str, q: queue.Queue, result: dict, use_sevenths: bool):
    """워커 스레드: 분석만 하고(파일 저장 없음) 출력은 큐로, 결과는 result로 전달."""
    writer = QueueWriter(q)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            result.update(analyze.analyze(input_path=path, output_path=None,
                                          use_sevenths=use_sevenths))
    except SystemExit:
        pass  # analyze 내부의 sys.exit — 오류 메시지는 이미 출력됨
    except Exception:
        q.put("\n[오류] 예기치 못한 문제가 발생했습니다:\n" + traceback.format_exc())
    finally:
        q.put(DONE)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.running = False
        self.last_input = None
        self.result = {}  # 워커가 채우는 분석 결과 (segments)

        root.title("음원 코드/조성 분석기")
        root.geometry("760x560")
        root.minsize(560, 400)

        # --- 상단: 파일 선택 + 실행 버튼 ---
        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")

        self.open_btn = ttk.Button(top, text="🎵  음원 파일 열기", command=self.pick_file)
        self.open_btn.pack(side="left")

        self.analyze_btn = ttk.Button(top, text="▶  분석 시작", command=self.on_analyze,
                                      state="disabled")
        self.analyze_btn.pack(side="left", padx=(6, 0))

        self.sevenths_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="7th 코드 인식", variable=self.sevenths_var
                        ).pack(side="left", padx=(8, 0))

        self.path_var = tk.StringVar(value="mp3 / wav / flac / m4a / ogg 파일을 선택하세요")
        path_label = ttk.Label(top, textvariable=self.path_var, foreground="#666")
        path_label.pack(side="left", padx=10, fill="x", expand=True)

        # 내보내기 버튼들 (분석 완료 후 활성화 — 저장은 사용자가 원할 때만)
        self.midi_btn = ttk.Button(top, text="🎹 MIDI 저장", command=self.export_midi,
                                   state="disabled")
        self.midi_btn.pack(side="right", padx=(6, 0))

        self.image_btn = ttk.Button(top, text="🖼 이미지 저장", command=self.export_image,
                                    state="disabled")
        self.image_btn.pack(side="right", padx=(6, 0))

        self.doc_btn = ttk.Button(top, text="📄 문서 내보내기", command=self.export_doc,
                                  state="disabled")
        self.doc_btn.pack(side="right")

        # --- 진행바 (6단계 진행 상황을 [n/6] 마커로 파싱해서 표시) ---
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=10)

        # --- 결과 출력 ---
        self.text = ScrolledText(root, wrap="word", font=("Malgun Gothic", 10),
                                 state="disabled", padx=8, pady=8)
        self.text.pack(fill="both", expand=True, padx=10, pady=10)

        # --- 자매제품 띠배너 (OFFCUT STUDIO) ---
        adbar = tk.Frame(root, bg="#28324c", cursor="hand2")
        adbar.pack(fill="x", side="bottom")
        ad_inner = tk.Frame(adbar, bg="#28324c")
        ad_inner.pack(fill="x", padx=10, pady=6)
        tk.Label(ad_inner, text="O", bg="#ff5a2e", fg="#fff",
                 font=("Malgun Gothic", 12, "bold"), width=2
                 ).pack(side="left", padx=(0, 10))
        ad_text = tk.Frame(ad_inner, bg="#28324c")
        ad_text.pack(side="left", fill="x", expand=True)
        tk.Label(ad_text, text="OFFCUT STUDIO  ·  자매 제품",
                 bg="#28324c", fg="#ffffff",
                 font=("Malgun Gothic", 9, "bold"), anchor="w").pack(fill="x")
        tk.Label(ad_text, text="긴 영상, 컷만 쏙쏙 뽑는 프리미어 편집 가속기",
                 bg="#28324c", fg="#cdd3e2",
                 font=("Malgun Gothic", 8), anchor="w").pack(fill="x")
        tk.Label(ad_inner, text="보러가기 →", bg="#ff5a2e", fg="#fff",
                 font=("Malgun Gothic", 8, "bold"),
                 padx=10, pady=3).pack(side="right")
        for w in (adbar, ad_inner, ad_text, *ad_inner.winfo_children(), *ad_text.winfo_children()):
            w.bind("<Button-1>", lambda e: webbrowser.open(
                "https://www.offcut.dev/?utm_source=ssakssak_chord&utm_medium=app_banner"))

        # --- 상태바 ---
        self.status_var = tk.StringVar(value="대기 중")
        ttk.Label(root, textvariable=self.status_var, padding=(10, 4),
                  relief="sunken").pack(fill="x", side="bottom")

        # 백그라운드 업데이트 체크 → 결과는 root.after 로 GUI 스레드에 marshal
        ssak_update.check_for_update(
            APP_VERSION, VERSION_INFO_URL,
            lambda d: self.root.after(0, self._on_update_result, d),
        )

        self.append(
            "음원 코드/조성 분석기\n"
            "─────────────────────────────────────\n"
            "1. [음원 파일 열기] 버튼으로 파일을 선택하고\n"
            "2. [▶ 분석 시작] 버튼을 누르면 분석이 시작됩니다.\n\n"
            "분석이 끝나면 조성(스케일), BPM, 코드 진행이 여기에 표시됩니다.\n"
            "파일은 자동으로 만들어지지 않고, 필요할 때만 버튼으로 저장하세요:\n"
            "  📄 문서 내보내기 — 분석 결과 텍스트(.txt)\n"
            "  🖼 이미지 저장 — 코드 타임라인 그림(.png)\n"
            "  🎹 MIDI 저장 — 코드 진행 미디(.mid)\n\n"
            "※ 결과는 참고용 근사치입니다.\n"
        )

        # 탐색기에서 exe 아이콘 위에 파일을 끌어다 놓고 실행한 경우: 경로만 채워둔다
        if len(sys.argv) == 2 and os.path.isfile(sys.argv[1]):
            self.set_file(sys.argv[1])

        root.after(100, self.poll_queue)

    # --- UI 동작 ---

    def pick_file(self):
        if self.running:
            return
        path = filedialog.askopenfilename(title="분석할 음원 파일 선택",
                                          filetypes=AUDIO_FILETYPES)
        if path:
            self.set_file(path)

    def set_file(self, path: str):
        """파일을 선택만 해두고, 분석은 [분석 시작] 버튼을 눌러야 실행."""
        self.last_input = path
        self.path_var.set(path)
        self.analyze_btn.config(state="normal")
        self.set_export_buttons("disabled")
        self.result.clear()
        self.status_var.set("파일 선택됨 — [▶ 분석 시작] 버튼을 누르세요")

    def set_export_buttons(self, state: str):
        for btn in (self.doc_btn, self.image_btn, self.midi_btn):
            btn.config(state=state)

    def on_analyze(self):
        if self.running or not self.last_input:
            return
        self.start(self.last_input)

    def start(self, path: str):
        self.running = True
        self.result.clear()
        self.open_btn.config(state="disabled")
        self.analyze_btn.config(state="disabled")
        self.set_export_buttons("disabled")
        self.status_var.set("분석 시작... (곡 길이에 따라 수십 초 걸릴 수 있습니다)")
        self.clear()
        self.progress["value"] = 2
        threading.Thread(target=run_analysis,
                         args=(path, self.q, self.result, self.sevenths_var.get()),
                         daemon=True).start()

    STAGE_RE = re.compile(r"\[(\d)/6\]\s*(.+)")

    def poll_queue(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is DONE:
                    self.finish()
                else:
                    self.append(item)
                    # "[n/6] 단계명" 마커를 찾아 진행바/상태바 갱신
                    for m in self.STAGE_RE.finditer(item):
                        step = int(m.group(1))
                        self.progress["value"] = step / 6 * 100
                        self.status_var.set(f"진행 중 ({step}/6) — {m.group(2).strip()}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def finish(self):
        self.running = False
        self.progress["value"] = 100
        self.open_btn.config(state="normal")
        self.analyze_btn.config(state="normal")
        if self.result.get("segments"):
            self.set_export_buttons("normal")
            self.status_var.set("완료 — 필요하면 내보내기 버튼으로 문서/이미지/MIDI를 저장하세요")
        else:
            self.status_var.set("분석 실패 — 위 메시지를 확인하세요")

    # --- 내보내기 (사용자가 버튼을 눌렀을 때만 파일 생성) ---

    def _ask_save_path(self, title, ext, filetypes):
        """저장 위치를 물어본다. 기본값: 음원 폴더 / 음원이름_chords.<ext>"""
        base = os.path.splitext(os.path.basename(self.last_input))[0]
        return filedialog.asksaveasfilename(
            title=title,
            initialdir=os.path.dirname(os.path.abspath(self.last_input)),
            initialfile=f"{base}_chords{ext}",
            defaultextension=ext,
            filetypes=filetypes,
        )

    def export_doc(self):
        lines = self.result.get("report_lines")
        if not lines:
            return
        path = self._ask_save_path("분석 결과 문서 저장", ".txt", [("텍스트 파일", "*.txt")])
        if not path:
            return
        if analyze.write_report(lines, path):
            self.status_var.set(f"문서 저장 완료: {os.path.basename(path)}")
            messagebox.showinfo("문서 내보내기", f"저장되었습니다:\n{path}")
        else:
            messagebox.showerror("오류", "문서 저장에 실패했습니다.")

    def export_image(self):
        segments = self.result.get("segments")
        if not segments:
            return
        path = self._ask_save_path("코드 타임라인 이미지 저장", ".png", [("PNG 이미지", "*.png")])
        if not path:
            return
        ok = analyze.save_visualization(segments, self.result["duration"],
                                        self.last_input, path)
        if ok:
            self.status_var.set(f"이미지 저장 완료: {os.path.basename(path)}")
            messagebox.showinfo("이미지 저장", f"저장되었습니다:\n{path}")
        else:
            messagebox.showerror("오류", "이미지 저장에 실패했습니다.")

    def export_midi(self):
        segments = self.result.get("segments")
        if not segments:
            return
        path = self._ask_save_path("MIDI 파일 저장", ".mid", [("MIDI 파일", "*.mid")])
        if not path:
            return
        try:
            ok = analyze.save_midi(segments, path, tempo_bpm=self.result.get("tempo", 0))
        except Exception as e:
            messagebox.showerror("오류", f"MIDI 저장 실패: {e}")
            return
        if ok:
            self.status_var.set(f"MIDI 저장 완료: {os.path.basename(path)}")
            messagebox.showinfo("MIDI 저장", f"저장되었습니다:\n{path}")
        else:
            messagebox.showwarning("MIDI 저장", "내보낼 코드가 없습니다 (전부 N.C.).")

    # --- 텍스트 위젯 헬퍼 ---

    def append(self, s: str):
        self.text.config(state="normal")
        self.text.insert("end", s)
        self.text.see("end")
        self.text.config(state="disabled")

    def clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")


    def _on_update_result(self, d):
        kind = d.get("kind")
        if kind == "uptodate" or kind == "error":
            return
        url = d.get("download_url") or "https://www.ssakkimchi.com"
        notes = d.get("release_notes") or ""
        msg = d.get("message", "") + (f"\n\n{notes}" if notes else "")
        msg += "\n\n다운로드 페이지를 열까요?"
        title = "업데이트 필요" if kind in ("force_update", "killswitch") else "업데이트 안내"
        if messagebox.askyesno(title, msg):
            import webbrowser
            webbrowser.open(url)


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # 고해상도 화면에서 흐릿함 방지
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
