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

// The desk runs a copy of the PineTS bundle with these changes, rebuilt whenever
// PineTS is reinstalled. A change whose code is no longer in the bundle is skipped.
const DESK_PATCHES = [
  {
    // PineTS drops the stop and limit of a strategy.exit placed on the same bar as
    // the reversal entry it belongs to. TradingView fills them: the POC Confluence
    // export stops out at 06:45 on 2026-09-04 on such an order.
    name: "reversal exit",
    from: "p._attachedAtReversal&&!p._isPersistent&&(p.limit!==void 0&&(S=void 0),p.stop!==void 0&&(v=void 0))",
    to: "!1",
  },
  {
    // hour(time, "America/New_York") built a new Intl.DateTimeFormat on every call.
    // A script that reads the day of each closed trade on each bar spent minutes
    // there. The formatter is kept per timezone and each answer per timestamp.
    name: "timezone formatter",
    from:
      'try{const n=new Intl.DateTimeFormat("en-US",{timeZone:e,year:"numeric",month:"numeric",day:"numeric",hour:"numeric",minute:"numeric",second:"numeric",weekday:"short",hour12:!1}).formatToParts(new Date(t)),',
    to:
      'try{const __h=globalThis.__deskTz||(globalThis.__deskTz={f:new Map,r:new Map}),__k=e+"|"+t,__c=__h.r.get(__k);if(__c)return{...__c};let __f=__h.f.get(e);__f||(__f=new Intl.DateTimeFormat("en-US",{timeZone:e,year:"numeric",month:"numeric",day:"numeric",hour:"numeric",minute:"numeric",second:"numeric",weekday:"short",hour12:!1}),__h.f.set(e,__f));const n=__f.formatToParts(new Date(t)),',
  },
  {
    name: "timezone answer",
    needs: "timezone formatter",
    from: 'return{year:s("year"),month:s("month"),day:s("day"),hour:a,minute:s("minute"),second:s("second"),dayOfWeek:l[o]??0}}catch{',
    to: 'const __o={year:s("year"),month:s("month"),day:s("day"),hour:a,minute:s("minute"),second:s("second"),dayOfWeek:l[o]??0};__h.r.size>2e5&&__h.r.clear(),__h.r.set(__k,__o);return{...__o}}catch{',
  },
  {
    // With process_orders_on_close, a stop or limit placed or moved on the bar's close
    // that the close has already crossed fills on that close, with slippage on a stop.
    // PineTS waited for the next open: the EMA BB Mean Reversion NQ trailing stop at
    // 10:30 on 2026-09-24 filled at 30580 on the next bar instead of 30579 at the close.
    // A position that goes flat this way cancels the exits placed while it was open.
    name: "exit crossed at the close",
    from: '}else i&&(a.category??"entry")==="entry"&&a.type==="market"&&(Hi(t,r),Vl(t,a,r,n,r));e.pending_orders=e.pending_orders.filter(a=>a.status==="pending"),Hi(t,r),Fl(t)}',
    to:
      '}else if(i&&a.category==="exit"&&a.trail_price===void 0&&a.trail_points===void 0&&(a.stop!==void 0||a.limit!==void 0)){' +
      "const o=e.opentrades.filter(p=>!a.from_entry||p.entry_id===a.from_entry);" +
      "if(o.length){const d=Math.sign(o[0].size),x=a.stop!==void 0&&(d>0?r<=a.stop:r>=a.stop),w=a.limit!==void 0&&(d>0?r>=a.limit:r<=a.limit);" +
      "if(x||w){const l=o.reduce((p,h)=>p+Math.abs(h.size),0);let c=l;a.qty&&a.qty>0?c=Math.min(a.qty,l):a.qty_percent&&a.qty_percent>0&&(c=as(t,l,a.qty_percent));" +
      'const u=x?Jr(t,-d,r):r;os(t,a.from_entry,c,u,n,{exitId:a.id,exitComment:x?a.comment_loss??a.comment:a.comment_profit??a.comment}),a.status="filled",a.fill_price=u,a.fill_bar=t.idx,a.fill_time=n,' +
      'e.opentrades.length===0&&e.pending_orders.forEach(q=>{q!==a&&q.status==="pending"&&q.category==="exit"&&q._placedWithPosition&&(q.status="cancelled")})}}}' +
      'else i&&(a.category??"entry")==="entry"&&a.type==="market"&&(Hi(t,r),Vl(t,a,r,n,r));e.pending_orders=e.pending_orders.filter(a=>a.status==="pending"),Hi(t,r),Fl(t)}',
  },
  {
    // A bar that opens past the stop fills the stop at the open, even when the same
    // bar later reaches the target. PineTS ordered the two only by the high and low.
    // The Day shift BOS 17k NQ 5m export stops out at the open of 19:35 on 2025-11-18
    // at 24534.5, where PineTS took the target at 24530.
    name: "exit past the open",
    from: 'if(se&&Te?ve=T?"profit":"loss":se?ve="profit":Te&&(ve="loss"),ve==="loss")',
    to: 'if(se&&Te?ve=((V!==void 0&&(w?r<=V:r>=V))?"loss":($!==void 0&&(w?r>=$:r<=$))?"profit":T?"profit":"loss"):se?ve="profit":Te&&(ve="loss"),ve==="loss")',
  },
  {
    // TradingView moves Open, High, Low, Close only when the open is closer to the high.
    // An open halfway between them goes to the low first. PineTS went to the high first:
    // the same export stops out at 06:25 on 2025-12-22, where PineTS took the target.
    name: "exit path on a tie",
    from: "const C=Math.abs(n-r)<=Math.abs(r-s),T=w?C:!C",
    to: "const C=Math.abs(n-r)<Math.abs(r-s),T=w?C:!C",
  },
  {
    name: "extreme path on a tie",
    from: "a=Math.abs(n-r)<=Math.abs(r-s);return i===1?!a:a",
    to: "a=Math.abs(n-r)<Math.abs(r-s);return i===1?!a:a",
  },
  {
    // ta.pivothigh and ta.pivotlow found the pivots of the whole history on every bar
    // and then read the current one, so a run took time with the square of its bars.
    // A one-year 2-minute run of a BOS script passed the 10-minute limit. This checks
    // only the bars around the current one, with the same comparisons, so every
    // value is the one PineTS gave before.
    name: "pivot at one bar",
    from: "function Cy(t,e,i){",
    to:
      "function __deskPivotAt(t,e,i,c,h){const z=Number.isInteger(c)&&c>=0&&c<t.length?NaN:void 0,d=c-(e+i);if(!(d>=0&&Number.isInteger(d)&&c<t.length))return z;" +
      "const s=t[c-i];if(s===void 0||isNaN(s))return z;" +
      "for(let o=1;o<=e;o++){const l=t[c-i-o];if(l===void 0||isNaN(l))break;if(h?l>s:l<s)return z}" +
      "for(let o=1;o<=i;o++){const l=t[c-i+o];if(l===void 0||isNaN(l))break;if(h?l>=s:l<=s)return z}" +
      "return s}function Cy(t,e,i){",
  },
  {
    name: "pivot high at one bar",
    needs: "pivot at one bar",
    from: "l=Cy(o,s,a),c=t.idx;return t.precision(l[c])",
    to: "c=t.idx;return t.precision(__deskPivotAt(o,s,a,c,!0))",
  },
  {
    name: "pivot low at one bar",
    needs: "pivot at one bar",
    from: "l=Iy(o,s,a),c=t.idx;return t.precision(l[c])",
    to: "c=t.idx;return t.precision(__deskPivotAt(o,s,a,c,!1))",
  },
];
const DESK_VERSION = 5;

