#!/bin/bash
# PreToolUse hook: desktop notification carrying the question text.
# Claude asks with AskUserQuestion, Codex asks with request_user_input, and the
# runtime's name arrives as $1 so the toast says which one is waiting.
# macOS (osascript) / Windows (PowerShell toast) / Linux (notify-send).

. "$(dirname "${BASH_SOURCE[0]}")/hook-lib.sh"

# The project name is what tells two windows apart, so it carries the title, and a
# runtime that names itself in $1 stays in front of it: "Codex: Ai Adapter".
PROJECT_LABEL="$(hook_project_label)"
if [ -n "${1:-}" ]; then
  TITLE="$1${PROJECT_LABEL:+: $PROJECT_LABEL}"
else
  TITLE="${PROJECT_LABEL:-Claude Code}"
fi
STATUS="มีคำถามรอตอบ"
MAC_SOUND="kuay-anutin"
WINDOWS_SOUND="ms-winsoundevent:Notification.Reminder"

# The title and the text ride in as argv. Pasting them into the script source instead
# breaks on the first double quote one of them holds, and osascript reports that onto a
# stderr no runtime reads, so the notification is simply lost, silently.
notify_macos() {
  osascript - "$TITLE" "$STATUS" "$MAC_SOUND" <<'APPLESCRIPT'
on run {noteTitle, noteText, noteSound}
  display notification noteText with title noteTitle sound name noteSound
end run
APPLESCRIPT
}

# ToastText02 = bold title line + wrapped body line.
notify_windows() {
  NOTIFY_TITLE="$TITLE" NOTIFY_TEXT="$STATUS" NOTIFY_SOUND="$WINDOWS_SOUND" \
    powershell.exe -NoProfile -NonInteractive -Command '
      [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
      $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
      $nodes = $xml.GetElementsByTagName("text")
      [void]$nodes.Item(0).AppendChild($xml.CreateTextNode($env:NOTIFY_TITLE))
      [void]$nodes.Item(1).AppendChild($xml.CreateTextNode($env:NOTIFY_TEXT))
      $audio = $xml.CreateElement("audio")
      $audio.SetAttribute("src", $env:NOTIFY_SOUND)
      [void]$xml.DocumentElement.AppendChild($audio)
      $appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
      [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
    ' 2>/dev/null
}

notify_linux() {
  command -v notify-send >/dev/null 2>&1 && notify-send "$TITLE" "$STATUS"
}

notify() {
  case "$OSTYPE" in
    darwin*) notify_macos ;;
    msys* | cygwin* | win32) notify_windows ;;
    *) notify_linux ;;
  esac
}

if [ ! -t 0 ]; then
    JSON=$(cat)
    if [ -n "$JSON" ]; then
        MSG=$(echo "$JSON" | python3 -c "
import sys, json, re
try:
    data = json.load(sys.stdin)
    tool_input = data.get('tool_input', {}) or {}
    text = ''
    for key in ('question', 'prompt', 'message', 'text'):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    # Codex has shipped the question inside a list of questions too.
    if not text:
        first = (tool_input.get('questions') or [None])[0]
        if isinstance(first, dict):
            text = first.get('question') or first.get('prompt') or ''
        elif isinstance(first, str):
            text = first
    if text:
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'^#+\s*', '', text)
        text = text.replace('\`', '').strip()
        if len(text) > 80:
            text = text[:77] + '...'
        print(text)
except:
    pass
" 2>/dev/null)
        if [ -n "$MSG" ]; then
            STATUS="$MSG"
        fi
    fi
fi

notify
exit 0
