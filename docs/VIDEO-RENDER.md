# VIDEO-RENDER.md — как рендерить демо-видео LEX

**Статус:** pre-sprint материал (2026-11-25). Код продукта не пишется до старта спринта.

**Что это.** Пошаговый рецепт сборки демо-видео из трёх источников — снятого прогона,
озвучки и субтитров — с конкретными командами под тулкит этой машины. Дополняет
`docs/DEMO-VIDEO-PROMPT.md` (там — что за оформление и почему), здесь — **как это
запустить руками**.

**Источник данных:**

| Что берём | Откуда | Примечание |
|---|---|---|
| Кадры (тело прогона) | `docs/DEMO-SCRIPT.md` — сцены 0–7 | один непрерывный дубль, без склеек |
| Оформление (титры, счётчик) | `docs/DEMO-VIDEO-PROMPT.md` §3 | Remotion-проект |
| Тайминг сцен | `docs/DEMO-TIMING.md` | если файла нет на момент рендера — блокер, см. §7 |
| Текст озвучки | `docs/DEMO-SCRIPT.md` — реплики | читаются как скрипт голоса |
| Голос | Fish Audio TTS (тулкит) | синтез; см. §3.1 |
| Субтитры | whisper-cli по готовой озвучке | см. §4 |

---

## 0. Проверка окружения (до всего остального)

```powershell
# загрузить тулкит (кэши и PATH на D:)
. 'D:\tools\video-toolkit\scripts\video-env.ps1'

# что должно быть на месте
D:\tools\ffmpeg\ffmpeg.exe  -version          # ожидаем 9.0.2+, есть libx264 и aac
D:\Program\nodejs\node.exe  -v                 # node 20+
D:\tools\video-toolkit\node_modules\.bin\remotion.cmd --version   # 4.0.545
Test-Path D:\tools\whisper-cpp\build\bin\Release\whisper-cli.exe   # True
```

Если `remotion.cmd --version` падает или `remotion browser ensure` не может скачать
Chromium — **не блокируемся**: план Б в §6 (HyperFrames), план В — ffmpeg drawtext.

Фактические версии на этой машине (проверено 2026-11-25):

- ffmpeg 9.0.2-essentials_build (libx264, aac — есть)
- Remotion `4.0.545`, React `19.3.1`
- HyperFrames `0.9.80`
- whisper-cpp: `build\bin\Release\whisper-cli.exe`

---

## 1. Сбор и вход-выход каждого шага

```
raw-скринкаст (OBS)  ──►  lex-raw.mp4            тело прогона, 1080p30
озвучка (Fish TTS)   ──►  vo.mp3                 голос за кадром
субтитры (whisper)   ──►  subs.srt               из vo.mp3 или из итогового микса
оформление (Remotion)──►  lex-overlay.mp4        титры+счётчик поверх raw
микс (ffmpeg)        ──►  lex-mix.mp4            overlay + vo  (тело не режем)
финал (ffmpeg)       ──►  lex-demo-<sha8>.mp4    loudnorm + faststart + AAC
```

**Правило:** тело `lex-raw.mp4` правится **только** обрезкой пустого хвоста/головы
(§2.4) — не ускоряется, не переставляется, не склеивается.

---

## 2. Тело: скринкаст

### 2.1. Запись (OBS или ffmpeg gdigrab)

**OBS (предпочтительно):** источник «Захват экрана», 1920×1080, 30 fps, CQP 18,
формат mp4 (или mkv → remux в mp4 после, чтобы не терять запись при сбое).

**ffmpeg gdigrab** (если OBS недоступен; на Windows-native):

```powershell
D:\tools\ffmpeg\ffmpeg.exe -f gdigrab -framerate 30 -i desktop `
  -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p `
  -video_size 1920x1080  D:\work\lex-raw.mp4
```

> Из прошлого опыта фермы: прямой gdigrab из SSH-сессии (session 0) ничего не
> захватывает — запись запускать в пользовательской сессии (из терминала рабочего
> стола), не из служебного SSH.

