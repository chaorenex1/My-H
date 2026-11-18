import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { formatBytes, isEnoent } from "../vendor/utils/src/index.ts";
import { LRUCache } from "../vendor/utils/src/lru.ts";
import {
	buildDirectoryResource,
	containedRealPath,
	contentTypeForPath,
	ensureWithinRoot,
	validateRelativePath,
} from "./filesystem-resource";
import { parseInternalUrl } from "./parse";
import type {
	InternalResource,
	InternalUrl,
	ProtocolHandler,
	ResolveContext,
	SchemeSpec,
	UrlCompletion,
} from "./types";

export interface LocalProtocolOptions {
	getArtifactsDir?: () => string | null;
	getSessionId?: () => string | null;
}

const WINDOWS_LOCAL_ROOT_MAX_CHARS = 180;

function safeSessionId(options: LocalProtocolOptions): string {
	const raw = options.getSessionId?.() ?? "session";
	const safe = raw.replace(/[^a-zA-Z0-9_.-]/g, "_");
	return safe && safe !== "." && safe !== ".." ? safe : "session";
}

function shortLocalRoot(options: LocalProtocolOptions): string {
	// Derive the short root from the stable session id, never the artifact path,
	// so a session always maps to the same `local://` directory.
	return path.join(os.tmpdir(), "omp-local", safeSessionId(options));
}

const LOCAL_TEXT_SNIFF_BYTES = 8 * 1024;
const LOCAL_TEXT_RESOURCE_MAX_BYTES = 1024 * 1024;
const BINARY_FILE_EXTENSIONS = new Set([
	".7z",
	".avi",
	".bmp",
	".bz2",
	".db",
	".doc",
	".docx",
	".gif",
	".gz",
	".ico",
	".jpeg",
	".jpg",
	".m4v",
	".mkv",
	".mov",
	".mp4",
	".pdf",
	".png",
	".ppt",
	".pptx",
	".rar",
	".sqlite",
	".tgz",
	".webm",
	".webp",
	".wmv",
	".xls",
	".xlsx",
	".xz",
	".zip",
]);

function buildNonTextLocalResource(url: InternalUrl, filePath: string, size: number, reason: string): InternalResource {
	const content = `[Cannot read binary local:// file '${url.href}' (${formatBytes(size)}): ${reason}.]`;
	return {
		url: url.href,
		content,
		contentType: "text/plain",
		size: Buffer.byteLength(content, "utf-8"),
		sourcePath: filePath,
		notes: [LOCAL_WRITE_NOTE],
	};
}

function buildLargeLocalTextResource(url: InternalUrl, filePath: string, size: number): InternalResource {
	const content = `[Cannot materialize local:// file '${url.href}' as an internal text resource (${formatBytes(size)} exceeds ${formatBytes(LOCAL_TEXT_RESOURCE_MAX_BYTES)}).]`;
	return {
		url: url.href,
		content,
		contentType: "text/plain",
		size: Buffer.byteLength(content, "utf-8"),
		sourcePath: filePath,
		notes: [LOCAL_WRITE_NOTE],
	};
}

async function readFilePrefix(filePath: string, maxBytes: number): Promise<Uint8Array> {
	if (maxBytes <= 0) return new Uint8Array();
	const handle = await fs.open(filePath, "r");
	try {
		const buffer = Buffer.allocUnsafe(maxBytes);
		const { bytesRead } = await handle.read(buffer, 0, maxBytes, 0);
		return buffer.subarray(0, bytesRead);
	} finally {
		await handle.close();
	}
}

function isUtf8Text(bytes: Uint8Array): boolean {
	if (bytes.indexOf(0) !== -1) return false;
	try {
		new TextDecoder("utf-8", { fatal: true }).decode(bytes);
		return true;
	} catch {
		return false;
	}
}

