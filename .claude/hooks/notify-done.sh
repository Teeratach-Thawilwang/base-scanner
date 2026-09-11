#!/bin/bash
# Stop hook: desktop notification carrying the first line of the last assistant message.
# macOS (osascript) / Windows (PowerShell toast) / Linux (notify-send).

TITLE="${1:-Claude Code}"
STATUS="${2:-Done!}"
# Each runtime gets its own sound, so a finished turn says which one finished without
# looking. The caller sets these in .claude/settings.json or .codex/hooks.json; Claude
# sets neither and keeps the defaults. A Mac sound is a name in /System/Library/Sounds
# or ~/Library/Sounds, a Windows one is an ms-winsoundevent the toast API knows.
MAC_SOUND="${NOTIFY_MAC_SOUND:-Glass}"
WINDOWS_SOUND="${NOTIFY_WINDOWS_SOUND:-ms-winsoundevent:Notification.Default}"

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
    text = data.get('last_assistant_message', '')
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    if lines:
        summary = lines[0]
        summary = re.sub(r'^\*\*(.+?)\*\*', r'\1', summary)
        summary = re.sub(r'^#+\s*', '', summary)
        summary = re.sub(r'^- ', '', summary)
        summary = summary.replace('\`', '')
        if len(summary) > 80:
            summary = summary[:77] + '...'
        print(summary)
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
