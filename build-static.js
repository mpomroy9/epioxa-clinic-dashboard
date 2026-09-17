const fs = require("fs");
const path = require("path");

const outDir = path.join(__dirname, "dist");
const serverDir = path.join(outDir, "server");
const openAiDir = path.join(outDir, ".openai");
const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const generatedDashboard = fs.readFileSync(
  path.join(__dirname, "outputs", "epioxa-dashboard.html"),
  "utf8",
);

if (html !== generatedDashboard) {
  throw new Error("index.html is stale; rebuild or copy outputs/epioxa-dashboard.html first");
}

const payloadMatch = html.match(
  /<script id="dashboard-data" type="application\/json">([\s\S]*?)<\/script>/,
);
if (!payloadMatch) {
  throw new Error("Dashboard data payload is missing from index.html");
}
const dashboardData = JSON.parse(payloadMatch[1]);
const generatedAt = Date.parse(dashboardData.generated_at);
const lastRun = Date.parse(dashboardData.summary?.last_run);
if (!Number.isFinite(generatedAt) || !Number.isFinite(lastRun) || generatedAt < lastRun) {
  throw new Error("Dashboard timestamps are missing or stale");
}

fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(serverDir, { recursive: true });
fs.mkdirSync(openAiDir, { recursive: true });
fs.copyFileSync(path.join(__dirname, "index.html"), path.join(outDir, "index.html"));
fs.copyFileSync(path.join(__dirname, ".openai", "hosting.json"), path.join(openAiDir, "hosting.json"));
fs.writeFileSync(
  path.join(outDir, "build-metadata.json"),
  `${JSON.stringify({ generatedAt: dashboardData.generated_at, lastRun: dashboardData.summary.last_run }, null, 2)}\n`,
);
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
