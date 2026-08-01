"use strict";

const fs = require("node:fs");
const PptxGenJS = require("pptxgenjs");

const MAX_SOURCE_BYTES = 262144;
const MAX_SLIDES = 40;
const LAYOUTS = new Set([
  "cover",
  "section",
  "statement",
  "metric",
  "bullets",
  "split",
  "comparison",
  "timeline",
  "architecture",
  "end",
]);

function fail(message) {
  throw new Error(message);
}

function readPayload(path) {
  const payload = JSON.parse(fs.readFileSync(path, "utf8"));
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    fail("input must be a JSON object");
  }
  if (typeof payload.source !== "string" || !payload.source.trim()) {
    fail("source must be non-empty Markdown");
  }
  if (Buffer.byteLength(payload.source, "utf8") > MAX_SOURCE_BYTES) {
    fail("source exceeds its byte limit");
  }
  if (!payload.theme || typeof payload.theme !== "object") {
    fail("a registered theme is required");
  }
  if (payload.theme_ref !== "corp@1.0.0") {
    fail("unknown theme_ref");
  }
  return payload;
}

function unquote(value) {
  const trimmed = value.trim();
  if (
    trimmed.length >= 2 &&
    ((trimmed.startsWith('"') && trimmed.endsWith('"')) ||
      (trimmed.startsWith("'") && trimmed.endsWith("'")))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function frontMatter(source) {
  const match = source.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
  if (!match) fail("slides Markdown must start with front matter");
  const metadata = {};
  for (const raw of match[1].split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf(":");
    if (separator < 1) fail(`invalid front matter line: ${line}`);
    const key = line.slice(0, separator).trim();
    const value = unquote(line.slice(separator + 1));
    if (!/^[a-z][a-z0-9_]*$/.test(key) || !value) fail("invalid front matter entry");
    metadata[key] = value;
  }
  return { metadata, body: source.slice(match[0].length) };
}

function parseSlide(raw, index) {
  const layoutMatch = raw.match(/<!--\s*layout:\s*([a-z-]+)\s*-->/i);
  const layout = (layoutMatch?.[1] || "bullets").toLowerCase();
  if (!LAYOUTS.has(layout)) fail(`slide ${index} uses unsupported layout ${layout}`);
  if (/<\s*(?:style|script|iframe|object|embed)\b/i.test(raw) || /\{\{/.test(raw)) {
    fail(`slide ${index} contains executable or unrestricted markup`);
  }
  if (/!\[[^\]]*\]\(/.test(raw)) {
    fail(`slide ${index} must use governed asset refs instead of Markdown images`);
  }
  const notesMatch = raw.match(/<!--\s*notes:\s*([\s\S]*?)-->/i);
  const sourcesMatch = raw.match(/<!--\s*sources:\s*([\s\S]*?)-->/i);
  const clean = raw
    .replace(/<!--\s*layout:[\s\S]*?-->/gi, "")
    .replace(/<!--\s*notes:[\s\S]*?-->/gi, "")
    .replace(/<!--\s*sources:[\s\S]*?-->/gi, "")
    .trim();
  const lines = clean.split(/\r?\n/);
  const titleLine = lines.find((line) => /^#\s+/.test(line.trim()));
  if (!titleLine) fail(`slide ${index} needs one level-one heading`);
  const title = titleLine.trim().replace(/^#\s+/, "").trim();
  if (!title || title.length > 180) fail(`slide ${index} has an invalid title`);
  const sections = [];
  let current = { heading: "", bullets: [], text: [] };
  sections.push(current);
  let quote = "";
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line || line === titleLine.trim()) continue;
    if (/^##\s+/.test(line)) {
      current = { heading: line.replace(/^##\s+/, "").trim(), bullets: [], text: [] };
      sections.push(current);
    } else if (/^-\s+/.test(line)) {
      current.bullets.push(line.replace(/^-\s+/, "").trim());
    } else if (/^>\s+/.test(line)) {
      quote = line.replace(/^>\s+/, "").trim();
    } else if (!line.startsWith("<!--")) {
      current.text.push(line);
    }
  }
  return {
    number: index,
    layout,
    title,
    quote,
    sections: sections.filter(
      (section) => section.heading || section.bullets.length || section.text.length,
    ),
    notes: notesMatch?.[1]?.trim() || "",
    sources: (sourcesMatch?.[1] || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  };
}

function parseDeck(source, expectedTheme) {
  const normalized = source.replace(/\r\n/g, "\n").trim() + "\n";
  const { metadata, body } = frontMatter(normalized);
  for (const field of ["title", "audience", "objective", "narrative", "theme_ref"]) {
    if (!metadata[field]) fail(`front matter is missing ${field}`);
  }
  if (metadata.theme_ref !== expectedTheme) fail("front matter theme_ref does not match request");
  const blocks = body
    .split(/\n---\s*\n/g)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!blocks.length || blocks.length > MAX_SLIDES) {
    fail(`deck must contain between 1 and ${MAX_SLIDES} slides`);
  }
  return {
    metadata,
    slides: blocks.map((block, index) => parseSlide(block, index + 1)),
    source: normalized,
  };
}

function color(theme, name) {
  const value = theme.colors?.[name];
  if (typeof value !== "string" || !/^[0-9A-Fa-f]{6}$/.test(value)) {
    fail(`theme color ${name} is invalid`);
  }
  return value.toUpperCase();
}

function font(theme, name) {
  const value = theme.fonts?.[name];
  if (typeof value !== "string" || !value.trim()) fail(`theme font ${name} is invalid`);
  return value.trim();
}

function addText(slide, text, options) {
  slide.addText(Array.isArray(text) ? text : String(text || ""), {
    margin: 0,
    breakLine: false,
    fit: "shrink",
    valign: "mid",
    ...options,
  });
}

function addChrome(slide, deck, theme, number, dark = false) {
  const ink = color(theme, dark ? "paper" : "muted");
  addText(slide, deck.metadata.title.toUpperCase(), {
    x: 0.62,
    y: 0.25,
    w: 5.6,
    h: 0.22,
    fontFace: font(theme, "body"),
    fontSize: 5.8,
    bold: true,
    charSpacing: 2.2,
    color: ink,
  });
  addText(slide, String(number).padStart(2, "0"), {
    x: 12.08,
    y: 7.03,
    w: 0.6,
    h: 0.18,
    fontFace: font(theme, "mono"),
    fontSize: 5.5,
    color: ink,
    align: "right",
  });
}

function addTitle(slide, item, theme, dark = false) {
  const ink = color(theme, dark ? "paper" : "ink");
  addText(slide, item.title, {
    x: 0.62,
    y: 0.72,
    w: 11.9,
    h: 0.72,
    fontFace: font(theme, "head"),
    fontSize: item.title.length > 34 ? 22 : 27,
    bold: true,
    color: ink,
  });
  slide.addShape("rect", {
    x: 0.62,
    y: 1.52,
    w: 0.62,
    h: 0.06,
    line: { color: color(theme, "accent"), transparency: 100 },
    fill: { color: color(theme, "accent") },
  });
}

function bulletRuns(items) {
  return items.map((text) => ({
    text,
    options: { bullet: { indent: 14 }, hanging: 4, breakLine: true, paraSpaceAfterPt: 10 },
  }));
}

function allBullets(item) {
  return item.sections.flatMap((section) => section.bullets.concat(section.text));
}

function addSources(slide, item, theme, dark = false) {
  if (!item.sources.length) return;
  addText(slide, `SOURCES  ${item.sources.join(" · ")}`, {
    x: 0.64,
    y: 6.88,
    w: 10.7,
    h: 0.2,
    fontFace: font(theme, "mono"),
    fontSize: 5.2,
    color: color(theme, dark ? "paper" : "muted"),
    transparency: 18,
  });
}

function renderCover(slide, deck, item, theme) {
  const bg = color(theme, "night");
  slide.background = { color: bg };
  slide.addShape("rect", {
    x: 8.85,
    y: -0.3,
    w: 4.9,
    h: 8.1,
    rotate: 8,
    line: { color: color(theme, "accent"), transparency: 100 },
    fill: { color: color(theme, "accent"), transparency: 5 },
  });
  slide.addShape("line", {
    x: 0.72,
    y: 0.68,
    w: 1.08,
    h: 0,
    line: { color: color(theme, "coral"), width: 2.5 },
  });
  addText(slide, deck.metadata.audience.toUpperCase(), {
    x: 0.72,
    y: 0.86,
    w: 5.5,
    h: 0.3,
    fontFace: font(theme, "mono"),
    fontSize: 7,
    bold: true,
    charSpacing: 2.4,
    color: color(theme, "coral"),
  });
  addText(slide, item.title, {
    x: 0.72,
    y: 1.48,
    w: 8.9,
    h: 2.35,
    fontFace: font(theme, "head"),
    fontSize: item.title.length > 24 ? 35 : 43,
    bold: true,
    color: color(theme, "paper"),
    valign: "top",
    breakLine: true,
  });
  const subtitle = item.quote || item.sections.flatMap((s) => s.text).join(" ");
  addText(slide, subtitle || deck.metadata.objective, {
    x: 0.76,
    y: 4.35,
    w: 7.15,
    h: 0.82,
    fontFace: font(theme, "body"),
    fontSize: 16,
    color: color(theme, "paper"),
    transparency: 16,
    valign: "top",
  });
  addText(slide, deck.metadata.narrative, {
    x: 0.76,
    y: 6.48,
    w: 8.1,
    h: 0.34,
    fontFace: font(theme, "body"),
    fontSize: 8.5,
    color: color(theme, "paper"),
    transparency: 30,
  });
}

function renderStatement(slide, deck, item, theme) {
  slide.background = { color: color(theme, "paper") };
  addChrome(slide, deck, theme, item.number);
  addText(slide, item.title, {
    x: 0.72,
    y: 1.28,
    w: 7.1,
    h: 2.2,
    fontFace: font(theme, "head"),
    fontSize: item.title.length < 9 ? 55 : 38,
    bold: true,
    color: color(theme, "ink"),
    valign: "bottom",
  });
  slide.addShape("rect", {
    x: 8.58,
    y: 0.75,
    w: 3.95,
    h: 5.88,
    rectRadius: 0.08,
    line: { color: color(theme, "accent"), transparency: 100 },
    fill: { color: color(theme, "accent") },
  });
  addText(slide, item.quote || deck.metadata.objective, {
    x: 9.05,
    y: 1.35,
    w: 2.95,
    h: 2.7,
    fontFace: font(theme, "head"),
    fontSize: 22,
    bold: true,
    color: color(theme, "paper"),
    valign: "top",
  });
  const bullets = allBullets(item).slice(0, 4);
  if (bullets.length) {
    addText(slide, bulletRuns(bullets), {
      x: 0.78,
      y: 4.25,
      w: 6.85,
      h: 1.55,
      fontFace: font(theme, "body"),
      fontSize: 14,
      color: color(theme, "ink"),
      valign: "top",
    });
  }
}

function renderColumns(slide, deck, item, theme) {
  slide.background = { color: color(theme, "canvas") };
  addChrome(slide, deck, theme, item.number);
  addTitle(slide, item, theme);
  const sections = item.sections.length ? item.sections.slice(0, 4) : [{ heading: "重点", bullets: allBullets(item), text: [] }];
  const count = Math.min(sections.length, 4);
  const gap = 0.22;
  const width = (12.09 - gap * (count - 1)) / count;
  sections.slice(0, count).forEach((section, index) => {
    const x = 0.62 + index * (width + gap);
    const accent = ["accent", "coral", "green", "amber"][index];
    slide.addShape("roundRect", {
      x,
      y: 1.88,
      w: width,
      h: 4.62,
      rectRadius: 0.08,
      line: { color: color(theme, "line"), width: 0.8 },
      fill: { color: color(theme, "paper") },
    });
    addText(slide, String(index + 1).padStart(2, "0"), {
      x: x + 0.28,
      y: 2.12,
      w: 0.62,
      h: 0.34,
      fontFace: font(theme, "mono"),
      fontSize: 10,
      bold: true,
      color: color(theme, accent),
    });
    addText(slide, section.heading || `模块 ${index + 1}`, {
      x: x + 0.28,
      y: 2.6,
      w: width - 0.56,
      h: 0.72,
      fontFace: font(theme, "head"),
      fontSize: count > 3 ? 16 : 19,
      bold: true,
      color: color(theme, "ink"),
      valign: "top",
    });
    const content = section.bullets.concat(section.text).slice(0, 6);
    addText(slide, bulletRuns(content), {
      x: x + 0.29,
      y: 3.53,
      w: width - 0.58,
      h: 2.45,
      fontFace: font(theme, "body"),
      fontSize: count > 3 ? 10.5 : 12.5,
      color: color(theme, "ink"),
      valign: "top",
    });
  });
  addSources(slide, item, theme);
}

function renderTimeline(slide, deck, item, theme) {
  slide.background = { color: color(theme, "night") };
  addChrome(slide, deck, theme, item.number, true);
  addTitle(slide, item, theme, true);
  const sections = item.sections.slice(0, 5);
  slide.addShape("line", {
    x: 1.08,
    y: 3.26,
    w: 10.95,
    h: 0,
    line: { color: color(theme, "accent"), width: 2, transparency: 20 },
  });
  sections.forEach((section, index) => {
    const x = 0.82 + index * (11.35 / Math.max(1, sections.length - 1));
    slide.addShape("ellipse", {
      x,
      y: 3.05,
      w: 0.42,
      h: 0.42,
      line: { color: color(theme, "paper"), width: 1.5 },
      fill: { color: index === sections.length - 1 ? color(theme, "coral") : color(theme, "accent") },
    });
    addText(slide, section.heading, {
      x: x - 0.35,
      y: index % 2 === 0 ? 2.02 : 3.67,
      w: 1.85,
      h: 0.52,
      fontFace: font(theme, "head"),
      fontSize: 13,
      bold: true,
      color: color(theme, "paper"),
      valign: "top",
    });
    const detail = section.bullets.concat(section.text).slice(0, 2).join("\n");
    addText(slide, detail, {
      x: x - 0.35,
      y: index % 2 === 0 ? 2.48 : 4.18,
      w: 1.85,
      h: 0.82,
      fontFace: font(theme, "body"),
      fontSize: 8.5,
      color: color(theme, "paper"),
      transparency: 20,
      valign: "top",
    });
  });
  addSources(slide, item, theme, true);
}

function renderEnd(slide, deck, item, theme) {
  slide.background = { color: color(theme, "accent") };
  addText(slide, item.title, {
    x: 0.78,
    y: 1.08,
    w: 10.5,
    h: 1.75,
    fontFace: font(theme, "head"),
    fontSize: 43,
    bold: true,
    color: color(theme, "paper"),
    valign: "bottom",
  });
  addText(slide, item.quote || deck.metadata.objective, {
    x: 0.82,
    y: 3.55,
    w: 7.8,
    h: 0.88,
    fontFace: font(theme, "body"),
    fontSize: 17,
    color: color(theme, "paper"),
    transparency: 8,
  });
  addText(slide, "MMAG / PRESENTATION AGENT", {
    x: 0.82,
    y: 6.62,
    w: 4.6,
    h: 0.25,
    fontFace: font(theme, "mono"),
    fontSize: 6.5,
    bold: true,
    charSpacing: 2,
    color: color(theme, "paper"),
    transparency: 22,
  });
}

function renderDeck(deck, theme, output) {
  const pptx = new PptxGenJS();
  pptx.layout = "LAYOUT_WIDE";
  pptx.author = "MMAG Presentation Agent";
  pptx.company = "MMAG";
  pptx.subject = deck.metadata.objective;
  pptx.title = deck.metadata.title;
  pptx.lang = "zh-CN";
  pptx.theme = {
    headFontFace: font(theme, "head"),
    bodyFontFace: font(theme, "body"),
    lang: "zh-CN",
  };
  pptx.defineLayout({ name: "MMAG_WIDE", width: 13.333, height: 7.5 });
  pptx.layout = "MMAG_WIDE";
  deck.slides.forEach((item) => {
    const slide = pptx.addSlide();
    slide.margin = 0;
    if (item.layout === "cover" || item.layout === "section") {
      renderCover(slide, deck, item, theme);
    } else if (item.layout === "statement" || item.layout === "metric") {
      renderStatement(slide, deck, item, theme);
    } else if (item.layout === "timeline") {
      renderTimeline(slide, deck, item, theme);
    } else if (item.layout === "end") {
      renderEnd(slide, deck, item, theme);
    } else {
      renderColumns(slide, deck, item, theme);
    }
    if (item.notes && typeof slide.addNotes === "function") slide.addNotes(item.notes);
  });
  return pptx.writeFile({ fileName: output });
}

function escapeXml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function previewTitleLines(value) {
  const characters = Array.from(value);
  if (characters.length <= 24) return [value];
  const target = Math.ceil(characters.length / 2);
  const candidates = characters
    .map((character, index) => ({ character, index }))
    .filter(
      (item) =>
        item.index >= 10 &&
        item.index <= characters.length - 10 &&
        /[\s：:，,、]/.test(item.character),
    );
  const selected = candidates.sort(
    (left, right) => Math.abs(left.index - target) - Math.abs(right.index - target),
  )[0];
  const splitAt = selected?.index ?? 24;
  const includeSeparator = selected && /[：:，,、]/.test(selected.character) ? 1 : 0;
  const rightStart = selected ? splitAt + 1 : splitAt;
  return [
    characters.slice(0, splitAt + includeSeparator).join("").trim(),
    characters.slice(rightStart).join("").trim(),
  ];
}

function renderPreview(deck, theme, output) {
  const slide = deck.slides[0];
  const dark = slide.layout === "cover" || slide.layout === "section";
  const background = color(theme, dark ? "night" : "canvas");
  const foreground = color(theme, dark ? "paper" : "ink");
  const accent = color(theme, "accent");
  const detail = slide.quote || slide.sections.flatMap((item) => item.bullets.concat(item.text))[0] || deck.metadata.objective;
  const titleLines = previewTitleLines(slide.title);
  const titleSize = titleLines.length > 1 ? 48 : slide.title.length > 18 ? 58 : 68;
  const titleMarkup = titleLines
    .map(
      (line, index) =>
        `<tspan x="68" dy="${index === 0 ? 0 : 66}">${escapeXml(line)}</tspan>`,
    )
    .join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#${background}"/>
  <rect x="68" y="74" width="108" height="8" fill="#${accent}"/>
  <text x="68" y="145" fill="#${foreground}" font-family="${escapeXml(font(theme, "body"))}" font-size="16" font-weight="700" letter-spacing="4">${escapeXml(deck.metadata.audience.toUpperCase())}</text>
  <text x="68" y="248" fill="#${foreground}" font-family="${escapeXml(font(theme, "head"))}" font-size="${titleSize}" font-weight="700">${titleMarkup}</text>
  <text x="72" y="430" fill="#${foreground}" opacity="0.72" font-family="${escapeXml(font(theme, "body"))}" font-size="27">${escapeXml(detail.slice(0, 72))}</text>
  <text x="72" y="655" fill="#${foreground}" opacity="0.42" font-family="${escapeXml(font(theme, "mono"))}" font-size="13" letter-spacing="3">MMAG / PRESENTATION AGENT</text>
  <text x="1192" y="655" fill="#${foreground}" opacity="0.42" font-family="${escapeXml(font(theme, "mono"))}" font-size="13">01</text>
</svg>`;
  fs.writeFileSync(output, svg, { encoding: "utf8", mode: 0o600 });
}

async function main() {
  const [action, inputFlag, inputPath, outputFlag, outputPath] = process.argv.slice(2);
  if (!new Set(["source", "render", "preview"]).has(action)) fail("unsupported action");
  if (inputFlag !== "--input" || outputFlag !== "--output" || !inputPath || !outputPath) {
    fail("expected --input and --output");
  }
  const payload = readPayload(inputPath);
  const deck = parseDeck(payload.source, payload.theme_ref);
  if (action === "source") {
    fs.writeFileSync(outputPath, deck.source, { encoding: "utf8", mode: 0o600 });
  } else if (action === "preview") {
    renderPreview(deck, payload.theme, outputPath);
  } else {
    await renderDeck(deck, payload.theme, outputPath);
  }
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write(`${error?.message || "presentation build failed"}\n`);
    process.exit(1);
  },
);
