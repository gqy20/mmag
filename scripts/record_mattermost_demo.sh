#!/usr/bin/env bash
set -euo pipefail

# Record the real Mattermost user → Bot → Agent → approval → Artifact flow.
# Credentials stay in environment variables; generated media and logs stay under .eval-runs/.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
OUTPUT_ROOT="$PROJECT_ROOT/.eval-runs/recordings"
START_BOT=1
DRY_RUN=0
SKIP_PPT=0
SESSION="mmag-recording-$$"
VIEWPORT_WIDTH=1600
VIEWPORT_HEIGHT=900
VIDEO_WIDTH=2560
VIDEO_HEIGHT=1440

usage() {
  printf '%s\n' \
    "Usage: scripts/record_mattermost_demo.sh [options]" \
    "" \
    "Options:" \
    "  --env-file PATH       Environment file (default: .env)" \
    "  --output-dir PATH     Recording root (default: .eval-runs/recordings)" \
    "  --bot-already-running Do not start and stop uv run mmag" \
    "  --skip-ppt            Record all real Agent scenes except PPT" \
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
    --skip-ppt)
      SKIP_PPT=1
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
  "03 project: approved knowledge write"
  "04 evidence: the real approval remains visible"
)
if ((SKIP_PPT == 0)); then
  SHOT_LIST+=("05 ppt: real Agent, approvals, exact preview.png/deck.pptx delivery and native preview")
fi

if ((DRY_RUN)); then
  printf 'Recording configuration is valid.\n'
  printf 'Mattermost: %s\n' "$MM_URL"
  printf 'Bot: @%s\n' "${MM_RECORD_BOT#@}"
  printf 'Shot list:\n'
  printf '  - %s\n' "${SHOT_LIST[@]}"
  exit 0
fi

for command in playwright-cli ffmpeg ffprobe fc-match node rg uv; do
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
AUTH_STATE_DIR="$(mktemp -d)"
AUTH_STATE_FILE="$AUTH_STATE_DIR/mattermost-auth.json"

BOT_PID=""
cleanup() {
  playwright-cli -s="$SESSION" close >/dev/null 2>&1 || true
  if [[ -n "$BOT_PID" ]] && kill -0 "$BOT_PID" >/dev/null 2>&1; then
    kill -INT "$BOT_PID" >/dev/null 2>&1 || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  rm -rf -- "$AUTH_STATE_DIR"
}
trap cleanup EXIT INT TERM

if ((START_BOT)); then
  (
    cd "$PROJECT_ROOT"
    LOG_DIR="$LOG_DIR" \
      MAX_CONTEXT_MESSAGES="${MMAG_DEMO_MAX_CONTEXT_MESSAGES:-6}" \
      MAX_CONTEXT_CHARS="${MMAG_DEMO_MAX_CONTEXT_CHARS:-8000}" \
      PYTHONUNBUFFERED=1 \
      uv run mmag
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

MM_URL_JSON="$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$MM_URL")"
MM_USERNAME_JSON="$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$MM_RECORD_USERNAME")"
MM_PASSWORD_JSON="$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$MM_RECORD_PASSWORD")"

