// Parser-level cross-cell transform for execute_code's Bun kernel (ADR-4 D9).
//
// Cells execute through indirect eval in the runner's global scope. JavaScript
// eval cannot parse module syntax, so this pass rewrites a cell's top-level
// import/export declarations into dynamic imports plus `globalThis` publishing,
// and publishes top-level declarations so a later cell can reference them as
// bare identifiers. Everything else — regex/division, templates, comments,
// nested blocks, every function shape — is left to the parser; there is no
// tokenizer and no fail-closed construct list.

import { pathToFileURL } from "node:url";
import { parse } from "./eval_js_parser.ts";

interface Edit {
  start: number;
  end: number;
  text: string;
  seq: number;
}

/// The acorn node surface this pass reads; every field is validated before use.
interface AstNode {
  type: string;
  start: number;
  end: number;
  [key: string]: unknown;
}

export interface TransformResult {
  code: string;
  /// True when the cell must run inside `(async () => { … })()` (top-level
  /// await/return, or a rewritten static import).
  wrapped: boolean;
}

const META_NAME = "__hermes_import_meta__";
const IMPORT_HELPER = "__hermes_import__";
const IDENT = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
const POSITION_KEYS: Record<string, true> = { start: true, end: true, loc: true, range: true };

function asNode(value: unknown): AstNode | null {
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.type !== "string"
      || typeof candidate.start !== "number"
      || typeof candidate.end !== "number") return null;
  return candidate as AstNode;
}

function getNode(node: AstNode, key: string): AstNode | null {
  return asNode(node[key]);
}

function getNodes(node: AstNode, key: string): AstNode[] {
  const value = node[key];
  if (!Array.isArray(value)) return [];
  return value.map(asNode).filter((item): item is AstNode => item !== null);
}

function identifierName(node: AstNode | null): string {
  if (!node || node.type !== "Identifier" || typeof node.name !== "string") return "";
  return node.name;
}

function propertyAccess(object: string, name: string): string {
  return IDENT.test(name) ? `${object}.${name}` : `${object}[${JSON.stringify(name)}]`;
}

function propertyName(node: AstNode | null): string {
  if (!node) return "";
  if (node.type === "Identifier") return identifierName(node);
  if (typeof node.value === "string") return node.value;
  if (typeof node.value === "number") return String(node.value);
  return "";
}

/// Every Identifier bound by a declaration pattern (identifier, object/array
/// destructuring, defaults, rest).
function bindingNames(pattern: AstNode | null, out: string[] = []): string[] {
  if (!pattern) return out;
  switch (pattern.type) {
    case "Identifier":
      if (identifierName(pattern)) out.push(identifierName(pattern));
      break;
    case "ObjectPattern":
      for (const prop of getNodes(pattern, "properties")) {
        if (prop.type === "RestElement") bindingNames(getNode(prop, "argument"), out);
        else bindingNames(getNode(prop, "value"), out);
      }
      break;
    case "ArrayPattern":
      for (const element of getNodes(pattern, "elements")) bindingNames(element, out);
      break;
    case "AssignmentPattern":
      bindingNames(getNode(pattern, "left"), out);
      break;
    case "RestElement":
      bindingNames(getNode(pattern, "argument"), out);
      break;
    default:
      break;
  }
  return out;
}

function declarationNames(declaration: AstNode | null): string[] {
  if (!declaration) return [];
  if (declaration.type === "VariableDeclaration") {
    const names: string[] = [];
    for (const declarator of getNodes(declaration, "declarations")) {
      bindingNames(getNode(declarator, "id"), names);
    }
    return names;
  }
  const name = identifierName(getNode(declaration, "id"));
  return name ? [name] : [];
}

function publish(names: string[]): string {
  return names.map((name) => `globalThis[${JSON.stringify(name)}] = ${name};`).join(" ");
}

function parseModule(source: string): AstNode | null {
  return asNode(parse(source, {
    ecmaVersion: "latest",
    sourceType: "module",
    allowReturnOutsideFunction: true,
    allowAwaitOutsideFunction: true,
  }));
}

