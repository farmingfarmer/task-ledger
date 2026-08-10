// Static check for the dashboard's inline JS.
//
// `node --check` proves the file parses; it says nothing about a variable
// that was deleted along with the function above it. That is exactly how
// REASON_ROUTE reached the browser as "Can't find variable" while every
// test passed and the page rendered. This catches the class.
//
//   node integration/dashboard/check_js.js integration/dashboard/dashboard.html
var fs = require("fs");

var file = process.argv[2] || "integration/dashboard/dashboard.html";
var html = fs.readFileSync(file, "utf8");
var js = html.split("<script>")[1].split("</script>")[0];

// Comments and string literals are not code: an identifier inside either
// is prose, and scanning them is how a checker cries wolf.
var code = js
  .replace(/\/\*[\s\S]*?\*\//g, " ")
  .replace(/(^|[^:])\/\/[^\n]*/g, "$1 ")
  .replace(/"(?:[^"\\]|\\.)*"/g, '""')
  .replace(/'(?:[^'\\]|\\.)*'/g, "''")
  .replace(/\/(?:[^\/\\\n\[]|\\.|\[[^\]]*\])+\/[gimsuy]*/g, "/re/");

var declared = Object.create(null);
// Built-in globals that match the SCREAMING_CASE shape. Only ones that
// genuinely are all-caps qualify -- Math and Object never trip the regex.
["JSON", "NaN", "URL", "DOM"].forEach(function (g) { declared[g] = true; });
// `var a = 1, b = 2, c;` declares three names. Reading only the first is
// how this checker cried wolf over a chart's layout constants.
var vre = /\bvar\s+([^;{}]+)/g, vm;
while ((vm = vre.exec(code)))
  vm[1].split(",").forEach(function (part) {
    var name = part.trim().split(/[=\s]/)[0];
    if (/^[A-Za-z_$][\w$]*$/.test(name)) declared[name] = true;
  });
var fre = /\bfunction\s+([A-Za-z_$][\w$]*)/g, fm;
while ((fm = fre.exec(code))) declared[fm[1]] = true;
var pre = /function\s*[A-Za-z_$\w]*\s*\(([^)]*)\)/g, pm;
while ((pm = pre.exec(code)))
  pm[1].split(",").forEach(function (a) {
    a = a.trim(); if (a) declared[a] = true; });

// SCREAMING_CASE identifiers are module constants by convention here, so a
// missing one is unambiguous -- no guessing about scope or hoisting.
var used = Object.create(null), ure = /\b([A-Z][A-Z0-9_]{2,})\b/g, um;
while ((um = ure.exec(code))) used[um[1]] = true;

var missing = Object.keys(used).filter(function (u) { return !declared[u]; });
if (missing.length) {
  console.error("UNDECLARED: " + missing.join(", "));
  process.exit(1);
}

// The .hidden property only hides if CSS lets it. An author rule with its
// own display: outranks the browser's [hidden]{display:none}, so an element
// this code "hides" stays on screen -- which is how an empty profile switch
// and an empty banner shipped as a stray dot and an orange bar. One global
// rule fixes every case; this asserts it is still there.
// Comments stripped first: the rule is explained in a comment that quotes
// itself, and matching that comment made this check pass with the rule
// deleted -- a checker that cannot fail is worse than none.
var cssOnly = html.replace(/\/\*[\s\S]*?\*\//g, "");
if (!/\[hidden\]\s*\{[^}]*display\s*:\s*none/.test(cssOnly)) {
  console.error(
    "MISSING CSS: no [hidden]{display:none} rule. JS toggles .hidden on " +
    "elements that carry their own display:, and without this rule those " +
    "elements never actually hide.");
  process.exit(1);
}

console.log("dashboard JS: no undeclared module constants; hidden hides");
