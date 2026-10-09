# Third-Party Licenses

싹싹김치 음원 코드 냠냠은 아래 오픈소스 라이브러리를 사용합니다.
각 라이선스 전문은 PyPI/프로젝트 저장소 또는 설치된 `site-packages` 내 `LICENSE` 파일에서 확인할 수 있습니다.

| 패키지 | 라이선스 | 용도 |
|---|---|---|
| [librosa](https://librosa.org) | ISC | 크로마·비트·조성 분석 코어 |
| [numpy](https://numpy.org) | BSD-3-Clause | 수치 연산 |
| [scipy](https://scipy.org) | BSD-3-Clause | 신호 처리 |
| [soundfile](https://python-soundfile.readthedocs.io) | BSD-3-Clause | wav/flac/ogg 디코딩 (libsndfile 번들, LGPL-2.1) |
| [audioread](https://github.com/beetbox/audioread) | MIT | mp3/m4a 디코딩 폴백 |
| [matplotlib](https://matplotlib.org) | PSF-based (BSD 호환) | 코드 타임라인 PNG |
| [midiutil](https://github.com/MarkCWirt/MIDIUtil) | MIT | MIDI보내기 |

## 참고

- **libsndfile**(soundfile 휠에 동봉)은 LGPL-2.1입니다. GUI exe는 PyInstaller onefile로
  번들되며, 소스는 본 리포지토리에서 공개됩니다.
- mp3/m4a 재생은 시스템 ffmpeg/audioread 백엔드를 사용할 수 있습니다.
- 분석 결과는 참고용 근사치입니다. 실제 채보·편곡을 대체하지 않습니다.
