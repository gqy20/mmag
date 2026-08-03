async page => {
  const config = await page.evaluate(() => ({
    op: sessionStorage.getItem("mmag-demo-browser-op") || "",
    channel: sessionStorage.getItem("mmag-demo-group") || "",
    message: sessionStorage.getItem("mmag-demo-message") || "",
    url: sessionStorage.getItem("mmag-demo-url") || "",
  }));
  const base = config.url.replace(/\/$/, "");
  if (config.op === "open-group") {
    const teams = await page.context().request.get(`${base}/api/v4/users/me/teams`);
    const team = teams.ok() ? (await teams.json())[0]?.name || "" : "";
    if (!team || !config.channel) throw new Error("Could not resolve the demo group channel");
    await page.goto(`${base}/${team}/channels/${config.channel}`);
    await page.locator(
      "[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]"
    ).last().waitFor({state: "visible", timeout: 30000});
    return true;
  }
  const composer = page.locator(
    "[data-testid=post_textbox], #post_textbox, [contenteditable=true][role=textbox]"
  ).first();
  if (config.op === "send-slash") {
    await composer.fill(config.message);
    const responsePromise = page.waitForResponse(
      response => response.request().method() === "POST" && /\/api\/v4\/commands\/execute(?:\?|$)/.test(response.url()),
      {timeout: 15000}
    );
    await composer.press("Enter");
    const response = await responsePromise;
    if (!response.ok()) throw new Error(`Mattermost slash command failed: ${response.status()}`);
    await page.waitForTimeout(6000);
    return true;
  }
  if (config.op === "click-action") {
    const label = config.message;
    const thread = page.getByRole("region", {name: /^Thread /});
    await thread.waitFor({state: "visible", timeout: 10000});
    const button = thread.getByRole("button", {name: label, exact: true}).last();
    await button.waitFor({state: "visible", timeout: 30000});
    await button.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1500);
    await button.click();
    await page.waitForTimeout(5000);
    return true;
  }
  if (config.op !== "send-view") throw new Error(`Unsupported browser operation: ${config.op}`);
  await composer.fill(config.message);
  const responsePromise = page.waitForResponse(
    response => response.request().method() === "POST" && /\/api\/v4\/posts(?:\?|$)/.test(response.url()),
    {timeout: 15000}
  );
  await composer.press("Enter");
  const posted = await (await responsePromise).json();
  const rootPostId = String(posted.id || "");
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const response = await page.context().request.get(`${base}/api/v4/posts/${rootPostId}/thread`);
    const thread = response.ok() ? await response.json() : {posts: {}};
    if (Object.values(thread.posts || {}).some(post => post.id !== rootPostId)) {
      const root = page.locator(`#post_${rootPostId}`);
      await root.scrollIntoViewIfNeeded();
      await root.getByRole("button", {name: /\d+ repl(?:y|ies)/}).first().click();
      await page.getByRole("region", {name: /^Thread /}).waitFor({state: "visible", timeout: 10000});
      return rootPostId;
    }
    await page.waitForTimeout(250);
  }
  throw new Error("Timed out waiting for workspace response");
}
