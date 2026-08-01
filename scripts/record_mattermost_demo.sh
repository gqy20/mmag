#!/usr/bin/env bash
set -euo pipefail

# Record the real Mattermost user → Bot → Agent → approval → Artifact flow.
# Credentials stay in environment variables; generated media and logs stay under .eval-runs/.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
OUTPUT_ROOT="$PROJECT_ROOT/.eval-runs/recordings"
START_BOT=1
DRY_RUN=0
SESSION="mmag-recording-$$"

usage() {
  printf '%s\n' \
    "Usage: scripts/record_mattermost_demo.sh [options]" \
    "" \
    "Options:" \
    "  --env-file PATH       Environment file (default: .env)" \
    "  --output-dir PATH     Recording root (default: .eval-runs/recordings)" \
    "  --bot-already-running Do not start and stop uv run mmag" \
    "  --dry-run             Validate configuration and print the shot list" \
    "  -h, --help            Show this help"
}

while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --bot-already-running)
      START_BOT=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  printf 'Environment file not found: %s\n' "$ENV_FILE" >&2
  exit 2
fi

EXPLICIT_E2E="${MMAG_E2E_ENABLED:-}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
if [[ -n "$EXPLICIT_E2E" ]]; then
  MMAG_E2E_ENABLED="$EXPLICIT_E2E"
  export MMAG_E2E_ENABLED
fi

MM_RECORD_USERNAME="${MM_USERNAME:-${EMAIL:-}}"
MM_RECORD_PASSWORD="${MM_PASSWORD:-${PASSWORD:-}}"
MM_RECORD_BOT="${MM_E2E_BOT_USERNAME:-}"
MM_RECORD_ACK="${MM_ACK_MESSAGE:-get}"
export MM_RECORD_USERNAME MM_RECORD_PASSWORD MM_RECORD_BOT MM_RECORD_ACK

required=(MM_URL MM_RECORD_USERNAME MM_RECORD_PASSWORD MM_RECORD_BOT)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    printf 'Required recording value is missing: %s\n' "$name" >&2
    exit 2
  fi
done
if [[ ! "${MMAG_E2E_ENABLED:-}" =~ ^(1|true|yes)$ ]]; then
  printf 'Real recording is disabled; set MMAG_E2E_ENABLED=1 explicitly.\n' >&2
  exit 2
fi

SHOT_LIST=(
  "01 default-get: one accepted MMChat request immediately receives get"
  "02 mmchat: the same request continues to a real LLM answer and routing marker"
  "03 project: approved knowledge write followed by retrieval proof"
  "04 ppt: real ppt Agent, approvals, sandboxed generation and PPTX delivery"
  "05 evidence: final Mattermost result and artifact remain visible"
)

if ((DRY_RUN)); then
  printf 'Recording configuration is valid.\n'
  printf 'Mattermost: %s\n' "$MM_URL"
  printf 'Bot: @%s\n' "${MM_RECORD_BOT#@}"
  printf 'Shot list:\n'
  printf '  - %s\n' "${SHOT_LIST[@]}"
  exit 0
fi

for command in playwright-cli ffmpeg ffprobe fc-match rg uv; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command is unavailable: %s\n' "$command" >&2
    exit 2
  fi
done

RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEMO_RUN_ID="$RUN_STAMP-$$"
RUN_DIR="$OUTPUT_ROOT/$RUN_STAMP"
RAW_DIR="$RUN_DIR/raw"
EDIT_DIR="$RUN_DIR/edit"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$RAW_DIR" "$EDIT_DIR" "$LOG_DIR"

BOT_PID=""
cleanup() {
  playwright-cli -s="$SESSION" close >/dev/null 2>&1 || true
  if [[ -n "$BOT_PID" ]] && kill -0 "$BOT_PID" >/dev/null 2>&1; then
    kill -INT "$BOT_PID" >/dev/null 2>&1 || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ((START_BOT)); then
  (
    cd "$PROJECT_ROOT"
    LOG_DIR="$LOG_DIR" PYTHONUNBUFFERED=1 uv run mmag
  ) >"$LOG_DIR/bot-console.log" 2>&1 &
  BOT_PID=$!
  ready=0
  for _ in $(seq 1 90); do
    if ! kill -0 "$BOT_PID" >/dev/null 2>&1; then
      printf 'Bot exited before becoming ready. See %s\n' "$LOG_DIR/bot-console.log" >&2
      exit 1
    fi
    if rg -q 'Agent 就绪' "$LOG_DIR/bot-console.log"; then
      ready=1
      break
    fi
    sleep 1
  done
  if ((ready == 0)); then
    printf 'Bot did not become ready within 90 seconds.\n' >&2
    exit 1
  fi
fi

pw() {
  playwright-cli -s="$SESSION" "$@"
}

# The embedded JavaScript must remain single-quoted so Bash does not expand its
# template literals or process.env references.
# shellcheck disable=SC2016
LOGIN_JS='async page => {
  const login = page.locator("input[name=loginId], input[autocomplete=username], input[type=email], input[type=text]").first();
  const secret = page.locator("input[name=password], input[autocomplete=current-password], input[type=password]").first();
  await login.waitFor({state: "visible", timeout: 30000});
  await login.fill(process.env.MM_RECORD_USERNAME);
  await secret.fill(process.env.MM_RECORD_PASSWORD);
  await secret.press("Enter");
  await page.waitForURL(url => !url.pathname.includes("/login"), {timeout: 30000});
}'