function deskBundle(original) {
  const patched = path.join(path.dirname(original), `pinets.desk${DESK_VERSION}.es.js`);
  try {
    if (fs.existsSync(patched) && fs.statSync(patched).mtimeMs >= fs.statSync(original).mtimeMs) return patched;
    let text = fs.readFileSync(original, "utf8");
    for (const item of DESK_PATCHES) {
      // A change that uses names another change declares goes in only when that one did.
      const needed = item.needs && DESK_PATCHES.find((other) => other.name === item.needs);
      if (needed && !text.includes(needed.to)) continue;
      if (text.split(item.from).length !== 2) {
        process.stderr.write(`PineTS changed: the ${item.name} code was not found, so that change is not made.\n`);
        continue;
      }
      text = text.replace(item.from, item.to);
    }
    const temporary = `${patched}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, text);
    try {
      fs.renameSync(temporary, patched);
    } catch (error) {
      fs.rmSync(temporary, { force: true });
      if (!fs.existsSync(patched)) throw error;
    }
    return patched;
  } catch (error) {
    process.stderr.write(`The desk copy of PineTS could not be written (${error}), so the stock bundle runs.\n`);
    return original;
  }
}

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
        // Commission and slippage stay as the script sets them. TradingView
        // charts neither when strategy() leaves them out.
        const extras = [
          ["margin_long", "0"],
          ["margin_short", "0"],
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
const { PineTS } = await import(pathToFileURL(deskBundle(pinets)).href);
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
function writeAside(...args) {
  process.stderr.write(`${args.map((item) => String(item)).join(" ")}\n`);
}
console.log = writeAside;
console.info = writeAside;
console.warn = writeAside;
console.error = writeAside;
try {
  const context = await pine.run(arm(String(request.source || "")));
  const strategy = context.strategy || {};
  const trades = (strategy.closedtrades || []).filter((trade) => trade && trade.exit_time);
  process.stdout.write(JSON.stringify({ trades }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: String(error && error.message ? error.message : error) }));
  process.exitCode = 1;
}
