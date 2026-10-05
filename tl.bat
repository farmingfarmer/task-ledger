@echo off
rem tl.bat -- the Windows half of ./tl. Same subcommands, same guards:
rem doctor runs before any DB-touching command so a broken environment
rem costs a third of a second instead of a full run, and the suites are
rem forced onto a scratch database. See the header of ./tl for why.
rem
rem   tl                  state and what is next
rem   tl reconcile        signals -> proposals
rem   tl review           the human decision loop
rem   tl --synth review   the same, against the synthetic ledger
setlocal
cd /d "%~dp0"

set "PROFILE=real"
if /i "%~1"=="--synth" ( set "PROFILE=synth" & shift )
if /i "%~1"=="--synthetic" ( set "PROFILE=synth" & shift )
if /i "%~1"=="--real" ( set "PROFILE=real" & shift )

rem Python's default text encoding on Windows is cp1252, so every bare
rem open() in the codebase -- written on a Mac, where the default is
rem UTF-8 -- misreads the first accented or em-dashed file it touches.
rem UTF-8 mode fixes the default itself, instead of chasing open() calls
rem one traceback at a time.
set "PYTHONUTF8=1"

rem Portable Postgres in the Windows port listens on 5433, not 5432.
if not defined TL_PORT set "TL_PORT=5433"
if "%PROFILE%"=="synth" (
  if defined TL_SYNTH_DSN ( set "DB_DSN=%TL_SYNTH_DSN%" ) else (
    set "DB_DSN=host=localhost port=%TL_PORT% dbname=taskintel_synth user=%USERNAME%" )
  set "TL_INBOX=%USERPROFILE%\task-ledger-synthetic-inbox"
) else (
  if not defined DB_DSN set "DB_DSN=host=localhost port=%TL_PORT% dbname=taskintel user=%USERNAME%"
  if not defined TL_INBOX set "TL_INBOX=%USERPROFILE%\task-ledger-inbox"
)
if not defined TL_SYNTH_DSN set "TL_SYNTH_DSN=host=localhost port=%TL_PORT% dbname=taskintel_synth user=%USERNAME%"

rem Where the Postgres command-line tools live. Several subcommands shell
rem out to psql/createdb/dropdb, and a fresh terminal has no PGBIN -- so
rem "could not find createdb/dropdb/psql" was the first thing a new window
rem said, about a program sitting in a known place on this very machine.
rem The portable install is checked first because that is what the Windows
rem port ships; anything on PATH still works when this finds nothing.
if not defined PGBIN (
  if exist "%USERPROFILE%\pgsql-portable\pgsql\bin\psql.exe" (
    set "PGBIN=%USERPROFILE%\pgsql-portable\pgsql\bin"
  )
)

if not defined TL_USER set "TL_USER=aad-dev"
if not defined TL_EMBED_MODEL set "TL_EMBED_MODEL=nomic-embed-text"
rem The judge model is NOT defaulted to a name. It used to default to
rem mistral-small3.1, which exists on the machine this was written on and
rem not on the machine it runs on -- so every call that needed a judge
rem came back HTTP 404 from /api/chat, several frames deep, on a ledger
rem that was already half-built. A default that is wrong on the target
rem machine is worse than no default: unset, the reconciler simply runs
rem without a judge, holds what it cannot decide, and the ask band puts
rem the strong matches in front of a person anyway.
rem   set TL_JUDGE_MODEL=llama3.1:8b     to turn the judge on

set "CMD=%~1"
if "%CMD%"=="" set "CMD=up"
if not "%~1"=="" shift

rem SHIFT does not affect %*, which always expands to the ORIGINAL argument
rem list -- so passing %* through after shifting re-sends the subcommand name
rem and every positional a second time. Gather what is left by hand instead:
rem ARG1 is the first positional (a file, usually) and REST is everything
rem after it, with ARGS being both together for subcommands taking neither.
set "ARG1=%~1"
set "REST="
if not "%~1"=="" shift
:gather
if "%~1"=="" goto :gathered
set "REST=%REST% "%~1""
shift
goto :gather
:gathered
set "ARGS="
if defined ARG1 set "ARGS="%ARG1%"%REST%"