### 2.2. Проверка, что тело целое

```powershell
D:\tools\ffmpeg\ffprobe.exe -v error -select_streams v:0 `
  -show_entries stream=width,height,r_frame_rate,codec_name,pix_fmt `
  -show_entries format=duration -of default=nw=1 D:\work\lex-raw.mp4
```

Ожидаем: `width=1920 height=1080 r_frame_rate=30/1 codec_name=h264 pix_fmt=yuv420p`.

### 2.3. Remux mkv → mp4 (если писали в mkv)

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-raw.mkv -c copy D:\work\lex-raw.mp4
```

### 2.4. Обрезка пустого хвоста/головы (единственная допустимая правка тела)

Если в начале/конце есть «мёртвые» секунды — обрезать **только** их, по секундам:

```powershell
# срезать первые 2.5 с и последние 1.0 с (пример)
D:\tools\ffmpeg\ffmpeg.exe -ss 2.5 -to 00:03:12.0 -i D:\work\lex-raw.mp4 `
  -c copy D:\work\lex-body.mp4
```

`-c copy` — без перекодирования, без потери качества, без шва. Содержание сцен 0–7
при обрезке не страдает.

---

## 3. Звук

### 3.1. Озвучка (Fish Audio TTS)

Реплики из `docs/DEMO-SCRIPT.md` → в текстовый файл (по сцене), затем синтез. Ключ
Fish уже настроен в тулките (DPAPI текущего пользователя); **ключ не читаем и не
пересылаем**.

```powershell
. 'D:\tools\video-toolkit\scripts\video-env.ps1'

# текст реплики сцены -> vo-sceneN.mp3
& 'D:\tools\video-toolkit\scripts\fish-tts.ps1' `
  -TextFile D:\work\vo\scene3.txt `
  -OutputPath D:\work\vo\scene3.mp3
```

Для каждой из 7 сцен — свой файл; затем склейка с паузами под тайминг из
`docs/DEMO-TIMING.md`.

> Требование по громкости: голос −16 LUFS, true-peak ≤ −1 dBTP — выравниваем в §5.3.

**Про «звук (NexusLink)»:** NexusLink — шлюз LLM-инференса (чат-модели); синтеза речи
у него нет. Голос для видео делает Fish Audio TTS из тулкита. Если под «звуком через
NexusLink» имелся в виду другой сервис — уточнить у оркестратора; в этой инструкции
рабочий путь — Fish.

### 3.2. Склейка озвучки под тайминг

```powershell
# список файлов в порядке сцен + паузы через anullsrc (пример каркаса):
# scene0 (15s) | 1s | scene1 (25s) | 0.5s | scene2 (30s) | ...
D:\tools\ffmpeg\ffmpeg.exe -f concat -safe 0 -i D:\work\vo\concat.txt `
  -c:a libmp3lame -b:a 192k D:\work\vo.mp3
```

`concat.txt`:
```
file 'D:/work/vo/scene0.mp3'
file 'D:/work/vo/pause-1s.mp3'
file 'D:/work/vo/scene1.mp3'
...
```

Паузы генерируются один раз:
```powershell
D:\tools\ffmpeg\ffmpeg.exe -f lavfi -i anullsrc=r=48000:cl=stereo -t 1.0 `
  -c:a libmp3lame D:\work\vo\pause-1s.mp3
```

### 3.3. Выравнивание длины «видео ↔ голос»

Если озвучка длиннее тела — **не ускорять видео**; подрезать текст реплики и
пересинтезировать (тайминг шагов важнее). Если короче — растянуть паузы, не видео.

---

## 4. Субтитры

### 4.1. Авто-субтитры из озвучки (whisper)

