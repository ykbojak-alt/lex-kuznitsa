# VIDEO-EXPORT.md — экспорт финального видеофайла LEX

**Статус:** pre-sprint материал (2026-11-25). Код продукта не пишется до старта спринта.

**Что это.** Экспортный контракт: параметры выходного файла и команды ffmpeg, которые их
дают. Продолжение `docs/VIDEO-RENDER.md` (там — сборка из частей), здесь — **приведение
к финальному формату сдачи** и проверка.

**Кому:** человеку или агенту, который берёт готовый микс и делает один сдаточный файл.

---

## 1. Целевой формат (контракт сдачи)

| Параметр | Значение | Почему именно так |
|---|---|---|
| Контейнер | MP4 (`+faststart`) | `moov` в начале файла — играет до полной загрузки |
| Видео-кодек | **H.264 High** (`libx264`, `-profile:v high`) | совместим со всеми плеерами и формами загрузки |
| Разрешение | **1920×1080** | читаемость текста терминала/UI; 16:9 |
| Частота кадров | **30 fps** (`-r 30`) | единый таймлайн, без телесин-дёрганья |
| Пиксельный формат | **yuv420p** | обязателен для браузеров/мобильных |
| Битрейт видео | VBR, `-crf 18`, `-maxrate 12M -bufsize 24M` | текст без артефактов, файл ≤ 100 МБ |
| Аудио-кодек | **AAC**, `-b:a 192k`, 48 кГц, стерео | совместимость, разборчивость голоса |
| Громкость | **−16 LUFS**, true-peak ≤ −1 dBTP | единый уровень, без клиппинга |
| Длительность | демо ≤ **3:00** (цель 2:50); питч ≤ 1:30 | лимит Положения |
| Размер | демо ≤ **100 МБ**; питч ≤ 60 МБ | удобство загрузки |
| Имя | `lex-demo-<sha8>-<YYYYMMDD>.mp4` / `lex-pitch-<sha8>-<YYYYMMDD>.mp4` | SHA привязан к прогону |

**Строка ffprobe-эталона** (что должно получиться):
```
Stream #0:0  Video: h264 (High), yuv420p, 1920x1080, 30 fps
Stream #0:1  Audio: aac (LC), 48000 Hz, stereo, 192 kb/s
format: mov,mp4,m4a  · duration ≤ 00:03:00
```

---

## 2. Экспорт из готового микса (основной путь)

Вход — `lex-mix.mp4` из `docs/VIDEO-RENDER.md` §5.2 (видео уже с титрами, звук — голос).
Один проход: видео **без перекодирования** (`copy`), звук — нормализация и AAC,
плюс `faststart`.

```powershell
$F  = 'D:\tools\ffmpeg\ffmpeg.exe'
$in = 'D:\work\lex-mix.mp4'
$sha8 = (git -C D:\projects\iikuznitsa-sprint rev-parse --short=8 HEAD)
$out = "D:\work\lex-demo-$sha8-$(Get-Date -Format yyyyMMdd).mp4"

& $F -y -i $in `
  -map 0:v:0 -map 0:a:0 `
  -c:v copy `
  -c:a aac -b:a 192k -ar 48000 -ac 2 `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart `
  $out
```

> Если `lex-mix.mp4` уже имеет `+faststart` и нормализованный звук (как в
> `VIDEO-RENDER.md` §5.3), этот шаг можно свести к `-c copy` + переименование.

---

## 3. Экспорт «с нуля» (если нужен полный контроль)

Когда микс не готов как отдельный файл — единый проход по телу + озвучке с титрами
через `drawtext`/`subtitles`. Здесь видео **перекодируется** (поэтому `-crf 18` и
`-profile:v high`):

```powershell
$F  = 'D:\tools\ffmpeg\ffmpeg.exe'
$out = "D:\work\lex-demo-$sha8.mp4"

& $F -y -i D:\work\lex-body.mp4 -i D:\work\vo.mp3 `
  -map 0:v:0 -map 1:a:0 `
  -c:v libx264 -preset slow -crf 18 -profile:v high -level 4.2 `
  -pix_fmt yuv420p -r 30 `
  -maxrate 12M -bufsize 24M `
  -c:a aac -b:a 192k -ar 48000 -ac 2 `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart -shortest `
  $out
```

