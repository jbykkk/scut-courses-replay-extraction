#!/usr/bin/env node
// Standalone probe for the same range implementation used by the extension.
import { readFile, writeFile, mkdir, access } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { inspectAudio, downloadIndexedAudio } from "../extension/mp4-audio-range.mjs";
export { parseMultipart, createRangeReader, planAudioRanges, inspectAudio, downloadIndexedAudio } from "../extension/mp4-audio-range.mjs";

async function main() {
  const args = process.argv.slice(2);
  if (!args.length || args.includes("--help")) {
    console.log("Usage: node scripts/audio-range.mjs --manifest PATH --sub-id ID [--output FILE.m4a] [--gap-bytes 0] [--batch-size 128] [--concurrency 4]\nWithout --output, only fetch and inspect the MP4 index. No full-download fallback.");
    return;
  }
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    if (!["--manifest", "--sub-id", "--output", "--gap-bytes", "--batch-size", "--concurrency"].includes(args[i]) || !args[i + 1] || options[args[i]]) throw new Error("Invalid arguments; use --help");
    options[args[i]] = args[i + 1];
  }
  if (!options["--manifest"] || !/^\d+$/.test(options["--sub-id"] || "")) throw new Error("--manifest and numeric --sub-id are required");
  const maxGap = Number(options["--gap-bytes"] || 0);
  const batchSize = Number(options["--batch-size"] || 128);
  const concurrency = Number(options["--concurrency"] || 4);
  if (!Number.isInteger(maxGap) || maxGap < 0 || maxGap > 65536 || !Number.isInteger(batchSize) || batchSize < 1 || batchSize > 128 || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) throw new Error("Invalid range options");
  if (options["--output"] && await access(options["--output"]).then(() => true, error => {
    if (error.code === "ENOENT") return false;
    throw error;
  })) throw new Error("Output already exists; choose another file");
  const manifest = JSON.parse(await readFile(options["--manifest"], "utf8"));
  const entry = manifest.items?.find(item => String(item.sub_id) === options["--sub-id"]);
  if (!entry?.media_url) throw new Error("Selected lesson has no media_url in the manifest");
  const controller = new AbortController();
  process.once("SIGINT", () => controller.abort(new Error("Cancelled")));
  const started = performance.now();
  const index = await inspectAudio(entry.media_url, { signal: controller.signal });
  let summary = index.summary;
  if (options["--output"]) {
    const output = resolve(options["--output"]);
    const result = await downloadIndexedAudio(index, { signal: controller.signal, maxGap, batchSize, concurrency });
    await mkdir(dirname(output), { recursive: true });
    await writeFile(output, Buffer.from(await result.blob.arrayBuffer()), { flag: "wx" });
    summary = result.summary;
  } else index.file.stop();
  console.log(JSON.stringify({ ...summary, elapsed_seconds: (performance.now() - started) / 1000 }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    // Network errors may include the input URL: only print the error class here.
    console.error(error.name === "TypeError" ? "Network request failed" : error.message);
    process.exitCode = 1;
  });
}