# shellcheck disable=SC2016
OPEN_DM_JS='async page => {
  const bot = process.env.MM_RECORD_BOT.replace(/^@/, "");
  let team = new URL(page.url()).pathname.split("/").filter(Boolean)[0] || "";
  const teamId = process.env.MM_TEAM_ID || "";
  if (teamId) {
    const response = await page.context().request.get(`${process.env.MM_URL}/api/v4/teams/${teamId}`);
    if (response.ok()) team = (await response.json()).name || team;
  }
  if (!team) throw new Error("Could not resolve the Mattermost team name");
  await page.goto(`${process.env.MM_URL}/${team}/messages/@${bot}`);
  await page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").last().waitFor({state: "visible", timeout: 30000});
}'

# shellcheck disable=SC2016
SEND_JS='async page => {
  const body = page.locator("body");
  const pattern = /批准\s+`?([A-Za-z0-9_-]{8,128})`?/g;
  const seen = [...(await body.innerText()).matchAll(pattern)].map(item => item[1]);
  sessionStorage.setItem("mmag-demo-seen-approvals", JSON.stringify(seen));
  sessionStorage.setItem("mmag-demo-next", "");
  const ack = process.env.MM_RECORD_ACK;
  const before = await page.getByText(ack, {exact: true}).count();
  const composer = page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").last();
  await composer.fill(process.env.MMAG_DEMO_MESSAGE);
  await composer.press("Enter");
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await page.getByText(ack, {exact: true}).count() > before) return;
    await page.waitForTimeout(250);
  }
  throw new Error(`Timed out waiting for the default acknowledgment: ${ack}`);
}'

# shellcheck disable=SC2016
WAIT_NEXT_JS='async page => {
  const marker = process.env.MMAG_DEMO_MARKER;
  const seen = new Set(JSON.parse(sessionStorage.getItem("mmag-demo-seen-approvals") || "[]"));
  const pattern = /批准\s+`?([A-Za-z0-9_-]{8,128})`?/g;
  const deadline = Date.now() + Number(process.env.MMAG_DEMO_TIMEOUT_MS || 300000);
  while (Date.now() < deadline) {
    const text = await page.locator("body").innerText();
    if (text.split(marker).length - 1 >= 2) {
      sessionStorage.setItem("mmag-demo-next", "final");
      return;
    }
    const ids = [...text.matchAll(pattern)].map(item => item[1]);
    const next = ids.find(id => !seen.has(id));
    if (next) {
      seen.add(next);
      sessionStorage.setItem("mmag-demo-seen-approvals", JSON.stringify([...seen]));
      sessionStorage.setItem("mmag-demo-approval", next);
      sessionStorage.setItem("mmag-demo-next", "approval");
      return;
    }
    await page.waitForTimeout(500);
  }
  throw new Error(`Timed out waiting for ${marker} or an approval`);
}'

# shellcheck disable=SC2016
APPROVE_JS='async page => {
  const approval = sessionStorage.getItem("mmag-demo-approval");
  if (!approval) throw new Error("No approval ID was captured");
  const composer = page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").last();
  await composer.fill(`批准 ${approval}`);
  await composer.press("Enter");
  await page.waitForTimeout(2500);
  sessionStorage.setItem("mmag-demo-next", "");
}'

HOLD_JS='async page => {
  await page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").last().scrollIntoViewIfNeeded();
  await page.waitForTimeout(Number(process.env.MMAG_DEMO_HOLD_MS || 5000));
}'