С жёсткими субтитрами (путь к `.srt` — прямыми слэшами):

```powershell
& $F -y -i D:\work\lex-body.mp4 -i D:\work\vo.mp3 `
  -map 0:v:0 -map 1:a:0 `
  -vf "subtitles=D:/work/subs.srt:force_style='FontName=DejaVu Sans,FontSize=24,Outline=1,Shadow=0'" `
  -c:v libx264 -preset slow -crf 18 -profile:v high -level 4.2 -pix_fmt yuv420p -r 30 `
  -maxrate 12M -bufsize 24M `
  -c:a aac -b:a 192k -ar 48000 -ac 2 `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart -shortest `
  D:\work\lex-demo-subs.mp4
```

С soft-субтитрами (отдельной дорожкой, отключаемые):

```powershell
& $F -y -i D:\work\lex-mix.mp4 -i D:\work\subs.srt `
  -map 0:v:0 -map 0:a:0 -map 1:0 `
  -c:v copy -c:a aac -b:a 192k `
  -c:s mov_text -metadata:s:s:0 language=rus `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart `
  "D:\work\lex-demo-subs.mp4"
```

---

## 4. Двухпроходный CBR (только если форма требует стабильный битрейт)

VBR `-crf 18` обычно достаточно. Если площадка загрузки требует CBR — два прохода:

```powershell
# проход 1 — анализ
& $F -y -i D:\work\lex-mix.mp4 -c:v libx264 -b:v 10M -pass 1 -passlogfile D:\work\x264 `
  -pix_fmt yuv420p -profile:v high -r 30 -an -f mp4 NUL

# проход 2 — кодирование
& $F -y -i D:\work\lex-mix.mp4 -c:v libx264 -b:v 10M -pass 2 -passlogfile D:\work\x264 `
  -pix_fmt yuv420p -profile:v high -level 4.2 -r 30 `
  -c:a aac -b:a 192k -ar 48000 -ac 2 -af "loudnorm=I=-16:TP=-1.0:LRA=11" `
  -movflags +faststart D:\work\lex-demo-cbr.mp4
```

(В PowerShell `NUL` — устройство-«чёрная дыра»; в bash — `/dev/null`.)

---

## 5. Проверка экспорта

### 5.1. Один запрос — все ключевые параметры

```powershell
$P = 'D:\tools\ffmpeg\ffprobe.exe'
& $P -v error -select_streams v:0 `
  -show_entries stream=codec_name,profile,width,height,pix_fmt,r_frame_rate,bit_rate `
  -show_entries format=duration,size,format_name `
  -of default=nw=1 "D:\work\lex-demo-<sha8>.mp4"
```

Ожидаем:
```
codec_name=h264
profile=High
width=1920
height=1080
pix_fmt=yuv420p
r_frame_rate=30/1
duration=... (≤ 180)
format_name=mov,mp4,m4a,3gp,3g2,mj2
```

### 5.2. Проверка `+faststart` (moov до mdat)

```powershell
& $P -v trace "D:\work\lex-demo-<sha8>.mp4" 2>&1 |
  Select-String -Pattern "type:'(moov|mdat)'" | Select-Object -First 2
```

`moov` должен идти **раньше** `mdat`. Если наоборот — перепаковать:
```powershell
& 'D:\tools\ffmpeg\ffmpeg.exe' -i in.mp4 -c copy -movflags +faststart out.mp4
```

### 5.3. Громкость и true-peak

```powershell
& 'D:\tools\ffmpeg\ffmpeg.exe' -i "D:\work\lex-demo-<sha8>.mp4" `
  -af loudnorm=print_format=summary -f null - 2>&1 |
  Select-String "Input Integrated|Output Integrated|Output True Peak"
```

Целимся: `Output Integrated` ≈ −16 LUFS, `Output True Peak` ≤ −1 dBTP.

### 5.4. Размер и SHA-256

```powershell
Get-Item "D:\work\lex-demo-<sha8>.mp4" | Select-Object Name, Length
Get-FileHash "D:\work\lex-demo-<sha8>.mp4" -Algorithm SHA256 | Select-Object Hash
```

