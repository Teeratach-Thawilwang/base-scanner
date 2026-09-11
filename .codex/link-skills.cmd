@echo off
REM Windows twin of link-skills.sh. A directory junction, not a symlink, because a
REM junction needs no admin rights and no Developer Mode.
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.codex\skills" rmdir "%ROOT%\.codex\skills"
mklink /J "%ROOT%\.codex\skills" "%ROOT%\.claude\skills"