pw open "$MM_URL" >/dev/null
pw resize 1920 1080 >/dev/null
pw run-code "$LOGIN_JS" >/dev/null
pw run-code "$OPEN_DM_JS" >/dev/null

declare -a CLIP_PATHS=()
declare -a CLIP_SPEEDS=()
declare -a CLIP_LABELS=()
CLIP_INDEX=0

start_clip() {
  pw video-start >/dev/null
}

stop_clip() {
  local name="$1"
  local speed="$2"
  local label="$3"
  CLIP_INDEX=$((CLIP_INDEX + 1))
  local path
  path="$RAW_DIR/$(printf '%02d' "$CLIP_INDEX")-$name.webm"
  pw video-stop --filename="$path" >/dev/null
  CLIP_PATHS+=("$path")
  CLIP_SPEEDS+=("$speed")
  CLIP_LABELS+=("$label")
}

send_clip() {
  local name="$1"
  local label="$2"
  export MMAG_DEMO_MESSAGE="$3"
  export MMAG_DEMO_HOLD_MS="${4:-2000}"
  start_clip
  pw run-code "$SEND_JS" >/dev/null
  pw run-code "$HOLD_JS" >/dev/null
  stop_clip "$name" 1 "$label"
}

wait_clip() {
  local name="$1"
  local speed="$2"
  local label="$3"
  export MMAG_DEMO_MARKER="$4"
  export MMAG_DEMO_TIMEOUT_MS=300000
  start_clip
  pw run-code "$WAIT_NEXT_JS" >/dev/null
  stop_clip "$name" "$speed" "$label"
}

state_value() {
  local output
  output="$(pw eval "sessionStorage.getItem('mmag-demo-next')")"
  printf '%s\n' "$output" | awk '/^### Result/{getline; gsub(/^"|"$/, ""); print; exit}'
}

approve_clip() {
  start_clip
  pw run-code "$APPROVE_JS" >/dev/null
  stop_clip "$1" 1 "$2"
}

finish_with_approvals() {
  local prefix="$1"
  local label="$2"
  local marker="$3"
  local max_approvals="$4"
  local count=0
  while true; do
    wait_clip "$prefix-wait-$count" 5 "$label" "$marker"
    local state
    state="$(state_value)"
    if [[ "$state" == "final" ]]; then
      break
    fi
    if [[ "$state" != "approval" ]]; then
      printf 'Unexpected browser recording state: %s\n' "$state" >&2
      exit 1
    fi
    count=$((count + 1))
    if ((count > max_approvals)); then
      printf 'Approval count exceeded the recording limit for %s\n' "$prefix" >&2
      exit 1
    fi
    approve_clip "$prefix-approve-$count" "人工审批"
  done
}

MMCHAT_MARKER="DEMO-MMCHAT-02-$DEMO_RUN_ID"
send_clip \
  "default-get" \
  "默认 get：请求已进入处理" \
  "@${MM_RECORD_BOT#@} 请从 Agent、Skill、Capability、Policy 四个层次说明 MMAG 如何防止模型自行扩大权限，并给出一个审批示例。请保留编号 $MMCHAT_MARKER。"
finish_with_approvals "mmchat" "真实 AI 生成等待" "$MMCHAT_MARKER" 0
export MMAG_DEMO_HOLD_MS=7000
start_clip
pw run-code "$HOLD_JS" >/dev/null
stop_clip "mmchat-result" 1 "检查真实回复内容"

PROJECT_MARKER="DEMO-PROJECT-03-$DEMO_RUN_ID"
send_clip \
  "project-request" \
  "Project Agent 与 Skill" \
  "项目助理：确认‘所有通过入口验收的用户消息默认回复 get’为当前项目决策，并保存到知识库。请保留编号 $PROJECT_MARKER。"
finish_with_approvals "project" "知识写入等待" "$PROJECT_MARKER" 2

PROJECT_VERIFY_MARKER="DEMO-PROJECT-VERIFY-03-$DEMO_RUN_ID"
send_clip \
  "project-verify-request" \
  "验证知识真正写入" \
  "项目助理：查询刚才确认的默认 ACK 决策，并保留编号 $PROJECT_VERIFY_MARKER。"
finish_with_approvals "project-verify" "知识读取等待" "$PROJECT_VERIFY_MARKER" 0
export MMAG_DEMO_HOLD_MS=7000
start_clip
pw run-code "$HOLD_JS" >/dev/null
stop_clip "project-result" 1 "Project 决策回读成功"