```powershell
$W = 'D:\tools\whisper-cpp\build\bin\Release\whisper-cli.exe'
$M = 'D:\tools\whisper-cpp\models\ggml-small.bin'

& $W -m $M -f D:\work\vo.mp3 -osrt -of D:\work\subs -l ru
# -> D:\work\subs.srt
```

Проверить: русский язык распознан (`-l ru`), нет пустых строк на паузах.

### 4.2. Правка субтитров

Открыть `subs.srt`, поправить термины: `LEX`, `profile: local`, `[3/7]`,
`SR-01`, `rerank` — whisper их исказит. Тайминги не ломать.

### 4.3. Вшить субтитры в видео (опционально)

Жюри смотрит в плеере — встроенные субтитры надёжнее внешнего файла. Soft-sub
(можно выключить) — отдельной дорожкой `mov_text`:

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-mix.mp4 -i D:\work\subs.srt `
  -c:v copy -c:a copy -c:s mov_text -metadata:s:s:0 language=rus `
  D:\work\lex-subs.mp4
```

Жёсткие субтитры (вшиты в кадр) — только если плеер жюри может не показать soft-subs;
тогда см. §5.4 (`subtitles` фильтр).

---

## 5. Оформление и финальный микс (Remotion + ffmpeg)

### 5.1. Рендер оформления (Remotion)

Проект оформления — по структуре из `docs/DEMO-VIDEO-PROMPT.md` §3.1. Тело читается
как `public/lex-body.mp4`, тайминги — из `timing/scenes-full.ts` (сверены с
`docs/DEMO-TIMING.md`).

```powershell
cd D:\work\remotion-demo
D:\tools\video-toolkit\node_modules\.bin\remotion.cmd render DemoFull `
  D:\work\lex-overlay.mp4 --codec=h264 --crf=18 --pixel-format=yuv420p
```

> Remotion перекодирует видео (накладывает титры). Поэтому `lex-overlay.mp4` уже
> H.264/yuv420p; финальный ffmpeg-шаг его только нормализует, не пережимает заново.

### 5.2. Микс: оформленное видео + озвучка

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-overlay.mp4 -i D:\work\vo.mp3 `
  -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k -shortest `
  D:\work\lex-mix.mp4
```

`-shortest` — по короткой дорожке; если голос короче видео — озвучку дополнить тишиной
(см. §3.3), иначе обрежется хвост.

### 5.3. Нормализация громкости + faststart (финал)

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-mix.mp4 `
  -c:v copy -c:a aac -b:a 192k `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart `
  D:\work\lex-demo-<sha8>.mp4
```

Проверка громкости:
```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-demo-<sha8>.mp4 `
  -af loudnorm=print_format=summary -f null - 2>&1 |
  Select-String "Output Integrated|Output True Peak"
```

### 5.4. Если нужны жёсткие субтитры

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-mix.mp4 `
  -vf "subtitles=D:/work/subs.srt:force_style='FontName=DejaVu Sans,FontSize=24,Outline=1,Shadow=0'" `
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -c:a copy `
  -movflags +faststart D:\work\lex-demo-subs.mp4
```

(На Windows путь в фильтре — через прямые слэши и без кавычек внутри.)

---

## 6. План Б и план В (если Remotion не работает)

**План Б — HyperFrames** (та же идея «видео = код», уже стоит в тулките):

```powershell
. 'D:\tools\video-toolkit\scripts\video-env.ps1'
D:\tools\video-toolkit\node_modules\.bin\hyperframes.cmd --help
```

Композиция повторяет `layers/` из `docs/DEMO-VIDEO-PROMPT.md`; HyperFrames рендерит
HTML→видео через собственный ffmpeg.

**План В — ffmpeg drawtext** (минимальные титры без браузерного рендера):

```powershell
D:\tools\ffmpeg\ffmpeg.exe -i D:\work\lex-body.mp4 `
  -vf "drawtext=text='Действующая редакция':x=(w-tw)/2:y=h-th-60:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.55:enable='between(t,90,120)'" `
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -c:a copy D:\work\lex-draw.mp4
```

