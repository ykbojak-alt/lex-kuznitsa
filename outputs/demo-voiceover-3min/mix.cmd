@echo off
REM Наложение голоса Fish на демо LEX. Итог — ровно 3:00.
REM Использование: mix.cmd "C:\path\to\demo-video.mp4"
REM Результат: <имя>-voiced.mp4 рядом с исходником.

setlocal
set SRC=%~1
if "%SRC%"=="" (
  echo Укажите файл видео: mix.cmd "C:\path\demo-video.mp4"
  exit /b 1
)
set DIR=%~dp0
set OUT=%~dpn1-voiced.mp4

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
 "[1:a]adelay=0|0[a1];^
  [2:a]adelay=26250|26250[a2];^
  [3:a]adelay=52400|52400[a3];^
  [4:a]adelay=72720|72720[a4];^
  [5:a]adelay=92470|92470[a5];^
  [6:a]adelay=121310|121310[a6];^
  [7:a]adelay=126560|126560[a7];^
  [8:a]adelay=142320|142320[a8];^
  [9:a]adelay=159190|159190[a9];^
  [a1][a2][a3][a4][a5][a6][a7][a8][a9]amix=inputs=9:duration=longest:dropout_transition=0,^
  apad=whole_dur=180,atrim=0:180[mix]" ^
  -map 0:v -map "[mix]" ^
  -c:v copy -c:a aac -b:a 160k ^
  -movflags +faststart ^
  "%OUT%"

echo Готово: %OUT%
endlocal