PPT_MARKER="DEMO-PPT-04-$DEMO_RUN_ID"
send_clip \
  "ppt-request" \
  "真实 PPT Agent" \
  "PPT 助理：生成一份三页的《MMAG 企业智能体能力概览》。第 1 页是 Agent、Skill、Capability 架构；第 2 页是 Policy、审批与 Sandbox 安全边界；第 3 页是 Mattermost 交互与评估闭环。使用简洁企业风格，生成 PPTX 并发送到当前线程。请保留编号 $PPT_MARKER。"
finish_with_approvals "ppt" "PPT 生成与上传等待" "$PPT_MARKER" 4
export MMAG_DEMO_HOLD_MS=9000
start_clip
pw run-code "$HOLD_JS" >/dev/null
stop_clip "ppt-result" 1 "PPTX Artifact 交付"

export MMAG_DEMO_HOLD_MS=5000
start_clip
pw run-code "$HOLD_JS" >/dev/null
stop_clip "final-evidence" 1 "真实 Agent 与 Artifact 证据"

FONT_FILE="$(fc-match -f '%{file}\n' 'Noto Sans CJK SC' | head -1)"
if [[ -z "$FONT_FILE" || ! -f "$FONT_FILE" ]]; then
  FONT_FILE="$(fc-match -f '%{file}\n' sans-serif | head -1)"
fi

TITLE_MP4="$EDIT_DIR/00-title.mp4"
END_MP4="$EDIT_DIR/99-end.mp4"
ffmpeg -y -v error -f lavfi -i color=c=0x111827:s=1920x1080:d=5:r=25 \
  -vf "drawtext=fontfile='$FONT_FILE':text='MMAG 企业智能体真实演示':fontcolor=white:fontsize=64:x=(w-text_w)/2:y=430,drawtext=fontfile='$FONT_FILE':text='Mattermost · Agent · Skill · Approval · Artifact':fontcolor=0x93c5fd:fontsize=36:x=(w-text_w)/2:y=520" \
  -an -c:v libx264 -pix_fmt yuv420p "$TITLE_MP4"

NORMALIZED=("$TITLE_MP4")
for index in "${!CLIP_PATHS[@]}"; do
  input="${CLIP_PATHS[$index]}"
  speed="${CLIP_SPEEDS[$index]}"
  label="${CLIP_LABELS[$index]}"
  output="$EDIT_DIR/$(printf '%02d' "$((index + 1))").mp4"
  ffmpeg -y -v error -i "$input" \
    -vf "setpts=PTS/$speed,fps=25,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x111827,drawtext=fontfile='$FONT_FILE':text='$label':fontcolor=white:fontsize=32:box=1:boxcolor=black@0.65:boxborderw=18:x=36:y=36" \
    -an -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p "$output"
  NORMALIZED+=("$output")
done

ffmpeg -y -v error -f lavfi -i color=c=0x111827:s=1920x1080:d=5:r=25 \
  -vf "drawtext=fontfile='$FONT_FILE':text='真实流程完成':fontcolor=white:fontsize=68:x=(w-text_w)/2:y=440,drawtext=fontfile='$FONT_FILE':text='默认 get · 真实 Agent · 人工审批 · PPTX 交付':fontcolor=0x86efac:fontsize=36:x=(w-text_w)/2:y=535" \
  -an -c:v libx264 -pix_fmt yuv420p "$END_MP4"
NORMALIZED+=("$END_MP4")

CONCAT_FILE="$EDIT_DIR/concat.txt"
: >"$CONCAT_FILE"
for path in "${NORMALIZED[@]}"; do
  printf "file '%s'\n" "$path" >>"$CONCAT_FILE"
done

FINAL_VIDEO="$RUN_DIR/mmag-real-agent-demo.mp4"
ffmpeg -y -v error -f concat -safe 0 -i "$CONCAT_FILE" -c copy -movflags +faststart "$FINAL_VIDEO"
ffmpeg -v error -i "$FINAL_VIDEO" -f null -

DURATION="$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FINAL_VIDEO")"
SIZE="$(stat -c '%s' "$FINAL_VIDEO")"
CHECKSUM="$(sha256sum "$FINAL_VIDEO" | awk '{print $1}')"
printf 'video=%s\n' "$FINAL_VIDEO"
printf 'duration_seconds=%s\n' "$DURATION"
printf 'size_bytes=%s\n' "$SIZE"
printf 'sha256=%s\n' "$CHECKSUM"

awk -v duration="$DURATION" 'BEGIN { if (duration > 195) exit 1 }' || {
  printf 'Warning: final video exceeds the 3m15s upper target; review wait acceleration.\n' >&2
}