async function buildFileResource(
	url: InternalUrl,
	resolved: Extract<ResolvedLocalTarget, { kind: "file" }>,
): Promise<InternalResource> {
	if (BINARY_FILE_EXTENSIONS.has(path.extname(resolved.path).toLowerCase())) {
		return buildNonTextLocalResource(url, resolved.path, resolved.size, "extension is a known binary/container type");
	}
	const sniffBytes = await readFilePrefix(resolved.path, Math.min(resolved.size, LOCAL_TEXT_SNIFF_BYTES));
	if (!isUtf8Text(sniffBytes)) {
		return buildNonTextLocalResource(url, resolved.path, resolved.size, "content is not valid UTF-8 text");
	}
	if (resolved.size > LOCAL_TEXT_RESOURCE_MAX_BYTES) {
		return buildLargeLocalTextResource(url, resolved.path, resolved.size);
	}
	const content = await Bun.file(resolved.path).text();
	return {
		url: url.href,
		content,
		contentType: contentTypeForPath(resolved.path),
		size: Buffer.byteLength(content, "utf-8"),
		sourcePath: resolved.path,
		notes: [LOCAL_WRITE_NOTE],
	};
}

async function listFilesRecursively(rootPath: string): Promise<string[]> {
	const files: string[] = [];
	// Sibling directories are read concurrently; the final sort fixes the order.
	const walk = async (relativeDir: string): Promise<void> => {
		const entries = await fs.readdir(path.join(rootPath, relativeDir), { withFileTypes: true });
		const subdirs: Promise<void>[] = [];
		for (const entry of entries) {
			const entryPath = path.join(relativeDir, entry.name);
			if (entry.isDirectory()) {
				subdirs.push(walk(entryPath));
			} else if (entry.isFile()) {
				files.push(entryPath.replaceAll(path.sep, "/"));
			}
		}
		await Promise.all(subdirs);
	};
	await walk("");
	return files.sort((a, b) => a.localeCompare(b));
}

/**
 * `complete()` runs on every keystroke; reuse a root's sorted listing for a
 * short window instead of re-walking and re-sorting the tree each time.
 */
const completionListings = new LRUCache<string, Promise<string[]>>({ max: 16, ttl: 2000 });

function listCompletionFiles(localRoot: string): Promise<string[]> {
	const cached = completionListings.get(localRoot);
	if (cached) return cached;
	const listing = listFilesRecursively(localRoot);
	completionListings.set(localRoot, listing);
	listing.catch(() => completionListings.delete(localRoot));
	return listing;
}

async function buildListing(url: InternalUrl, localRoot: string): Promise<InternalResource> {
	const files = await listFilesRecursively(localRoot);
	const listing = files.length === 0 ? "(empty)" : files.map(file => `- [${file}](local://${file})`).join("\n");
	const content =
		`# Local\n\n` +
		`Session-scoped scratch space for intermediate files.\n\n` +
		`Root: ${localRoot}\n\n` +
		`${files.length} file${files.length === 1 ? "" : "s"} available:\n\n` +
		`${listing}\n`;

	return {
		url: url.href,
		content,
		contentType: "text/markdown",
		size: Buffer.byteLength(content, "utf-8"),
		sourcePath: localRoot,
	};
}

const LOCAL_AUTHORITY_RE = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i;

function extractRelativePath(url: InternalUrl): string {
	// The authority is the first path segment, decoded below with the rest, so take it as
	// written: `rawHost` is already decoded (a second decode corrupts names containing `%`),
	// and WHATWG `hostname` drops `user@` / `:port` parts of names like `a@b` or `a:1`.
	const host = url.rawHref?.match(LOCAL_AUTHORITY_RE)?.[1] ?? url.hostname;
	const pathname = url.rawPathname ?? url.pathname;

	const combined = host
		? pathname && pathname !== "/"
			? `${host}${pathname}`
			: host
		: pathname && pathname !== "/"
			? pathname.slice(1)
			: "";

	if (!combined) {
		return "";
	}

	let decoded: string;
	try {
		decoded = decodeURIComponent(combined.replaceAll("\\", "/"));
	} catch {
		throw new Error(`Invalid URL encoding in local:// path: ${url.href}`);
	}
	validateRelativePath(decoded, "local");
	return decoded;
}

