import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const pinets = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  ".tools",
  "pinets",
  "node_modules",
  "pinets",
  "dist",
  "pinets.min.es.js",
);

function arm(source) {
  const start = source.search(/strategy\s*\(/);
  if (start < 0) return source;
  const open = source.indexOf("(", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "(") depth += 1;
    else if (char === ")") {
      depth -= 1;
      if (depth === 0) {
        const call = source.slice(open + 1, index);
        const extras = [
          ["margin_long", "0"],
          ["margin_short", "0"],
          ["commission_type", "strategy.commission.cash_per_contract"],
          ["commission_value", "5.5"],
          ["slippage", "1"],
          ["initial_capital", "50000"],
          ["default_qty_type", "strategy.fixed"],
          ["default_qty_value", "1"],
        ].filter(([name]) => !new RegExp(`\\b${name}\\s*=`).test(call));
        if (!extras.length) return source;
        const addition = extras.map(([name, value]) => `${name}=${value}`).join(", ");
        const prefix = call.trim() ? ", " : "";
        return source.slice(0, index) + prefix + addition + source.slice(index);
      }
    }
  }
  return source;
}

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const { PineTS } = await import(pathToFileURL(pinets).href);
const candles = request.candles || [];
const pine = new PineTS(candles, request.symbol || "ES", String(request.timeframe || "1"));
const mintick = Number(request.mintick) || 0.25;
pine._syminfo = {
  ticker: request.symbol || "ES",
  tickerid: request.symbol || "ES",
  root: request.symbol || "ES",
  description: request.symbol || "ES",
  type: "futures",
  currency: "USD",
  basecurrency: "USD",
  timezone: "America/New_York",
  mintick,
  minmove: 1,
  pricescale: Math.max(1, Math.round(1 / mintick)),
  pointvalue: Number(request.pointvalue) || 1,
  mincontract: 1,
};
try {
  const context = await pine.run(arm(String(request.source || "")));
  const strategy = context.strategy || {};
  const trades = (strategy.closedtrades || []).filter((trade) => trade && trade.exit_time);
  process.stdout.write(JSON.stringify({ trades }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: String(error && error.message ? error.message : error) }));
  process.exitCode = 1;
}