function replaceRanges(source: string, ranges: Array<[number, number]>, replacement: string): string {
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  let out = "";
  let cursor = 0;
  for (const [start, end] of sorted) {
    if (start < cursor) continue;
    out += source.slice(cursor, start) + replacement;
    cursor = end;
  }
  return out + source.slice(cursor);
}

function applyEdits(source: string, edits: Edit[]): string {
  edits.sort((a, b) => a.start - b.start || a.seq - b.seq);
  let out = "";
  let cursor = 0;
  for (const edit of edits) {
    if (edit.start < cursor) continue; // earlier replacement already covers this range
    out += source.slice(cursor, edit.start) + edit.text;
    cursor = edit.end;
  }
  return out + source.slice(cursor);
}

export function transformCell(source: string, sessionCwd: string): TransformResult {
  // TypeScript first: acorn parses JS (annotations/enums removed, ESM kept).
  const js = new Bun.Transpiler({ loader: "ts" }).transformSync(source);
  const parsed = parseModule(js);
  if (!parsed) return { code: js, wrapped: false };

  const edits: Edit[] = [];
  const deferred: string[] = [];
  const metaRanges: Array<[number, number]> = [];
  let wrapped = false;
  let seq = 0;
  let moduleCounter = 0;

  const edit = (start: number, end: number, text: string) => {
    edits.push({ start, end, text, seq: seq++ });
  };
  const insert = (at: number, text: string) => edit(at, at, text);

  const scan = (value: unknown, functionDepth: number) => {
    const node = asNode(value);
    if (!node) return;
    if (node.type === "MetaProperty"
        && identifierName(getNode(node, "meta")) === "import"
        && identifierName(getNode(node, "property")) === "meta") {
      metaRanges.push([node.start, node.end]);
    }
    if (functionDepth === 0) {
      if (node.type === "AwaitExpression" || node.type === "ReturnStatement") wrapped = true;
      if (node.type === "ForOfStatement" && node.await === true) wrapped = true;
    }
    const childDepth = functionDepth
      + (node.type === "FunctionDeclaration"
         || node.type === "FunctionExpression"
         || node.type === "ArrowFunctionExpression" ? 1 : 0);
    for (const key of Object.keys(node)) {
      if (POSITION_KEYS[key]) continue;
      const child = node[key];
      if (Array.isArray(child)) {
        for (const item of child) scan(item, childDepth);
      } else if (child !== null && typeof child === "object") {
        scan(child, childDepth);
      }
    }
  };
  scan(parsed, 0);

  // `import.meta` is not legal in eval, and a text rewrite would corrupt the
  // same characters inside strings/templates/comments. Replace only the ranges
  // the parser reported, then re-parse so every later edit sees the new offsets.
  let codeSource = js;
  let ast = parsed;
  if (metaRanges.length) {
    codeSource = replaceRanges(js, metaRanges, META_NAME);
    const reparsed = parseModule(codeSource);
    if (reparsed) ast = reparsed;
  }

  const rewriteImport = (node: AstNode) => {
    wrapped = true;
    const moduleName = `__hermes_module_${moduleCounter++}`;
    const source = propertyName(getNode(node, "source"));
    const lines = [`const ${moduleName} = await ${IMPORT_HELPER}(${JSON.stringify(source)});`];
    for (const specifier of getNodes(node, "specifiers")) {
      const local = identifierName(getNode(specifier, "local"));
      if (!local) continue;
      if (specifier.type === "ImportNamespaceSpecifier") {
        lines.push(`const ${local} = ${moduleName}; globalThis[${JSON.stringify(local)}] = ${local};`);
      } else if (specifier.type === "ImportDefaultSpecifier") {
        lines.push(`const ${local} = ${propertyAccess(moduleName, "default")}; globalThis[${JSON.stringify(local)}] = ${local};`);
      } else if (specifier.type === "ImportSpecifier") {
        const imported = propertyName(getNode(specifier, "imported"));
        lines.push(`const ${local} = ${propertyAccess(moduleName, imported)}; globalThis[${JSON.stringify(local)}] = ${local};`);
      }
    }
    edit(node.start, node.end, lines.join(" "));
  };

  const rewriteExportNamed = (node: AstNode) => {
    const declaration = getNode(node, "declaration");
    if (declaration) {
      edit(node.start, declaration.start, "");
      insert(declaration.end, "\n;" + publish(declarationNames(declaration)) + "\n");
      return;
    }
    const sourceNode = getNode(node, "source");
    if (sourceNode) {
      wrapped = true;
      const moduleName = `__hermes_module_${moduleCounter++}`;
      const source = propertyName(sourceNode);
      const lines = [`const ${moduleName} = await ${IMPORT_HELPER}(${JSON.stringify(source)});`];
      for (const specifier of getNodes(node, "specifiers")) {
        const exported = propertyName(getNode(specifier, "exported"));
        if (specifier.type === "ExportNamespaceSpecifier") {
          lines.push(`globalThis[${JSON.stringify(exported)}] = ${moduleName};`);
        } else {
          const local = propertyName(getNode(specifier, "local"));
          lines.push(`globalThis[${JSON.stringify(exported)}] = ${propertyAccess(moduleName, local)};`);
        }
      }
      edit(node.start, node.end, lines.join(" "));
      return;
    }
    // `export { a, b as c }` publishes live bindings once the cell has initialised them.
    for (const specifier of getNodes(node, "specifiers")) {
      const local = propertyName(getNode(specifier, "local"));
      const exported = propertyName(getNode(specifier, "exported"));
      deferred.push(`globalThis[${JSON.stringify(exported)}] = ${local};`);
    }
    edit(node.start, node.end, "");
  };

  const rewriteExportDefault = (node: AstNode) => {
    const declaration = getNode(node, "declaration");
    if (!declaration) return;
    const declarationSource = codeSource.slice(declaration.start, declaration.end);
    if (declaration.type === "FunctionDeclaration" || declaration.type === "ClassDeclaration") {
      const name = identifierName(getNode(declaration, "id"));
      if (name) {
        edit(node.start, declaration.start, "");
        insert(declaration.end,
               "\n;" + publish([name]) + ` globalThis["default"] = ${name};\n`);
      } else {
        edit(node.start, node.end,
             `const __hermes_default__ = (${declarationSource}); globalThis["default"] = __hermes_default__;`);
      }
      return;
    }
    edit(node.start, node.end, `globalThis["default"] = (${declarationSource});`);
  };

  const rewriteExportAll = (node: AstNode) => {
    wrapped = true;
    const source = propertyName(getNode(node, "source"));
    const imported = `await ${IMPORT_HELPER}(${JSON.stringify(source)})`;
    const exported = propertyName(getNode(node, "exported"));
    if (exported) {
      edit(node.start, node.end, `globalThis[${JSON.stringify(exported)}] = ${imported};`);
    } else {
      edit(node.start, node.end, `Object.assign(globalThis, ${imported});`);
    }
  };

  for (const node of getNodes(ast, "body")) {
    switch (node.type) {
      case "ImportDeclaration":
        rewriteImport(node);
        break;
      case "ExportNamedDeclaration":
        rewriteExportNamed(node);
        break;
      case "ExportDefaultDeclaration":
        rewriteExportDefault(node);
        break;
      case "ExportAllDeclaration":
        rewriteExportAll(node);
        break;
      case "VariableDeclaration":
      case "FunctionDeclaration":
      case "ClassDeclaration": {
        const names = declarationNames(node);
        if (names.length) insert(node.end, "\n;" + publish(names) + "\n");
        break;
      }
      default:
        break;
    }
  }

  let transformed = applyEdits(codeSource, edits);
  if (deferred.length) transformed += "\n;" + deferred.join(" ") + "\n";
  if (metaRanges.length) {
    const url = pathToFileURL(sessionCwd + "/cell.ts").href;
    const meta = JSON.stringify({ url, dir: sessionCwd, filename: sessionCwd + "/cell.ts" });
    transformed = `const ${META_NAME} = ${meta};\n` + transformed;
  }
  return { code: transformed, wrapped };
}
