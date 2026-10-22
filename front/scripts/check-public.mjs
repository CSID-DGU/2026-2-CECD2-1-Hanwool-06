import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { extname } from "node:path";

const publicDir = fileURLToPath(new URL("../public/", import.meta.url));
const allowed = new Set([".png", ".svg", ".ico", ".woff2", ".jpg", ".webp"]);
const unexpected = readdirSync(publicDir, { recursive: true, withFileTypes: true })
  .filter((entry) => entry.isFile() && !allowed.has(extname(entry.name).toLowerCase()));
if (unexpected.length) throw new Error(`보호 데이터는 public에 둘 수 없습니다: ${unexpected.map((entry) => entry.name).join(", ")}`);
