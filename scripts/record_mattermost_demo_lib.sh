#!/usr/bin/env bash

# Shell helpers for the Mattermost demo recorder. The caller owns strict mode and cleanup.

mattermost_login() {
  local username="$1" password="$2" headers body token
  headers="$(mktemp)"
  body="$(mktemp)"
  curl -fsS -D "$headers" -o "$body" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg login "$username" --arg password "$password" \
      '{login_id:$login,password:$password}')" \
    "${MM_URL%/}/api/v4/users/login"
  token="$(awk 'BEGIN{IGNORECASE=1} /^Token:/{gsub("\r",""); print $2}' "$headers")"
  printf '%s|%s|%s\n' "$token" "$(jq -r .id "$body")" "$(jq -r .username "$body")"
  rm -f -- "$headers" "$body"
}

seed_demo_post() {
  local token="$1" message="$2"
  curl -fsS -o /dev/null -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg channel "$MM_GROUP_ID" --arg message "$message" \
      '{channel_id:$channel,message:$message}')" "${MM_URL%/}/api/v4/posts"
}

prepare_demo_group() {
  local primary_token persona_token team_id channel_name group_json
  IFS='|' read -r primary_token MM_PRIMARY_USER_ID MM_PRIMARY_NAME \
    <<<"$(mattermost_login "$MM_RECORD_USERNAME" "$MM_RECORD_PASSWORD")"
  IFS='|' read -r persona_token MM_PERSONA_USER_ID MM_PERSONA_NAME \
    <<<"$(mattermost_login "$MM_PERSONA_USERNAME" "$MM_PERSONA_PASSWORD")"
  MM_BOT_ID="$(curl -fsS -H "Authorization: Bearer $primary_token" \
    "${MM_URL%/}/api/v4/users/username/${MM_RECORD_BOT#@}" | jq -r .id)"
  team_id="$(curl -fsS -H "Authorization: Bearer $primary_token" \
    "${MM_URL%/}/api/v4/users/me/teams" | jq -r '.[0].id')"
  channel_name="mmag-demo-$(date -u +%H%M%S)-$$"
  group_json="$(curl -fsS -H "Authorization: Bearer $primary_token" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg team "$team_id" --arg name "$channel_name" \
      '{team_id:$team,name:$name,display_name:"项目演示 · 智能协作",type:"P",purpose:"MMAG 多人协作演示"}')" \
    "${MM_URL%/}/api/v4/channels")"
  MM_GROUP_ID="$(jq -r .id <<<"$group_json")"
  MM_GROUP_NAME="$(jq -r .name <<<"$group_json")"
  if [[ -z "$MM_GROUP_ID" || "$MM_GROUP_ID" == null ]]; then
    printf 'Could not prepare the multi-user Mattermost demo channel.\n' >&2
    return 1
  fi
  for user_id in "$MM_PERSONA_USER_ID" "$MM_BOT_ID"; do
    curl -fsS -o /dev/null -H "Authorization: Bearer $primary_token" \
      -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg user "$user_id" --arg channel "$MM_GROUP_ID" \
        '{user_id:$user,channel_id:$channel}')" \
      "${MM_URL%/}/api/v4/channels/$MM_GROUP_ID/members"
  done
  seed_demo_post "$primary_token" \
    "产品侧：本周演示聚焦个人工作台、数字人和群聊总结，周三前冻结范围。"
  seed_demo_post "$persona_token" \
    "研发侧：Agent、Skill 与审批主链已经可用；PPT 导出还要完成一次真实验收。"
  seed_demo_post "$primary_token" \
    "行动项：测试负责人今天完成 Mattermost 全流程回归，发现阻塞立即在群里同步。"
  export MM_GROUP_ID MM_GROUP_NAME MM_PRIMARY_NAME MM_PERSONA_NAME
}

rollback_demo_manifest() {
  local manifest="$1" baseline="$2" complete="$3" temporary
  [[ "$complete" == 1 || ! -f "$manifest" ]] && return
  temporary="$(mktemp "${manifest%/*}/.clips.XXXXXX")"
  head -n "$baseline" "$manifest" >"$temporary"
  mv -f -- "$temporary" "$manifest"
}

result_value() {
  awk '/^### Result/{getline; gsub(/\r/, ""); gsub(/^"|"$/, ""); print; exit}'
}

expand_thread() {
  pw run-code "$EXPAND_THREAD_JS" >/dev/null
}

slash_clip() {
  local name="$1" label="$2" command="$3"
  start_clip
  pw sessionstorage-set mmag-demo-message "$command" >/dev/null
  pw sessionstorage-set mmag-demo-browser-op send-slash >/dev/null
  pw run-code "$BROWSER_HELPER_JS" >/dev/null
  stop_clip "$name" "$label"
}

action_clip() {
  local name="$1" label="$2" action="$3"
  start_clip
  pw sessionstorage-set mmag-demo-message "$action" >/dev/null
  pw sessionstorage-set mmag-demo-browser-op click-action >/dev/null
  pw run-code "$BROWSER_HELPER_JS" >/dev/null
  stop_clip "$name" "$label"
}