if /i "%CMD%"=="help" goto :usage
if /i "%CMD%"=="up" goto :up
if /i "%CMD%"=="doctor" goto :up
if /i "%CMD%"=="env" goto :env
if /i "%CMD%"=="ollama" goto :ollama
if /i "%CMD%"=="tick" goto :tick
if /i "%CMD%"=="reconcile" goto :reconcile
if /i "%CMD%"=="review" goto :review
if /i "%CMD%"=="digest" goto :digest
if /i "%CMD%"=="ask" goto :ask
if /i "%CMD%"=="share" goto :share
if /i "%CMD%"=="dashboard" goto :dashboard
if /i "%CMD%"=="extract" goto :extract
if /i "%CMD%"=="ingest" goto :ingest
if /i "%CMD%"=="transcripts" goto :transcripts
if /i "%CMD%"=="quickscan" goto :quickscan
if /i "%CMD%"=="brief" goto :brief
if /i "%CMD%"=="kit" goto :kit
if /i "%CMD%"=="statusreport" goto :statusreport
if /i "%CMD%"=="copilot" goto :copilot
if /i "%CMD%"=="recall" goto :recall
if /i "%CMD%"=="handover" goto :handover
if /i "%CMD%"=="demo-set" goto :demoset
if /i "%CMD%"=="demo-tx" goto :demotx
if /i "%CMD%"=="score" goto :score
if /i "%CMD%"=="demo" goto :demo
if /i "%CMD%"=="demo-check" goto :democheck
if /i "%CMD%"=="test" goto :test
if /i "%CMD%"=="prompt" goto :prompt
if /i "%CMD%"=="blind" goto :blind
if /i "%CMD%"=="map" goto :map
echo tl: unknown subcommand "%CMD%" 1>&2
goto :usage

:usage
echo tl -- one entrypoint for the daily loop.
echo.
echo   up ^| doctor     preflight and current pipeline state ^(default^)
echo   env             print the SET lines, for a bare shell
echo   ollama          find and start the local model server, wherever it lives
echo   tick            the whole cadence: pool, reconcile everyone, deliver
echo   reconcile       signals + board -^> ReviewPacket ^(writes^)
echo   review          accept / edit / reject / defer the packet
echo   digest          the week, addressed to a person
echo   ask "question"  answers over confirmed state, with receipts
echo   share ^<args^>    grant / revoke / list access
echo   dashboard       serve http://127.0.0.1:8090 ^(--blind --hands-on --picker^)
echo   extract ^<file^>  one transcript -^> typed signals ^(writes^)
echo   ingest ^<file^>   a Copilot paste -^> the inbox
echo   transcripts ^<f^> a Teams transcript ^(Word .txt^) -^> the inbox
echo   quickscan ^<f^>   what a transcript names + what is new ^(read-only^)
echo   brief [meeting] before the standup: what this room said + what became of it
echo   recall ^<words^>  who said that, when -- quoted, with receipts
echo   handover ^<who^>  everything one person carries, with history
echo   kit             package the system + a ledger copy for a teammate
echo   statusreport ^<f^> --for ^<who^>  a weekly status report -^> their own claims
echo   copilot ^<file^>  build a Copilot-claims ledger beside yours, to compare
echo   demo-set        generate the synthetic transcript demo set
echo   demo-tx ^<src^>   build the demo board from transcripts ^(writes^)
echo   score ^<dir^>     extraction vs golden labels, measured
echo   demo            seed a reviewable state, no Ollama needed
echo   demo-check      drive the real app and assert the demo holds up
echo   test            every suite, forced onto a scratch database
echo   prompt          the Copilot prompt, ready to paste ^(--with ^<file^>^)
echo   blind ^<people^> who sees which ledger under TL_BLIND=1 ^(--balance^)
echo   map             rebuild docs\system_map.html
exit /b 2

:banner
echo tl: profile=%PROFILE% as=%TL_USER%
exit /b 0

rem Fail before the work, not during it.
:preflight
python scripts\doctor.py >nul 2>&1
if errorlevel 1 (
  python scripts\doctor.py
  echo.
  echo tl: doctor failed -- nothing was run. Fix the above and retry. 1>&2
  exit /b 1
)
exit /b 0

:env
echo set "DB_DSN=%DB_DSN%"
echo set "TL_INBOX=%TL_INBOX%"
echo set "TL_SYNTH_DSN=%TL_SYNTH_DSN%"
exit /b 0

:up
call :banner
python scripts\doctor.py
exit /b %errorlevel%

