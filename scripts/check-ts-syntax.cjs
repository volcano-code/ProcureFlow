/* Syntax only: this is NOT tsc type checking or next build. Requires TypeScript. */
const fs = require('node:fs');
const path = require('node:path');
let ts;
try { ts = process.env.PF_TYPESCRIPT_PATH ? require(process.env.PF_TYPESCRIPT_PATH) : require('../apps/web/node_modules/typescript'); }
catch { console.error('Install apps/web dependencies before running this syntax-only check.'); process.exit(2); }
const root = path.resolve(__dirname, '../apps/web');
const files = [];
function walk(dir) { for (const item of fs.readdirSync(dir, {withFileTypes: true})) { if(['node_modules', '.next'].includes(item.name)) continue; const p=path.join(dir,item.name); if(item.isDirectory()) walk(p); else if(/\.tsx?$/.test(p)&&!p.endsWith('.d.ts')) files.push(p); } }
walk(root);
const errors = [];
for (const file of files) {
  const out = ts.transpileModule(fs.readFileSync(file, 'utf8'), {fileName:file, reportDiagnostics:true,
    compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,jsx:ts.JsxEmit.ReactJSX,isolatedModules:true}});
  for (const d of out.diagnostics || []) if (d.category === ts.DiagnosticCategory.Error) errors.push(ts.flattenDiagnosticMessageText(d.messageText, '\n'));
}
console.log(JSON.stringify({check:'typescript-transpile-syntax-only', files:files.map(p=>path.relative(root,p)), errors, full_typecheck:false, next_build:false}, null, 2));
if (errors.length) process.exit(1);
