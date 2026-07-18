const fs = require("fs");
const path = require("path");

const outDir = path.join(__dirname, "dist");
const serverDir = path.join(outDir, "server");
const openAiDir = path.join(outDir, ".openai");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(serverDir, { recursive: true });
fs.mkdirSync(openAiDir, { recursive: true });
fs.copyFileSync(path.join(__dirname, "index.html"), path.join(outDir, "index.html"));
fs.copyFileSync(path.join(__dirname, ".openai", "hosting.json"), path.join(openAiDir, "hosting.json"));
fs.writeFileSync(
  path.join(serverDir, "index.js"),
  `const html = ${JSON.stringify(html)};

export default {
  async fetch() {
    return new Response(html, {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "public, max-age=300"
      }
    });
  }
};
`,
);