/** Resolve the session-scoped local:// root, shortening long Windows artifact paths before they hit MAX_PATH. */
export function resolveLocalRoot(options: LocalProtocolOptions, platform: NodeJS.Platform = process.platform): string {
	const artifactsDir = options.getArtifactsDir?.();
	if (artifactsDir) {
		const candidate = path.resolve(artifactsDir, "local");
		if (platform === "win32" && candidate.length >= WINDOWS_LOCAL_ROOT_MAX_CHARS) {
			return shortLocalRoot(options);
		}
		return candidate;
	}

	return path.join(os.tmpdir(), "omp-local", safeSessionId(options));
}

export interface LocalFileEntry {
	/** Root-relative, `/`-separated name, e.g. `plans/x-plan.md`. */
	name: string;
	/** `local://${name}` */
	url: string;
	/** Absolute path under the resolved local root. */
	path: string;
	size: number;
	mtimeMs: number;
}

/**
 * Every regular file under the session's local:// root, newest first (ties by name); missing root → [].
 * Uncached, unlike completion listings, so pickers see files written moments ago. The walker skips
 * symlinks, so each entry is a real file inside the root.
 */
export async function listLocalFiles(options: LocalProtocolOptions): Promise<LocalFileEntry[]> {
	const root = path.resolve(resolveLocalRoot(options));
	let names: string[];
	try {
		names = await listFilesRecursively(root);
	} catch (err) {
		if (isEnoent(err)) return [];
		throw err;
	}
	const entries = await Promise.all(
		names.map(async (name): Promise<LocalFileEntry | null> => {
			const filePath = path.join(root, name);
			try {
				const stat = await fs.stat(filePath);
				return { name, url: `local://${name}`, path: filePath, size: stat.size, mtimeMs: stat.mtimeMs };
			} catch (err) {
				if (isEnoent(err)) return null;
				throw err;
			}
		}),
	);
	return entries
		.filter((entry): entry is LocalFileEntry => entry !== null)
		.sort((a, b) => b.mtimeMs - a.mtimeMs || a.name.localeCompare(b.name));
}

/** Resolve a local:// URL to an on-disk path under the active session's local root. */
export function resolveLocalUrlToPath(
	input: string | InternalUrl,
	options: LocalProtocolOptions,
	platform: NodeJS.Platform = process.platform,
): string {
	const url = typeof input === "string" ? parseInternalUrl(input) : input;
	const localRoot = path.resolve(resolveLocalRoot(options, platform));
	const relativePath = extractRelativePath(url);

	if (!relativePath) {
		return localRoot;
	}

	const resolved = path.resolve(localRoot, relativePath);
	ensureWithinRoot(resolved, localRoot, "local", url.href);
	return resolved;
}

const LOCAL_WRITE_NOTE = "Session-scoped scratch space; files here live as long as the session.";

type ResolvedLocalTarget =
	| { kind: "listing"; root: string }
	| { kind: "directory"; path: string }
	| { kind: "file"; path: string; size: number };

/**
 * Resolve a local:// URL to its on-disk target with realpath + containment
 * checks on the root, parent, and target so symlinks cannot escape the session
 * local root. Does NOT read or decode file contents — callers decide how to
 * consume the resolved path.
 */
