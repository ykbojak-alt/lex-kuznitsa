@echo off
REM Голосовые комментарии Fish для демо LEX — наложение на видео.
REM Использование: mix-demo-voiceover.cmd "C:\path\to\demo-video.mp4"
REM Результат: demo-video-voiced.mp4 рядом с исходником.

setlocal
set SRC=%~1
if "%SRC%"=="" (
  echo Укажите файл видео: mix-demo-voiceover.cmd "C:\path\demo-video.mp4"
  exit /b 1
)
set DIR=%~dp0
set OUT=%~dpn1-voiced.mp4

REM Карта наложения: фрагмент, момент старта (сек), громкость.
REM Голос Fish выровнен так, чтобы каждый фрагмент начинался на своём кадре.
REM Музыки в видео нет, поэтому звук исходника глушим полностью (-an).

ffmpeg -y -i "%SRC%" ^
  -i "%DIR%demo-f1.mp3" ^
  -i "%DIR%demo-f2.mp3" ^
  -i "%DIR%demo-f3.mp3" ^
  -i "%DIR%demo-f4.mp3" ^
  -i "%DIR%demo-f5.mp3" ^
  -i "%DIR%demo-f6.mp3" ^
  -i "%DIR%demo-f7.mp3" ^
  -i "%DIR%demo-f8.mp3" ^
  -i "%DIR%demo-f9.mp3" ^
  -filter_complex ^
 "[1:a]adelay=0|0,volume=1.0[a1];^
  [2:a]adelay=27000|27000,volume=1.0[a2];^
  [3:a]adelay=53700|53700,volume=1.0[a3];^
  [4:a]adelay=74500|74500,volume=1.0[a4];^
  [5:a]adelay=94800|94800,volume=1.0[a5];^
  [6:a]adelay=124400|124400,volume=1.0[a6];^
  [7:a]adelay=129900|129900,volume=1.0[a7];^
  [8:a]adelay=146100|146100,volume=1.0[a8];^
  [9:a]adelay=163400|163400,volume=1.0[a9];^
  [a1][a2][a3][a4][a5][a6][a7][a8][a9]amix=inputs=9:duration=longest:dropout_transition=0[mix]" ^
  -map 0:v -map "[mix]" ^
  -c:v copy -c:a aac -b:a 160k ^
  -movflags +faststart ^
  "%OUT%"

echo Готово: %OUT%
endlocal
