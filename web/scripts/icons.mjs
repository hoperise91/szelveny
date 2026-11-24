// PNG icons from public/favicon.svg (iOS home screen, iMessage and other link previews
// don't use SVG icons). Run from web/: node scripts/icons.mjs
import sharp from "sharp";
import { readFileSync } from "node:fs";

const svg = readFileSync("public/favicon.svg");
for (const [file, size] of [["apple-touch-icon.png", 180], ["favicon-32.png", 32]]) {
  const pad = size <= 32 ? 0 : Math.round(size * 0.14);
  const mark = await sharp(svg, { density: 72 * (size / 30) * 2 })
    .resize({ height: size - 2 * pad, width: size - 2 * pad, fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } })
    .toBuffer();
  const bg = size <= 32 ? { r: 0, g: 0, b: 0, alpha: 0 } : "#f6f3ec";
  await sharp({ create: { width: size, height: size, channels: 4, background: bg } })
    .composite([{ input: mark, gravity: "center" }]).png().toFile(`public/${file}`);
}