План В — грубее, но сдаётся: честный прогон с подписями лучше, чем отсутствие видео.

---

## 7. Зависимость от `docs/DEMO-TIMING.md`

Тайминги сцен (начало/длительность каждой из 7 сцен) и моменты шагов `[N/7]` для
счётчика берутся из `docs/DEMO-TIMING.md`. **На 2026-11-25 этого файла в репозитории
нет** (его готовит параллельный блок).

Если к моменту рендера `DEMO-TIMING.md` отсутствует:

1. Взять ориентировочные тайминги из `docs/DEMO-SCRIPT.md` §2 (там таблица сцен с
   началом и длительностью) — это допустимая замена на репетиции.
2. В `timing/scenes-*.ts` и в `concat.txt` внести фактические тайминги прогона.
3. **Не сдавать** финал, пока тайминги не сверены с реальной записью — счётчик `[N/7]`
   не должен расходиться с UI.

Источник правды для счётчика — **реальная запись**, а не ни один из документов: если
документ и запись расходятся, правится документ.

---

## 8. Полный прогон одной командой (каркас)

Рабочий каталог — `D:\work` (все промежуточные файлы на D:, см. контракт фермы):

```powershell
$W = 'D:\work'; $F = 'D:\tools\ffmpeg\ffmpeg.exe'
$sha8 = (git -C D:\projects\iikuznitsa-sprint rev-parse --short=8 HEAD)

# 1 тело уже снято -> $W\lex-body.mp4
# 2 озвучка склеена -> $W\vo.mp3
# 3 субтитры (опц.) -> $W\subs.srt

# 4 оформление
Push-Location D:\work\remotion-demo
D:\tools\video-toolkit\node_modules\.bin\remotion.cmd render DemoFull $W\lex-overlay.mp4 --codec=h264 --crf=18 --pixel-format=yuv420p
Pop-Location

# 5 микс + нормализация + faststart
& $F -i $W\lex-overlay.mp4 -i $W\vo.mp3 -map 0:v:0 -map 1:a:0 `
  -c:v copy -c:a aac -b:a 192k -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart -shortest "$W\lex-demo-$sha8.mp4"

# 6 контроль
& $F -i "$W\lex-demo-$sha8.mp4" -f null - 2>&1 | Select-String "Duration|Stream #"
```

Сжатая версия 2:50 — та же цепочка, `render DemoShort` и тайминги `scenes-short.ts`.

---

## 9. Чек-лист рендера

- [ ] Окружение загружено (`video-env.ps1`), ffmpeg/Remotion/whisper на месте.
- [ ] `lex-body.mp4` — 1080p30, h264, один дубль (обрезка только головы/хвоста).
- [ ] Озвучка склеена по таймингу, длина ≈ длине тела.
- [ ] Субтитры распознаны по-русски, термины поправлены.
- [ ] Оформление отрендерено (`DemoFull` / `DemoShort`).
- [ ] Микс: видео-дорожка `copy` (без повторного перекодирования).
- [ ] Финал: `loudnorm −16 LUFS`, true-peak ≤ −1 dBTP, `+faststart`, AAC.
- [ ] Длительность ≤ 3:00 (сжатая ≤ 2:50), имя содержит SHA.
- [ ] Прогон через `docs/VIDEO-CHECKLIST.md`.

---

## Связанные документы

- `docs/DEMO-SCRIPT.md` — сцены, реплики, тайминг-ориентир, стоп-условия.
- `docs/DEMO-VIDEO-PROMPT.md` — оформление (Remotion-структура, Dola вставки, формат).
- `docs/VIDEO-EXPORT.md` — экспортные параметры и финальные ffmpeg-команды.
- `docs/VIDEO-CHECKLIST.md` — приёмочный гейт.
- `docs/DEMO-TIMING.md` — тайминги сцен (создаётся параллельным блоком).
- `docs/PITCH-VIDEO.md` — видео-питч 90 с.