# Test credentials are JSON-escaped into this one suppressed Playwright command.
# They are not written to generated media, Bot logs, or recording metadata.
LOGIN_JS="async page => {
  const base = $MM_URL_JSON.replace(/\/$/, \"\");
  const current = await page.context().request.get(base + \"/api/v4/users/me\");
  if (current.ok()) return;
  const login = await page.context().request.post(base + \"/api/v4/users/login\", {
    data: {login_id: $MM_USERNAME_JSON, password: $MM_PASSWORD_JSON}
  });
  if (!login.ok()) throw new Error(\"Mattermost login failed: \" + login.status());
  const user = await login.json();
  const token = login.headers().token || \"\";
  if (!token || !user.id) throw new Error(\"Mattermost login response is incomplete\");
  await page.context().addCookies([
    {name: \"MMAUTHTOKEN\", value: token, url: base, httpOnly: true, sameSite: \"Lax\"},
    {name: \"MMUSERID\", value: String(user.id), url: base, sameSite: \"Lax\"}
  ]);
  const verified = await page.context().request.get(base + \"/api/v4/users/me\");
  if (!verified.ok()) throw new Error(\"Mattermost session verification failed: \" + verified.status());
}"

# shellcheck disable=SC2016
OPEN_DM_JS='async page => {
  const config = await page.evaluate(() => ({
    bot: sessionStorage.getItem("mmag-demo-bot") || "",
    teamId: sessionStorage.getItem("mmag-demo-team-id") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const bot = config.bot.replace(/^@/, "");
  let team = await page.evaluate(() => {
    const candidate = location.pathname.split("/").filter(Boolean)[0] || "";
    return ["landing", "login", "signup"].includes(candidate) ? "" : candidate;
  });
  const teamId = config.teamId;
  if (teamId) {
    const response = await page.context().request.get(`${config.url.replace(/\/$/, "")}/api/v4/teams/${teamId}`);
    if (response.ok()) team = (await response.json()).name || team;
  }
  if (!team) {
    const response = await page.context().request.get(`${config.url.replace(/\/$/, "")}/api/v4/users/me/teams`);
    if (response.ok()) team = (await response.json())[0]?.name || "";
  }
  if (!team) throw new Error("Could not resolve the Mattermost team name");
  await page.goto(`${config.url.replace(/\/$/, "")}/${team}/messages/@${bot}`);
  await page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").last().waitFor({state: "visible", timeout: 30000});
}'

# shellcheck disable=SC2016
SEND_JS='async page => {
  const threadRegion = page.locator("[role=region][aria-label^=\"Thread \"]");
  if (await threadRegion.count() && await threadRegion.first().isVisible()) {
    const close = threadRegion.first().getByRole("button", {name: "Close"});
    if (await close.count() && await close.isVisible()) {
      await close.click();
      await threadRegion.first().waitFor({state: "hidden", timeout: 10000});
    }
  }
  const config = await page.evaluate(() => ({
    ack: sessionStorage.getItem("mmag-demo-ack") || "",
    message: sessionStorage.getItem("mmag-demo-message") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const ack = config.ack;
  const composer = page.locator("[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]").first();
  await composer.waitFor({state: "visible", timeout: 30000});
  await composer.fill(config.message);
  const postResponsePromise = page.waitForResponse(
    response => response.request().method() === "POST" && /\/api\/v4\/posts(?:\?|$)/.test(response.url()),
    {timeout: 15000}
  );
  await composer.press("Enter");
  const postResponse = await postResponsePromise;
  if (!postResponse.ok()) throw new Error(`Mattermost post creation failed: ${postResponse.status()}`);
  const posted = await postResponse.json();
  const rootPostId = String(posted.id || "");
  if (!rootPostId) throw new Error("Mattermost did not return a post ID");
  if (posted.root_id) throw new Error("A new demo request was posted inside an existing Thread");
  const base = config.url.replace(/\/$/, "");
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const response = await page.context().request.get(`${base}/api/v4/posts/${rootPostId}/thread`);
    if (!response.ok()) throw new Error(`Mattermost thread lookup failed: ${response.status()}`);
    const thread = await response.json();
    const acknowledged = Object.values(thread.posts || {}).some(
      post => post.id !== rootPostId && String(post.message || "").trim() === ack
    );
    if (acknowledged) {
      const rootPost = page.locator(`#post_${rootPostId}`);
      await rootPost.waitFor({state: "visible", timeout: 15000});
      await rootPost.scrollIntoViewIfNeeded();
      const replyButton = rootPost.getByRole("button", {name: /\d+ repl(?:y|ies)/}).first();
      await replyButton.waitFor({state: "visible", timeout: 15000});
      await replyButton.click();
      await page.getByRole("region", {name: /^Thread /}).waitFor({state: "visible", timeout: 10000});
      return rootPostId;
    }
    await page.waitForTimeout(250);
  }
  throw new Error(`Timed out waiting for the default acknowledgment: ${ack}`);
}'

# shellcheck disable=SC2016
WAIT_NEXT_JS='async page => {
  const config = await page.evaluate(() => ({
    marker: sessionStorage.getItem("mmag-demo-marker") || "",
    rootPostId: sessionStorage.getItem("mmag-demo-root-post-id") || "",
    seenApprovals: sessionStorage.getItem("mmag-demo-seen-approvals") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const marker = config.marker;
  const rootPostId = config.rootPostId;
  if (!rootPostId) throw new Error(`Request post ID was not captured for ${marker}`);
  const seen = new Set(config.seenApprovals.split(",").filter(Boolean));
  const pattern = /批准\s+`?([A-Za-z0-9_-]{8,128})`?/g;
  const base = config.url.replace(/\/$/, "");
  const threadRegion = page.getByRole("region", {name: /^Thread /});
  await threadRegion.waitFor({state: "visible", timeout: 10000});
  const deadline = Date.now() + 300000;
  while (Date.now() < deadline) {
    const response = await page.context().request.get(`${base}/api/v4/posts/${rootPostId}/thread`);
    if (!response.ok()) {
      throw new Error(`Mattermost thread lookup failed: ${response.status()}`);
    }
    const thread = await response.json();
    const posts = Object.values(thread.posts || {}).filter(post => post.id !== rootPostId);
    const ids = posts.flatMap(post => [...String(post.message || "").matchAll(pattern)].map(item => item[1]));
    const next = ids.find(id => !seen.has(id));
    if (next) {
      await threadRegion.getByRole("textbox", {name: "Reply to this thread..."}).scrollIntoViewIfNeeded();
      await page.waitForTimeout(750);
      return `approval|${next}`;
    }
    const terminal = posts.find(post => ["result", "error"].includes(post.props?.mmag_kind));
    if (terminal) {
      await threadRegion.getByRole("textbox", {name: "Reply to this thread..."}).scrollIntoViewIfNeeded();
      await page.waitForTimeout(750);
      return terminal.props.mmag_kind === "result" ? "final|" : "error|";
    }
    await page.waitForTimeout(500);
  }
  throw new Error(`Timed out waiting for a terminal post or approval for ${marker}`);
}'

# shellcheck disable=SC2016
APPROVE_JS='async page => {
  const config = await page.evaluate(() => ({
    approval: sessionStorage.getItem("mmag-demo-approval-id") || "",
    rootPostId: sessionStorage.getItem("mmag-demo-root-post-id") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const approval = config.approval;
  const rootPostId = config.rootPostId;
  if (!approval) throw new Error("No approval ID was captured");
  if (!rootPostId) throw new Error("No request post ID was captured");
  const threadRegion = page.getByRole("region", {name: /^Thread /});
  await threadRegion.waitFor({state: "visible", timeout: 10000});
  const composer = threadRegion.getByRole("textbox", {name: "Reply to this thread..."});
  await composer.waitFor({state: "visible", timeout: 30000});
  const message = `批准 ${approval}`;
  await composer.fill(message);
  await page.waitForTimeout(1000);
  const postResponsePromise = page.waitForResponse(
    response => response.request().method() === "POST" && /\/api\/v4\/posts(?:\?|$)/.test(response.url()),
    {timeout: 15000}
  );
  await composer.press("Enter");
  const postResponse = await postResponsePromise;
  if (!postResponse.ok()) throw new Error(`Mattermost approval post failed: ${postResponse.status()}`);
  await page.getByText(message, {exact: true}).last().waitFor({state: "visible", timeout: 15000});
  await page.waitForTimeout(2500);
}'

# shellcheck disable=SC2016
HOLD_JS='async page => {
  const config = await page.evaluate(() => ({
    rootPostId: sessionStorage.getItem("mmag-demo-root-post-id") || "",
  }));
  let threadRegion = page.getByRole("region", {name: /^Thread /});
  if (!await threadRegion.isVisible()) {
    const rootPost = page.locator(`#post_${config.rootPostId}`);
    await rootPost.waitFor({state: "visible", timeout: 15000});
    await rootPost.scrollIntoViewIfNeeded();
    const replyButton = rootPost.getByRole("button", {name: /\d+ repl(?:y|ies)/}).first();
    await replyButton.click();
    threadRegion = page.getByRole("region", {name: /^Thread /});
    await threadRegion.waitFor({state: "visible", timeout: 10000});
    }
  const composer = threadRegion.getByRole("textbox", {name: "Reply to this thread..."});
  await composer.waitFor({state: "visible", timeout: 30000});
  await composer.scrollIntoViewIfNeeded();
  const holdMs = await page.evaluate(() => Number(sessionStorage.getItem("mmag-demo-hold-ms") || 5000));
  await page.waitForTimeout(holdMs);
}'

# Verify actual Mattermost file attachments through the authenticated browser context.
# Textual Artifact refs in the Agent response do not satisfy this check.
# shellcheck disable=SC2016
WAIT_PPT_FILES_JS='async page => {
  const config = await page.evaluate(() => ({
    rootPostId: sessionStorage.getItem("mmag-demo-root-post-id") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const rootPostId = config.rootPostId;
  if (!rootPostId) throw new Error("PPT request post ID was not captured");
  const expected = ["preview.png", "deck.pptx"];
  const base = config.url.replace(/\/$/, "");
  const deadline = Date.now() + 300000;
  let terminalSeenAt = 0;
  while (Date.now() < deadline) {
    const threadResponse = await page.context().request.get(`${base}/api/v4/posts/${rootPostId}/thread`);
    if (!threadResponse.ok()) {
      throw new Error(`Mattermost thread lookup failed: ${threadResponse.status()}`);
    }
    const thread = await threadResponse.json();
    const fileIds = [...new Set(
      Object.values(thread.posts || {}).flatMap(post => Array.isArray(post.file_ids) ? post.file_ids : [])
    )];
    const files = {};
    for (const fileId of fileIds) {
      const infoResponse = await page.context().request.get(`${base}/api/v4/files/${fileId}/info`);
      if (!infoResponse.ok()) continue;
      const info = await infoResponse.json();
      if (expected.includes(info.name)) files[info.name] = fileId;
    }
    if (expected.every(name => files[name])) {
      const threadRegion = page.getByRole("region", {name: /^Thread /});
      await threadRegion.waitFor({state: "visible", timeout: 10000});
      await threadRegion.getByRole("textbox", {name: "Reply to this thread..."}).scrollIntoViewIfNeeded();
      return `${files["preview.png"]}|${files["deck.pptx"]}`;
    }
    const terminal = Object.values(thread.posts || {}).find(
      post => ["result", "error"].includes(post.props?.mmag_kind)
    );
    if (terminal?.props?.mmag_kind === "error") {
      throw new Error("PPT Agent returned an error before delivering attachments");
    }
    if (terminal) {
      terminalSeenAt ||= Date.now();
      if (Date.now() - terminalSeenAt >= 15000) {
        throw new Error(
          `PPT Agent completed without required attachments: ${expected.filter(name => !files[name]).join(", ")}`
        );
      }
    }
    await page.waitForTimeout(750);
  }
  throw new Error(`Timed out waiting for actual Mattermost attachments: ${expected.join(", ")}`);
}'

# shellcheck disable=SC2016
SHOW_PPT_PREVIEW_JS='async page => {
  const files = await page.evaluate(() => ({
    previewId: sessionStorage.getItem("mmag-demo-preview-file-id") || "",
    pptxId: sessionStorage.getItem("mmag-demo-pptx-file-id") || "",
    rootPostId: sessionStorage.getItem("mmag-demo-root-post-id") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const previewId = files.previewId;
  const pptxId = files.pptxId;
  if (!previewId || !pptxId) {
    throw new Error("Verified PPT delivery state is incomplete");
  }
  if (!files.rootPostId) throw new Error("PPT request post ID is missing");
  const threadRegion = page.getByRole("region", {name: /^Thread /});
  await threadRegion.waitFor({state: "visible", timeout: 10000});
  const thumbnail = threadRegion.locator(`img[src*="${previewId}"]`).last();
  await thumbnail.waitFor({state: "visible", timeout: 30000});
  await thumbnail.scrollIntoViewIfNeeded();
  await thumbnail.click();
  await page.waitForFunction(
    id => [...document.images].some(image => {
      const rect = image.getBoundingClientRect();
      return image.src.includes(id) && rect.width >= 800 && rect.height >= 400;
    }),
    previewId,
    {timeout: 15000}
  );
  await page.waitForTimeout(7000);
  await page.keyboard.press("Escape");
  await threadRegion.getByRole("textbox", {name: "Reply to this thread..."}).scrollIntoViewIfNeeded();
  await page.waitForTimeout(2500);
}'

pw open "$MM_URL" >/dev/null
LOGIN_OUTPUT="$(pw run-code "$LOGIN_JS")"
if printf '%s\n' "$LOGIN_OUTPUT" | rg -q '^### Error'; then
  printf 'Mattermost browser login failed.\n' >&2
  exit 1
fi
pw resize "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" >/dev/null
pw sessionstorage-set mmag-demo-url "$MM_URL" >/dev/null
pw sessionstorage-set mmag-demo-bot "$MM_RECORD_BOT" >/dev/null
pw sessionstorage-set mmag-demo-team-id "${MM_TEAM_ID:-}" >/dev/null
pw run-code "$OPEN_DM_JS" >/dev/null
pw state-save "$AUTH_STATE_FILE" >/dev/null

declare -a CLIP_PATHS=()
declare -a CLIP_SPEEDS=()
declare -a CLIP_LABELS=()
CLIP_INDEX=0
REQUEST_ROOT_ID=""
WAIT_RESULT=""
MMAG_DEMO_SEEN_APPROVALS=""
export MMAG_DEMO_SEEN_APPROVALS

result_value() {
  awk '/^### Result/{getline; gsub(/\r/, ""); gsub(/^"|"$/, ""); print; exit}'
}

start_clip() {
  pw resize "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" >/dev/null
  pw run-code "async page => { await page.video().start({size: {width: $VIEWPORT_WIDTH, height: $VIEWPORT_HEIGHT}}); }" >/dev/null
  if [[ -n "$REQUEST_ROOT_ID" ]]; then
    pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  fi
}

stop_clip() {
  local name="$1"
  local speed="$2"
  local label="$3"
  CLIP_INDEX=$((CLIP_INDEX + 1))
  local path
  path="$RAW_DIR/$(printf '%02d' "$CLIP_INDEX")-$name.webm"
  local path_json
  path_json="$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$path")"
  pw run-code "async page => { await page.video().stop({path: $path_json}); }" >/dev/null
  CLIP_PATHS+=("$path")
  CLIP_SPEEDS+=("$speed")
  CLIP_LABELS+=("$label")
}

send_clip() {
  local name="$1"
  local label="$2"
  local output
  export MMAG_DEMO_MESSAGE="$3"
  export MMAG_DEMO_REQUEST_MARKER="${4:-}"
  export MMAG_DEMO_HOLD_MS="${5:-2000}"
  start_clip
  pw sessionstorage-set mmag-demo-message "$MMAG_DEMO_MESSAGE" >/dev/null
  pw sessionstorage-set mmag-demo-ack "$MM_RECORD_ACK" >/dev/null
  pw sessionstorage-set mmag-demo-hold-ms "$MMAG_DEMO_HOLD_MS" >/dev/null
  output="$(pw run-code "$SEND_JS")"
  REQUEST_ROOT_ID="$(printf '%s\n' "$output" | result_value)"
  if [[ -z "$REQUEST_ROOT_ID" || "$REQUEST_ROOT_ID" == "null" ]]; then
    printf 'Could not capture the Mattermost request post ID.\n' >&2
    printf '%s\n' "$output" >&2
    exit 1
  fi
  MMAG_DEMO_SEEN_APPROVALS=""
  pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  pw run-code "$HOLD_JS" >/dev/null
  stop_clip "$name" 1 "$label"
}

wait_clip() {
  local name="$1"
  local speed="$2"
  local label="$3"
  local output
  export MMAG_DEMO_MARKER="$4"
  export MMAG_DEMO_TIMEOUT_MS=300000
  start_clip
  pw sessionstorage-set mmag-demo-url "$MM_URL" >/dev/null
  pw sessionstorage-set mmag-demo-marker "$MMAG_DEMO_MARKER" >/dev/null
  pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  pw sessionstorage-set mmag-demo-seen-approvals "$MMAG_DEMO_SEEN_APPROVALS" >/dev/null
  output="$(pw run-code "$WAIT_NEXT_JS")"
  WAIT_RESULT="$(printf '%s\n' "$output" | result_value)"
  if [[ -z "$WAIT_RESULT" || "$WAIT_RESULT" == "null" ]]; then
    printf 'Could not capture the browser recording state.\n' >&2
    exit 1
  fi
  stop_clip "$name" "$speed" "$label"
}

approve_clip() {
  local output
  export MMAG_DEMO_APPROVAL_ID="$3"
  start_clip
  pw sessionstorage-set mmag-demo-url "$MM_URL" >/dev/null
  pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  pw sessionstorage-set mmag-demo-approval-id "$MMAG_DEMO_APPROVAL_ID" >/dev/null
  output="$(pw run-code "$APPROVE_JS")"
  if printf '%s\n' "$output" | rg -q '^### Error'; then
    printf 'Mattermost approval submission failed.\n' >&2
    printf '%s\n' "$output" | sed -n '/^### Error/,/^### Ran Playwright code/p' | sed '$d' >&2
    exit 1
  fi
  stop_clip "$1" 1 "$2"
}

finish_with_approvals() {
  local prefix="$1"
  local label="$2"
  local marker="$3"
  local max_approvals="$4"
  local stop_after_approval="${5:-0}"
  local count=0
  while true; do
    wait_clip "$prefix-wait-$count" 5 "$label" "$marker"
    local state approval_id
    IFS='|' read -r state approval_id <<<"$WAIT_RESULT"
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
    if [[ -z "$approval_id" ]]; then
      printf 'Approval state did not include an approval ID.\n' >&2
      exit 1
    fi
    MMAG_DEMO_SEEN_APPROVALS="${MMAG_DEMO_SEEN_APPROVALS:+$MMAG_DEMO_SEEN_APPROVALS,}$approval_id"
    approve_clip "$prefix-approve-$count" "人工审批" "$approval_id"
    if ((stop_after_approval)); then
      break
    fi
  done
}

MMCHAT_MARKER="DEMO-MMCHAT-02-$DEMO_RUN_ID"
send_clip \
  "default-get" \
  "默认 get：请求已进入处理" \
  "@${MM_RECORD_BOT#@} 请从 Agent、Skill、Capability、Policy 四个层次说明 MMAG 如何防止模型自行扩大权限，并给出一个审批示例。请保留编号 $MMCHAT_MARKER。" \
  "$MMCHAT_MARKER"
finish_with_approvals "mmchat" "真实 AI 生成等待" "$MMCHAT_MARKER" 0
export MMAG_DEMO_HOLD_MS=7000
start_clip
pw sessionstorage-set mmag-demo-hold-ms "$MMAG_DEMO_HOLD_MS" >/dev/null
pw run-code "$HOLD_JS" >/dev/null
stop_clip "mmchat-result" 1 "检查真实回复内容"

PROJECT_MARKER="DEMO-PROJECT-03-$DEMO_RUN_ID"
send_clip \
  "project-request" \
  "Project Agent 与 Skill" \
  "项目助理：确认‘所有通过入口验收的用户消息默认回复 get’为当前项目决策，并保存到知识库。请保留编号 $PROJECT_MARKER。" \
  "$PROJECT_MARKER"
# The following retrieval request is the deterministic completion evidence for this write.
# It also keeps the recording independent of a slow, optional post-write model summary.
finish_with_approvals "project" "知识写入等待" "$PROJECT_MARKER" 2 1

if ((SKIP_PPT == 0)); then
  PROJECT_VERIFY_MARKER="DEMO-PROJECT-VERIFY-03-$DEMO_RUN_ID"
  send_clip \
    "project-verify-request" \
    "验证知识真正写入" \
    "项目助理：查询刚才确认的默认 ACK 决策，并保留编号 $PROJECT_VERIFY_MARKER。" \
    "$PROJECT_VERIFY_MARKER"
  finish_with_approvals "project-verify" "知识读取等待" "$PROJECT_VERIFY_MARKER" 0
  export MMAG_DEMO_HOLD_MS=7000
  start_clip
  pw sessionstorage-set mmag-demo-hold-ms "$MMAG_DEMO_HOLD_MS" >/dev/null
  pw run-code "$HOLD_JS" >/dev/null
  stop_clip "project-result" 1 "Project 决策回读成功"
fi

if ((SKIP_PPT == 0)); then
  PPT_MARKER="DEMO-PPT-04-$DEMO_RUN_ID"
  send_clip \
    "ppt-request" \
    "真实 PPT Agent" \
    "PPT 助理：生成一份三页的《MMAG 企业智能体能力概览》。第 1 页是 Agent、Skill、Capability 架构；第 2 页是 Policy、审批与 Sandbox 安全边界；第 3 页是 Mattermost 交互与评估闭环。使用简洁企业风格。请将 preview.png 和 deck.pptx 作为附件发给我，并保留编号 $PPT_MARKER。" \
    "$PPT_MARKER"
  finish_with_approvals "ppt" "PPT 生成与上传等待" "$PPT_MARKER" 4
  export MMAG_DEMO_TIMEOUT_MS=300000
  start_clip
  pw sessionstorage-set mmag-demo-url "$MM_URL" >/dev/null
  pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  PPT_FILES_OUTPUT="$(pw run-code "$WAIT_PPT_FILES_JS")"
  PPT_FILES_RESULT="$(printf '%s\n' "$PPT_FILES_OUTPUT" | result_value)"
  IFS='|' read -r MMAG_DEMO_PREVIEW_FILE_ID MMAG_DEMO_PPTX_FILE_ID <<<"$PPT_FILES_RESULT"
  if [[ -z "$MMAG_DEMO_PREVIEW_FILE_ID" || -z "$MMAG_DEMO_PPTX_FILE_ID" ]]; then
    printf 'Verified PPT attachments did not return both file IDs.\n' >&2
    exit 1
  fi
  stop_clip "ppt-files-wait" 5 "核验真实预览图与 PPTX 附件"

  start_clip
  pw sessionstorage-set mmag-demo-preview-file-id "$MMAG_DEMO_PREVIEW_FILE_ID" >/dev/null
  pw sessionstorage-set mmag-demo-pptx-file-id "$MMAG_DEMO_PPTX_FILE_ID" >/dev/null
  pw sessionstorage-set mmag-demo-root-post-id "$REQUEST_ROOT_ID" >/dev/null
  pw run-code "$SHOW_PPT_PREVIEW_JS" >/dev/null
  stop_clip "ppt-preview" 1 "Mattermost 原生 PPT 预览"

  export MMAG_DEMO_HOLD_MS=5000
  start_clip
  pw sessionstorage-set mmag-demo-hold-ms "$MMAG_DEMO_HOLD_MS" >/dev/null
  pw run-code "$HOLD_JS" >/dev/null
  stop_clip "final-evidence" 1 "preview.png 与 deck.pptx 交付证据"
fi

TITLE_FONT='Sarasa Gothic SC SemiBold'
CAPTION_FONT='Sarasa UI SC SemiBold'

require_font_face() {
  local query="$1"
  local expected_family="$2"
  local expected_style="$3"
  local match family style file
  match="$(fc-match -f '%{family[0]}|%{style[0]}|%{file}\n' "$query" | head -1)"
  IFS='|' read -r family style file <<<"$match"
  if [[ "$family" != "$expected_family" || "$style" != "$expected_style" || ! -f "$file" ]]; then
    printf 'Required video font is unavailable: %s (%s %s); resolved to: %s\n' \
      "$query" "$expected_family" "$expected_style" "${match:-no match}" >&2
    exit 1
  fi
  printf 'video_font=%s|%s|%s\n' "$family" "$style" "$file"
}

require_font_face "$TITLE_FONT" 'Sarasa Gothic SC' 'SemiBold'
require_font_face "$CAPTION_FONT" 'Sarasa UI SC' 'SemiBold'

TITLE_MP4="$EDIT_DIR/00-title.mp4"
END_MP4="$EDIT_DIR/99-end.mp4"
ffmpeg -y -v error -f lavfi -i color=c=0x111827:s=${VIDEO_WIDTH}x${VIDEO_HEIGHT}:d=5:r=25 \
  -vf "drawtext=font='$TITLE_FONT':text='MMAG 企业智能体真实演示':fontcolor=white:fontsize=84:x=(w-text_w)/2:y=575,drawtext=font='$CAPTION_FONT':text='Mattermost · Agent · Skill · Approval · PPT Artifact':fontcolor=0x93c5fd:fontsize=46:x=(w-text_w)/2:y=695" \
  -an -c:v libx264 -pix_fmt yuv420p "$TITLE_MP4"

NORMALIZED=("$TITLE_MP4")
for index in "${!CLIP_PATHS[@]}"; do
  input="${CLIP_PATHS[$index]}"
  speed="${CLIP_SPEEDS[$index]}"
  label="${CLIP_LABELS[$index]}"
  output="$EDIT_DIR/$(printf '%02d' "$((index + 1))").mp4"
  ffmpeg -y -v error -i "$input" \
    -vf "setpts=PTS/$speed,fps=25,scale=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,pad=${VIDEO_WIDTH}:${VIDEO_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=0x111827,drawtext=font='$TITLE_FONT':text='$label':fontcolor=white:fontsize=42:box=1:boxcolor=black@0.65:boxborderw=22:x=48:y=48" \
    -an -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p "$output"
  NORMALIZED+=("$output")
done

if ((SKIP_PPT)); then
  END_SUMMARY='默认 get · 真实 Agent · 人工审批 · 知识写入'
  FINAL_NAME='mmag-real-agent-demo-no-ppt-2k.mp4'
else
  END_SUMMARY='默认 get · 真实 Agent · 人工审批 · PPT 预览与附件'
  FINAL_NAME='mmag-real-e2e-demo-2k.mp4'
fi
ffmpeg -y -v error -f lavfi -i color=c=0x111827:s=${VIDEO_WIDTH}x${VIDEO_HEIGHT}:d=5:r=25 \
  -vf "drawtext=font='$TITLE_FONT':text='真实流程完成':fontcolor=white:fontsize=88:x=(w-text_w)/2:y=585,drawtext=font='$CAPTION_FONT':text='$END_SUMMARY':fontcolor=0x86efac:fontsize=46:x=(w-text_w)/2:y=710" \
  -an -c:v libx264 -pix_fmt yuv420p "$END_MP4"
NORMALIZED+=("$END_MP4")

CONCAT_FILE="$EDIT_DIR/concat.txt"
: >"$CONCAT_FILE"
for path in "${NORMALIZED[@]}"; do
  printf "file '%s'\n" "$path" >>"$CONCAT_FILE"
done

FINAL_VIDEO="$RUN_DIR/$FINAL_NAME"
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