:ollama
rem Find and start Ollama, wherever this machine keeps it. The three
rem homes seen so far: on PATH, the installed app under LOCALAPPDATA,
rem and a bare exe in %USERPROFILE%\ollama. A server whose location
rem lives in somebody's memory is the disease this launcher exists to
rem cure; one word starts it from any of them.
set "OLLAMA_EXE="
where ollama >nul 2>&1 && set "OLLAMA_EXE=ollama"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined OLLAMA_EXE if exist "%USERPROFILE%\ollama\ollama.exe" set "OLLAMA_EXE=%USERPROFILE%\ollama\ollama.exe"
if not defined OLLAMA_EXE (
  echo tl: could not find ollama.exe -- looked on PATH, in
  echo     %LOCALAPPDATA%\Programs\Ollama, and %USERPROFILE%\ollama.
  echo     Say where it lives:  set "OLLAMA_EXE=C:\path\to\ollama.exe"
  exit /b 1
)
echo tl: starting Ollama from "%OLLAMA_EXE%" -- leave its window open
start "ollama" "%OLLAMA_EXE%" serve
exit /b 0

:tick
call :banner
call :preflight || exit /b 1
python cadence\cadence_tick.py %ARGS%
exit /b %errorlevel%

:reconcile
call :banner
call :preflight || exit /b 1
python reconciler\run_reconcile.py --user "%TL_USER%" --dsn "%DB_DSN%" --embed-model "%TL_EMBED_MODEL%" --judge-model "%TL_JUDGE_MODEL%" --write %ARGS%
exit /b %errorlevel%

:review
call :banner
call :preflight || exit /b 1
python cli\review.py --as "%TL_USER%" --dsn "%DB_DSN%" %ARGS%
exit /b %errorlevel%

:digest
call :banner
call :preflight || exit /b 1
python digest\run_digest.py --as "%TL_USER%" --dsn "%DB_DSN%" %ARGS%
exit /b %errorlevel%

:ask
if not defined ARG1 (
  echo tl: ask needs a question, e.g. tl ask "what is blocked" 1>&2
  exit /b 2
)
call :banner
call :preflight || exit /b 1
python ask\run_ask.py --as "%TL_USER%" --dsn "%DB_DSN%" %ARGS%
exit /b %errorlevel%

:share
call :banner
call :preflight || exit /b 1
if not defined ARG1 (
  python cli\share.py --as "%TL_USER%" --dsn "%DB_DSN%" list
) else (
  python cli\share.py --as "%TL_USER%" --dsn "%DB_DSN%" %ARGS%
)
exit /b %errorlevel%

:dashboard
call :banner
rem --blind, because the alternative was an environment variable that
rem had to be set in THIS window, before this command, every time -- and
rem forgetting it produces a session that runs perfectly and answers
rem nothing, since the arms were labelled all along. A setting that
rem decides whether an afternoon of five people's time is usable does
rem not belong in invisible shell state.
echo %ARGS% | findstr /C:"--blind" >nul && set "TL_BLIND=1"
echo %ARGS% | findstr /C:"--hands-on" >nul && set "TL_HANDS_ON=1"
echo %ARGS% | findstr /C:"--picker" >nul && set "TL_VIEWER_PICKER=1"
call :preflight || exit /b 1
rem Browse the literal IP; bare hostnames get https-upgraded on Windows.
echo tl: http://127.0.0.1:8090  ^(ctrl-c to stop^)
python integration\dashboard\dashboard_api.py %ARGS%
exit /b %errorlevel%

:extract
if not defined ARG1 (
  echo tl: extract needs a transcript 1>&2
  exit /b 2
)
call :banner
call :preflight || exit /b 1
python extraction\run_local.py --transcript "%ARG1%" --model "%TL_JUDGE_MODEL%" --write-db %REST%
exit /b %errorlevel%

:ingest
if not defined ARG1 (
  echo tl: ingest needs a file, e.g. tl ingest paste.txt 1>&2
  exit /b 2
)
call :banner
python scripts\copilot_ingest.py "%ARG1%" --out "%TL_INBOX%" %REST%
exit /b %errorlevel%

