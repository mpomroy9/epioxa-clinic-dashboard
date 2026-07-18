const fs = require("fs");
const path = require("path");

const outDir = path.join(__dirname, "dist");
fs.rmSync(outDir, { recursive: true, force: true });
fs.mkdirSync(outDir, { recursive: true });
fs.copyFileSync(path.join(__dirname, "index.html"), path.join(outDir, "index.html"));
