// Run with an existing CodeGraph lib: CODEGRAPH_LIB=/path/to/codegraph/lib node this-file.
// No upstream source/dependency installation, database, model, or watcher is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const cp = require('node:child_process');
const vm = require('node:vm');
const {stripTypeScriptTypes} = require('node:module');
const {test, before, after} = require('node:test');

assert.ok(process.env.CODEGRAPH_LIB, 'set CODEGRAPH_LIB to an already installed CodeGraph lib directory');
const installed = path.resolve(process.env.CODEGRAPH_LIB);
const {Parser, Language} = require(path.join(installed, 'node_modules/web-tree-sitter/tree-sitter.cjs'));
const helpers = require(path.join(installed, 'dist/extraction/tree-sitter-helpers.js'));
const patch = path.join(__dirname, '../patches/codegraph/0002-feat-Add-Elixir-extraction-and-resolution.patch');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'elixir-patch-test-'));
const env = {...process.env, HOME:temp, XDG_CONFIG_HOME:path.join(temp, 'config'), XDG_CACHE_HOME:path.join(temp, 'cache'), PYTHONDONTWRITEBYTECODE:'1'};
const contents = fs.readFileSync(patch, 'utf8');
function additions(file) {
  const marker = `diff --git a/${file} b/${file}\n`;
  const section = contents.split(marker)[1];
  assert.ok(section, `missing patch section: ${file}`);
  return section.split('\ndiff --git ')[0].split('\n').filter(line => line.startsWith('+') && !line.startsWith('+++')).map(line => line.slice(1)).join('\n');
}
function load(source, name, globals={}) {
  const code = stripTypeScriptTypes(source.replace(/^import .*;\n/gm, ''), {mode:'strip'})
    .replace(`export const ${name}`, `const ${name}`).replace(`export function ${name}`, `function ${name}`);
  const context = {module:{exports:{}}, ...globals};
  vm.runInNewContext(code + `\nmodule.exports = ${name};`, context);
  return context.module.exports;
}
const extractor = load(additions('src/extraction/languages/elixir.ts'), 'elixirExtractor', helpers);
const match = load(additions('src/resolution/name-matcher.ts'), 'matchElixirReference');
let grammar;
before(async () => {
  cp.execFileSync('git', ['apply', '--include=src/extraction/wasm/tree-sitter-elixir.wasm', patch], {cwd:temp, env, stdio:'pipe'});
  await Parser.init();
  grammar = await Language.load(path.join(temp, 'src/extraction/wasm/tree-sitter-elixir.wasm'));
});
after(() => {fs.rmSync(temp, {recursive:true, force:true});});

function graph(source) {
  const parser = new Parser();
  parser.setLanguage(grammar);
  const tree = parser.parse(source);
  assert.equal(tree.rootNode.hasError, false, 'fixture must parse without errors');
  const refs = [];
  const ctx = {
    source, filePath:'fixture.ex', nodes:[], nodeStack:[],
    pushScope(id) {this.nodeStack.push(id);}, popScope() {this.nodeStack.pop();},
    addUnresolvedReference(ref) {refs.push(ref);},
    createNode(kind, name, syntax, extra={}) {
      const owner = this.nodes.find(node => node.id === this.nodeStack.at(-1));
      const qualifiedName = extra.qualifiedName ?? (owner ? `${owner.qualifiedName}::${name}` : name);
      const node = {id:`${kind}:${qualifiedName}`, kind, name, qualifiedName, language:'elixir',
                    startLine:syntax.startPosition.row+1, endLine:syntax.endPosition.row+1,
                    endColumn:syntax.endPosition.column, ...extra};
      this.nodes.push(node); return node;
    },
    visitNode(node) {if (!extractor.visitNode(node, this)) for (const child of node.namedChildren) this.visitNode(child);}
  };
  ctx.visitNode(tree.rootNode);
  tree.delete(); parser.delete();
  const context = {getNodeById(id) {return ctx.nodes.find(node => node.id === id);},
                   getNodesByQualifiedName(name) {return ctx.nodes.filter(node => node.qualifiedName === name);}};
  return {
    callees(name) {
      const caller = ctx.nodes.find(node => node.qualifiedName === name);
      assert.ok(caller, `missing caller ${name}`);
      return refs.filter(ref => ref.fromNodeId === caller.id && ref.referenceKind === 'calls')
        .map(ref => match(ref, context)).filter(Boolean)
        .map(result => context.getNodeById(result.targetNodeId).qualifiedName).sort();
    }
  };
}

test('each lower arity evaluates only its omitted default expressions', () => {
  const result = graph(String.raw`defmodule Defaults do
  def first(), do: :first
  def second(), do: :second
  def body(a, b), do: {a, b}
  def run(a \\ first(), b \\ second()), do: body(a, b)
end`);
  assert.deepEqual(result.callees('Defaults::run/0'), ['Defaults::first/0', 'Defaults::run/2', 'Defaults::second/0']);
  assert.deepEqual(result.callees('Defaults::run/1'), ['Defaults::run/2', 'Defaults::second/0']);
  assert.deepEqual(result.callees('Defaults::run/2'), ['Defaults::body/2']);
});

test('declared defaults retain references when the body is in another clause', () => {
  const result = graph(String.raw`defmodule Defaults do
  def seed(), do: :seed
  def run(a \\ seed())
  def run(a), do: a
end`);
  assert.deepEqual(result.callees('Defaults::run/0'), ['Defaults::run/1', 'Defaults::seed/0']);
  assert.deepEqual(result.callees('Defaults::run/1'), []);
});

const imports = `defmodule Source do
  def f(x), do: x
  defmacro m(x), do: quote do: unquote(x)
end
defmodule Functions do
  import Source, only: :functions
  def run_f(x), do: f(x)
  def run_m(x), do: m(x)
  defp m(x), do: x
end
defmodule Macros do
  import Source, only: :macros
  def run_m(x), do: m(x)
  def run_f(x), do: f(x)
  defp f(x), do: x
end
defmodule Listed do
  import Source, only: [f: 1, m: 1]
  def run_f(x), do: f(x)
  def run_m(x), do: m(x)
end
defmodule Excluded do
  import Source, except: [m: 1]
  def run_f(x), do: f(x)
  def run_m(x), do: m(x)
  defp m(x), do: x
end`;

test('only functions resolves functions and preserves local macro-named functions', () => {
  const result = graph(imports);
  assert.deepEqual(result.callees('Functions::run_f/1'), ['Source::f/1']);
  assert.deepEqual(result.callees('Functions::run_m/1'), ['Functions::m/1']);
});
test('only macros resolves macros and preserves local function-named functions', () => {
  const result = graph(imports);
  assert.deepEqual(result.callees('Macros::run_m/1'), ['Source::m/1']);
  assert.deepEqual(result.callees('Macros::run_f/1'), ['Macros::f/1']);
});
test('literal import lists and except lists keep their existing behavior', () => {
  const result = graph(imports);
  assert.deepEqual(result.callees('Listed::run_f/1'), ['Source::f/1']);
  assert.deepEqual(result.callees('Listed::run_m/1'), ['Source::m/1']);
  assert.deepEqual(result.callees('Excluded::run_f/1'), ['Source::f/1']);
  assert.deepEqual(result.callees('Excluded::run_m/1'), ['Excluded::m/1']);
});