async function resolveLocalTarget(url: InternalUrl, opts: LocalProtocolOptions): Promise<ResolvedLocalTarget> {
	const localRoot = path.resolve(resolveLocalRoot(opts));
	await fs.mkdir(localRoot, { recursive: true });

	let resolvedRoot: string;
	try {
		resolvedRoot = await fs.realpath(localRoot);
	} catch (error) {
		if (isEnoent(error)) {
			throw new Error("Unable to initialize local:// root");
		}
		throw error;
	}

	const relativePath = extractRelativePath(url);
	const targetPath = relativePath ? path.resolve(resolvedRoot, relativePath) : resolvedRoot;
	ensureWithinRoot(targetPath, resolvedRoot, "local", url.href);

	if (targetPath === resolvedRoot) {
		return { kind: "listing", root: resolvedRoot };
	}

	const realTargetPath = await containedRealPath(targetPath, resolvedRoot, "local", url.href);
	if (realTargetPath === undefined) {
		throw new Error(`Local file not found: ${url.href}`);
	}

	const stat = await fs.stat(realTargetPath);
	if (stat.isDirectory()) {
		return { kind: "directory", path: realTargetPath };
	}
	if (!stat.isFile()) {
		throw new Error(`local:// URL must resolve to a file or directory: ${url.href}`);
	}
	return { kind: "file", path: realTargetPath, size: stat.size };
}

/** Realpath of an existing local:// target, or null when it (or the root) does not exist. */
async function locateLocalTarget(url: InternalUrl, opts: LocalProtocolOptions): Promise<string | null> {
	const localRoot = path.resolve(resolveLocalRoot(opts));
	const relativePath = extractRelativePath(url);
	const targetPath = relativePath ? path.resolve(localRoot, relativePath) : localRoot;
	ensureWithinRoot(targetPath, localRoot, "local", url.href);

	let realRoot: string;
	try {
		realRoot = await fs.realpath(localRoot);
	} catch (error) {
		if (!isEnoent(error)) throw error;
		return null;
	}
	const underRealRoot = relativePath ? path.resolve(realRoot, relativePath) : realRoot;
	return (await containedRealPath(underRealRoot, realRoot, "local", url.href)) ?? null;
}

/**
 * Protocol handler for local:// URLs.
 *
 * URL forms:
 * - local:// - Lists files at the session local root
 * - local://<path> - Reads a file under the session local root
 */
export class LocalProtocolHandler implements ProtocolHandler {
	readonly scheme = "local";
	/** Session scratch space: file-backed, so a locate() path is the resource. */
	readonly spec: SchemeSpec = {
		backing: "file",
		selectors: "lines",
		immutable: false,
		pathAuthority: true,
		linkable: true,
		singleSlashAlias: true,
	};

	/** The calling session's `local://` root mapping; absent when the caller has no session. */
	static resolveOptions(context?: ResolveContext): LocalProtocolOptions | undefined {
		return context?.localProtocolOptions;
	}

	async resolve(url: InternalUrl, context?: ResolveContext): Promise<InternalResource> {
		const opts = LocalProtocolHandler.resolveOptions(context);
		if (!opts) {
			throw new Error("No session - local:// unavailable");
		}

		const resolved = await resolveLocalTarget(url, opts);
		if (resolved.kind === "listing") {
			return buildListing(url, resolved.root);
		}
		if (resolved.kind === "directory") {
			return buildDirectoryResource(url.href, resolved.path, [LOCAL_WRITE_NOTE]);
		}

		return buildFileResource(url, resolved);
	}

	async locate(url: InternalUrl, context?: ResolveContext): Promise<string | null> {
		const opts = LocalProtocolHandler.resolveOptions(context);
		if (!opts) {
			throw new Error("No session - local:// unavailable");
		}
		return locateLocalTarget(url, opts);
	}

	locateSync(url: InternalUrl, context?: ResolveContext): string | undefined {
		const opts = LocalProtocolHandler.resolveOptions(context);
		if (!opts) return undefined;
		try {
			return resolveLocalUrlToPath(url, opts);
		} catch {
			return undefined;
		}
	}

	async complete(_query?: string, context?: ResolveContext): Promise<UrlCompletion[]> {
		const opts = LocalProtocolHandler.resolveOptions(context);
		if (!opts) return [];
		const localRoot = path.resolve(resolveLocalRoot(opts));
		try {
			const files = await listCompletionFiles(localRoot);
			return files.map(value => ({ value }));
		} catch (err) {
			if (isEnoent(err)) return [];
			throw err;
		}
	}
}