:statusreport
if not defined ARG1 (
  echo tl: statusreport needs a file and a person, e.g. 1>&2
  echo     tl statusreport report.txt --for jason-tjia 1>&2
  exit /b 2
)
call :banner
python scripts\status_report_ingest.py %ARGS% --dsn "%DB_DSN%"
exit /b %errorlevel%

:copilot
if not defined ARG1 (
  echo tl: copilot needs the claims file, e.g. tl copilot copilot_claims_raw.txt 1>&2
  exit /b 2
)
call :banner
python scripts\copilot_ledger.py %ARGS%
exit /b %errorlevel%

:kit
call :banner
python scripts\team_kit.py %ARGS%
exit /b %errorlevel%

:brief
call :banner
python scripts\brief.py %ARGS%
exit /b %errorlevel%

:recall
if not defined ARG1 (
  echo tl: recall needs words, e.g. tl recall synthetic data 1>&2
  exit /b 2
)
call :banner
python scripts\recall.py %ARGS%
exit /b %errorlevel%

:handover
if not defined ARG1 (
  echo tl: handover needs a person, e.g. tl handover hari 1>&2
  exit /b 2
)
call :banner
python scripts\handover.py %ARGS%
exit /b %errorlevel%

:quickscan
if not defined ARG1 (
  echo tl: quickscan needs a transcript, e.g. tl quickscan transcripts_raw.txt 1>&2
  exit /b 2
)
call :banner
python scripts\quickscan.py "%ARG1%" %REST%
exit /b %errorlevel%

:transcripts
if not defined ARG1 (
  echo tl: transcripts needs a .txt, e.g. tl transcripts transcripts_raw.txt 1>&2
  exit /b 2
)
call :banner
python scripts\transcript_ingest.py "%ARG1%" --out "%TL_INBOX%" %REST%
exit /b %errorlevel%

:demoset
python testdata\synth_transcripts.py --ingest %ARGS%
exit /b %errorlevel%

:demotx
rem Its own database on purpose: this seeds decisions in other people's
rem names, which is defensible on invented work and on nothing else.
if not defined ARG1 (
  echo tl: demo-tx needs a transcripts .txt or a window directory 1>&2
  exit /b 2
)
if not defined TL_DEMO_DSN set "TL_DEMO_DSN=host=localhost port=%TL_PORT% dbname=taskintel_txdemo user=%USERNAME%"
python scripts\demo_transcript_build.py "%ARG1%" --dsn "%TL_DEMO_DSN%" %REST%
exit /b %errorlevel%

:score
if not defined ARG1 (
  echo tl: score needs a corpus directory ^(with golden.json^) 1>&2
  exit /b 2
)
for %%I in ("%ARG1%") do set "CORPUS=%%~fI"
cd /d "%~dp0testdata"
python score_extraction.py --corpus "%CORPUS%" --model "%TL_JUDGE_MODEL%" %REST%
exit /b %errorlevel%

:demo
call :banner
call :preflight || exit /b 1
python scripts\demo_offline.py %ARGS%
exit /b %errorlevel%

:democheck
call :banner
python scripts\demo_preflight.py %ARGS%
exit /b %errorlevel%

:test
rem run_tests.bat mutates whatever board it is pointed at. Forced onto
rem scratch so an inherited real DB_DSN cannot become a write to it.
if defined TL_SCRATCH_DSN (
  set "DB_DSN=%TL_SCRATCH_DSN%"
) else (
  set "DB_DSN=host=localhost port=%TL_PORT% dbname=taskintel_scratch user=%USERNAME%"
)
rem /C: makes the trailing space part of ONE literal pattern. Without it
rem findstr treats the space as OR between two patterns, so the guard
rem matched "dbname=taskintel" as a substring -- which includes
rem taskintel_scratch, the very database this branch just selected. The
rem guard refused everything, including the safe thing it existed to
rem allow. It failed safe, but a protection that always fires is no
rem protection: it teaches people to work around it.
echo %DB_DSN% | findstr /C:"dbname=taskintel " >nul && (
  echo tl: refusing to run the suites against taskintel. 1>&2
  exit /b 1
)
echo tl: suites against a scratch database
call run_tests.bat %ARGS%
exit /b %errorlevel%

:prompt
python scripts\show_prompt.py %ARGS%
exit /b %errorlevel%

:blind
python scripts\blind_map.py %ARGS%
exit /b %errorlevel%

:map
python scripts\build_system_map.py %ARGS%
exit /b %errorlevel%
