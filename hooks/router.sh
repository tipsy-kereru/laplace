#!/bin/sh
# Laplace event router. POSIX sh.
#
# Dispatches SessionStart / UserPromptSubmit events. Reads the event name from
# $1 (or $CLAUDE_HOOK_EVENT), reads stdin exactly once, and routes.
#
# HARD INVARIANT: this script NEVER blocks the user or crashes the session.
# Every path exits 0. Logs go to stderr; stdout is reserved for JSON context
# injection on UserPromptSubmit when a Laplace signal is detected.
#
# jq is used when available; we fall back to grep -o. Malformed stdin fails open.

set -u

EVENT="${1:-${CLAUDE_HOOK_EVENT:-unknown}}"
STDIN_FILE="${CLAUDE_HOOK_STDIN_FILE:-}"

# Read stdin once. CLAUDE_HOOK_STDIN_FILE (if set) points at a file containing
# the JSON payload; otherwise read from stdin.
if [ -n "$STDIN_FILE" ] && [ -r "$STDIN_FILE" ]; then
  PAYLOAD=$(cat "$STDIN_FILE" 2>/dev/null || true)
else
  PAYLOAD=$(cat 2>/dev/null || true)
fi

# Whether jq is on PATH.
HAVE_JQ=0
command -v jq >/dev/null 2>&1 && HAVE_JQ=1

extract_field() {
  # $1 = field name. Prints the value or empty string on failure.
  field="$1"
  if [ "$HAVE_JQ" -eq 1 ]; then
    printf '%s' "$PAYLOAD" | jq -r --arg f "$field" '.[$f] // empty' 2>/dev/null || true
  else
    # Best-effort grep fallback: matches "field": "...value..."
    printf '%s' "$PAYLOAD" | grep -o "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
      | head -1 | sed 's/.*:[[:space:]]*"//; s/"$//' 2>/dev/null || true
  fi
}

case "$EVENT" in
  user-prompt-submit)
    PROMPT=$(extract_field prompt)
    # Detect Laplace signal markers. Never block — only inject minimal context.
    SIGNAL_DETECTED=0
    case "$PROMPT" in
      *"/laplace:"*) SIGNAL_DETECTED=1 ;;
      *"LAPLACE-P0P6-COMPLETE"*) SIGNAL_DETECTED=1 ;;
      *"LAPLACE-"*"-COMPLETE"*) SIGNAL_DETECTED=1 ;;
    esac
    if [ "$SIGNAL_DETECTED" -eq 1 ]; then
      # Emit minimal JSON context injection. Claude Code merges this into the
      # user's prompt context. Keep it tiny and never include secrets.
      printf '{"userPrompt": "Laplace signal detected. Routing context: /laplace:run active harness under .harness/."}\n'
    fi
    exit 0
    ;;
  session-start)
    # If a harness is initialized, print a one-line summary to stderr.
    # stdout stays clean (no context injection for session-start in MVP).
    if [ -d ".harness" ]; then
      # Best-effort issue + state extraction. Failures fall back to generic line.
      if [ -f ".harness/state/tasks.json" ] && [ "$HAVE_JQ" -eq 1 ]; then
        SUMMARY=$(jq -r 'to_entries | map("\(.key)=\(.value.status)") | join(", ")' \
          .harness/state/tasks.json 2>/dev/null | head -c 200 || true)
        if [ -n "$SUMMARY" ]; then
          printf 'Laplace active: %s\n' "$SUMMARY" >&2
        else
          printf 'Laplace active: harness initialized\n' >&2
        fi
      else
        printf 'Laplace active: harness initialized\n' >&2
      fi
    fi
    exit 0
    ;;
  *)
    # Unknown event: exit 0 silently.
    exit 0
    ;;
esac

exit 0
