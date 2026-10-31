/**
 * Purity gate for `omp-ui/src` (omp-ui/src/VENDORED.md, R1–R3/R5).
 *
 * R1 — a `src/**` import specifier never starts with `@oh-my-pi/`, except the installed
 *      `@oh-my-pi/pi-tui` (renderer) and `@oh-my-pi/pi-natives` (native addon).
 * R2 — no relative import escapes `src/`.
 * R3 — no `@oh-my-pi/pi-coding-agent` import (already covered by R1, kept as its own message).
 * R5 — `package.json` dependencies are exactly `@oh-my-pi/pi-tui` and `@oh-my-pi/pi-natives`.
 *
 * Plus: every relative import (and every vendored package subpath) resolves to a file.
 * Run with `bun scripts/check-imports.ts`; exit 1 on any violation.
 */
import { readdirSync, readFileSync, statSync } from "node:fs"
import { dirname, join, relative, resolve, sep } from "node:path"

const ROOT = resolve(import.meta.dir, "..")
const SRC = join(ROOT, "src")
const INSTALLED = ["@oh-my-pi/pi-tui", "@oh-my-pi/pi-natives"]

function installed(specifier: string): boolean {
	return INSTALLED.some(name => specifier === name || specifier.startsWith(`${name}/`))
}

interface Violation {
	rule: "R1" | "R2" | "R3" | "R5" | "resolve"
	file: string
	specifier: string
	detail?: string
}

function walk(dir: string, out: string[] = []): string[] {
	for (const entry of readdirSync(dir)) {
		const path = join(dir, entry)
		if (statSync(path).isDirectory()) walk(path, out)
		else if (entry.endsWith(".ts")) out.push(path)
	}
	return out
}

/** Import/export specifiers, line-anchored so code strings and comments do not match. */
function stripComments(text: string): string {
	return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1")
}

function specifiersOf(text: string): string[] {
	const found = new Set<string>()
	const source = stripComments(text)
	for (const match of source.matchAll(/(?:^|\n)[ \t]*(?:import|export)\s+(?:[^;'"]|\n)*?from\s*["']([^"']+)["']/g)) {
		found.add(match[1])
	}
	for (const match of source.matchAll(/(?:^|\n)[ \t]*import\s*["']([^"']+)["']/g)) found.add(match[1])
	for (const match of source.matchAll(/\bimport\s*\(\s*["']([^"']+)["']\s*\)/g)) found.add(match[1])
	for (const match of source.matchAll(/\brequire\s*\(\s*["']([^"']+)["']\s*\)/g)) found.add(match[1])
	return [...found]
}

function resolves(fromDir: string, specifier: string): boolean {
	const base = resolve(fromDir, specifier)
	const candidates = [base]
	if (base.endsWith(".js")) candidates.push(base.slice(0, -3) + ".ts", base.slice(0, -3) + ".tsx")
	candidates.push(
		`${base}.ts`,
		`${base}.tsx`,
		`${base}.d.ts`,
		join(base, "index.ts"),
		join(base, "index.tsx"),
		join(base, "index.js")
	)
	return candidates.some(path => {
		try {
			return statSync(path).isFile()
		} catch {
			return false
		}
	})
}

const violations: Violation[] = []
const files = walk(SRC)

for (const file of files) {
	const rel = relative(ROOT, file).split(sep).join("/")
	const text = readFileSync(file, "utf8")
	for (const specifier of specifiersOf(text)) {
		if (specifier.startsWith("@oh-my-pi/")) {
			if (installed(specifier)) continue
			if (specifier === "@oh-my-pi/pi-coding-agent" || specifier.startsWith("@oh-my-pi/pi-coding-agent/")) {
				violations.push({ rule: "R3", file: rel, specifier })
			}
			violations.push({ rule: "R1", file: rel, specifier })
			continue
		}
		if (!specifier.startsWith(".")) continue
		const target = resolve(dirname(file), specifier)
		const withinSrc = relative(SRC, target)
		if (withinSrc.startsWith("..") || withinSrc.split(sep)[0] === "..") {
			violations.push({ rule: "R2", file: rel, specifier, detail: relative(ROOT, target).split(sep).join("/") })
			continue
		}
		if (!resolves(dirname(file), specifier)) {
			violations.push({ rule: "resolve", file: rel, specifier })
		}
	}
}

const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8")) as { dependencies?: Record<string, string> }
const dependencies = Object.keys(pkg.dependencies ?? {})
const expected = [...INSTALLED].sort().join(", ")
if ([...dependencies].sort().join(", ") !== expected) {
	violations.push({ rule: "R5", file: "package.json", specifier: dependencies.join(", ") })
}

const resolved = files.length
if (violations.length === 0) {
	console.log(`ok: ${resolved} files, 0 unresolved relative modules, R1–R3/R5 clean`)
	process.exit(0)
}

for (const violation of violations) {
	const detail = violation.detail ? ` → ${violation.detail}` : ""
	console.error(`${violation.rule}: ${violation.file}: ${violation.specifier}${detail}`)
}
console.error(`FAIL: ${violations.length} violation(s) in ${resolved} files`)
process.exit(1)