> Из памяти фермы: на этой машине `Get-FileHash` иногда сбоит — fallback
> `sha256sum` (Git Bash) или `certutil -hashfile <file> SHA256`.

### 5.5. Уложиться в лимит размера

Если файл > 100 МБ при `-crf 18`, **не** резать видео — поднять CRF до 20–21
(для текста на экране это ещё безопасно) и перепроверить читаемость на 720p.

```powershell
& $F -i in.mp4 -c:v libx264 -crf 21 -preset slow -profile:v high -pix_fmt yuv420p `
  -c:a copy -movflags +faststart out.mp4
```

---

## 6. Экспорт питч-видео (90 с)

Тот же контракт, лимиты строже: ≤ 1:30 и ≤ 60 МБ. Команда — как в §2, с другим входом:

```powershell
$out = "D:\work\lex-pitch-$sha8-$(Get-Date -Format yyyyMMdd).mp4"
& $F -y -i D:\work\pitch-mix.mp4 -map 0:v:0 -map 0:a:0 `
  -c:v copy -c:a aac -b:a 192k -ar 48000 -ac 2 `
  -af "loudnorm=I=-16:TP=-1.0:LRA=11" -movflags +faststart $out
```

---

## 7. Именование и выкладка

```
D:\work\out\
  lex-demo-<sha8>-<YYYYMMDD>.mp4     ← сдаточный демо
  lex-demo-full-<sha8>.mp4           ← полная 3:20 (репетиция, не сдаётся)
  lex-pitch-<sha8>-<YYYYMMDD>.mp4    ← сдаточный питч
  MANIFEST-video.txt                 ← ниже
```

`MANIFEST-video.txt`:
```
file    lex-demo-<sha8>-<YYYYMMDD>.mp4
sha256  <hash>
sha8    <git sha прогона>
date    <YYYY-MM-DD>
duration <mm:ss>
size    <MB>
spec    h264 High / yuv420p / 1920x1080 / 30fps / AAC 48kHz / -16 LUFS
```

---

## 8. Частые ошибки экспорта

| Симптом | Причина | Как исправить |
|---|---|---|
| Файл не играет в браузере | не `yuv420p` или нет `+faststart` | `-pix_fmt yuv420p -movflags +faststart` |
| Видео дёргается | разные fps у частей (24 vs 30) | нормализовать всё в `-r 30` на перекодирующем проходе |
| Звук рассинхрон в конце | `-shortest` при разной длине | выровнять длину в `VIDEO-RENDER.md` §3.3 |
| Скачок громкости на стыке | нормализовали части по отдельности | один `loudnorm` на финальном миксе, не по частям |
| Текст «мылит» | слишком низкий битрейт/высокий CRF | `-crf 18`, `-preset slow`; не поднимать CRF выше 21 |
| > 100 МБ | высокий битрейт на длинном видео | CRF 20–21 или двухпроходный CBR 8–10 Мбит/с |
| `moov` после `mdat` | забыли `+faststart` | перепаковать `-c copy -movflags +faststart` |

---

## 9. Чек-лист экспорта

- [ ] H.264 High, 1920×1080, 30 fps, yuv420p — проверено ffprobe.
- [ ] AAC 192k, 48 кГц, стерео.
- [ ] −16 LUFS, true-peak ≤ −1 dBTP.
- [ ] `+faststart` (moov до mdat) — проверено.
- [ ] Длительность ≤ лимита (демо 3:00 / питч 1:30).
- [ ] Размер ≤ лимита (демо 100 МБ / питч 60 МБ).
- [ ] Имя по шаблону с SHA.
- [ ] SHA-256 посчитан, записан в `MANIFEST-video.txt`.
- [ ] Прогон через `docs/VIDEO-CHECKLIST.md`.

---

## Связанные документы

- `docs/VIDEO-RENDER.md` — сборка (тело, звук, субтитры, оформление, микс).
- `docs/DEMO-VIDEO-PROMPT.md` — оформление и формат титров.
- `docs/VIDEO-CHECKLIST.md` — приёмочный гейт перед сдачей.
- `docs/PITCH-VIDEO.md` — видео-питч 90 с.
