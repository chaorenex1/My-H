/**
 * Internal URL router: one process-global registry of scheme handlers.
 *
 * A scheme's {@link SchemeSpec} declares how it may be addressed (backing,
 * selector grammar, linkability), so renderers and the composer consult the
 * router instead of branching on scheme names. Handlers are stateless; they
 * read whatever they need from the caller-supplied {@link ResolveContext}.
 */
import { setInternalUrlCompletionHost } from "@oh-my-pi/pi-tui/prompt/internal-url-autocomplete";
import { splitInternalUrlSel } from "@oh-my-pi/pi-tui/tools/read";
import { setInternalUrlSchemeHost, splitUrlScheme } from "@oh-my-pi/pi-tui/tools/url-scheme-host";
import { LocalProtocolHandler } from "./local-protocol";
import { parseInternalUrl } from "./parse";
import type {
	InternalResource,
	InternalUrl,
	ProtocolHandler,
	ResolveContext,
	SchemeSpec,
	UrlCompletion,
} from "./types";

setInternalUrlCompletionHost({
	completionSchemes: () => InternalUrlRouter.instance().completionSchemes(),
	resolveCompletions: (scheme, query, context) => InternalUrlRouter.instance().complete(scheme, query, context),
});

setInternalUrlSchemeHost({
	spec: scheme => InternalUrlRouter.instance().spec(scheme),
	specs: () => InternalUrlRouter.instance().specs(),
});

/** A registered hierarchical URL after single-slash alias normalization. */
interface RegisteredUrl {
	/** Normalized `scheme://…` input. */
	url: string;
	/** Lowercased scheme. */
	scheme: string;
	handler: ProtocolHandler;
}

const SINGLE_SLASH_ALIAS_RE = /^([a-z][a-z0-9+.-]*):\/(?!\/)/i;
// `/home/me/repo/local://x.md` (or `C:\repo\local://x.md`): a filesystem path
// glued in front of a URL. Each match is a separator followed by `scheme://`
// and then a non-slash or the end (the bare `local://` root). Only `://`
// counts, since `dir/local:/x` (single slash) can be a real `local:` directory.
const PREFIXED_URL_SEGMENT_RE = /[\\/]([a-z][a-z0-9+.-]*):\/\/(?=[^/]|$)/gi;
// A prefix that is itself a URL (`https://h/local://x`) is not a filesystem
// path; a Windows drive letter (`C:`) is.
const URL_PREFIX_RE = /^(?![a-z]:[\\/])[a-z][a-z0-9+.-]*:/i;

/** Process-global scheme registry; renderers route internal URLs through its spec-driven API. */
export class InternalUrlRouter {
	static #instance: InternalUrlRouter | undefined;

	#handlers = new Map<string, ProtocolHandler>();

	constructor() {
		this.register(new LocalProtocolHandler());
	}

	/** Process-global router instance. */
	static instance(): InternalUrlRouter {
		InternalUrlRouter.#instance ??= new InternalUrlRouter();
		return InternalUrlRouter.#instance;
	}

	/** Reset the global instance in tests. */
	static resetForTests(): void {
		InternalUrlRouter.#instance = undefined;
	}

	/** Install (or replace) the handler for `handler.scheme`. */
	register(handler: ProtocolHandler): void {
		this.#handlers.set(handler.scheme.toLowerCase(), handler);
	}

	/** Remove a scheme's handler; true when one was registered. */
	unregister(scheme: string): boolean {
		return this.#handlers.delete(scheme.toLowerCase());
	}

	/** Registered handler for `scheme` (case-insensitive). */
	getHandler(scheme: string): ProtocolHandler | undefined {
		return this.#handlers.get(scheme.toLowerCase());
	}

	/** Declared spec of a registered scheme (case-insensitive). */
	spec(scheme: string): SchemeSpec | undefined {
		return this.#handlers.get(scheme.toLowerCase())?.spec;
	}

	/** Specs of every registered scheme, in registration order. */
	specs(): ReadonlyMap<string, SchemeSpec> {
		const specs = new Map<string, SchemeSpec>();
		for (const [scheme, handler] of this.#handlers) specs.set(scheme, handler.spec);
		return specs;
	}

	/**
	 * Rewrite mistyped spellings of a scheme that declares
	 * {@link SchemeSpec.singleSlashAlias} to `scheme://x`: the single-slash alias
	 * (`local:/x`) and a filesystem path prefixed onto the URL
	 * (`/home/me/repo/local://x`). Other inputs pass through.
	 */
	normalize(input: string): string {
		if (!URL_PREFIX_RE.test(input)) {
			// The first eligible scheme wins: later `local://` text belongs to the URL's own path.
			for (const segment of input.matchAll(PREFIXED_URL_SEGMENT_RE)) {
				if (this.#handlers.get(segment[1].toLowerCase())?.spec.singleSlashAlias) {
					return input.slice(segment.index + 1);
				}
			}
		}
		const match = SINGLE_SLASH_ALIAS_RE.exec(input);
		if (!match || !this.#handlers.get(match[1].toLowerCase())?.spec.singleSlashAlias) return input;
		return `${match[1]}://${input.slice(match[0].length)}`;
	}

	/** Whether `input` (after {@link normalize}) is a hierarchical `scheme://` URL of a registered scheme. */
	canHandle(input: string): boolean {
		return this.#registered(input) !== undefined;
	}

	/** Schemes whose handler supports host/path autocomplete. */
	completionSchemes(): string[] {
		const schemes: string[] = [];
		for (const [scheme, handler] of this.#handlers) {
			if (handler.complete) schemes.push(scheme);
		}
		return schemes;
	}

	/**
	 * Candidate completions for the host/path portion of `scheme://<query>`.
	 * Returns `null` when the scheme is unknown or does not support completion.
	 */
	async complete(scheme: string, query: string, context?: ResolveContext): Promise<UrlCompletion[] | null> {
		const handler = this.#handlers.get(scheme.toLowerCase());
		if (!handler?.complete) return null;
		return handler.complete(query, context);
	}

	/**
	 * {@link normalize} `input`, then peel a trailing read selector per
	 * spec.selectors (+portAuthority). Non-URLs and unknown schemes pass through
	 * unchanged.
	 */
	split(input: string): { path: string; sel?: string } {
		return splitInternalUrlSel(this.normalize(input), scheme => this.spec(scheme));
	}

	/**
	 * handler.locate on the URL with its read selector peeled; null when the
	 * scheme has no locate or the URL has no local backing.
	 */
	async locate(input: string, context?: ResolveContext): Promise<string | null> {
		const registered = this.#registered(input);
		if (!registered?.handler.locate) return null;
		return registered.handler.locate(parseInternalUrl(this.split(registered.url).path), context);
	}

	/** Sync locate for renderers; only schemes with spec.linkable. Never throws. */
	locateSync(input: string, context?: ResolveContext): string | undefined {
		const registered = this.#registered(input);
		if (!registered?.handler.spec.linkable || !registered.handler.locateSync) return undefined;
		let parsed: InternalUrl;
		try {
			parsed = parseInternalUrl(this.split(registered.url).path);
		} catch {
			return undefined;
		}
		return registered.handler.locateSync(parsed, context);
	}

	/** Resolve an internal URL through its registered protocol handler. */
	async resolve(input: string, context?: ResolveContext): Promise<InternalResource> {
		const { parsed, handler } = this.#route(input);
		const resource = await handler.resolve(parsed, context);
		return { ...resource, immutable: resource.immutable ?? handler.spec.immutable };
	}

	/** Normalized hierarchical URL of a registered scheme, with its handler; undefined otherwise. */
	#registered(input: string): RegisteredUrl | undefined {
		const url = this.normalize(input);
		const split = splitUrlScheme(url);
		const handler = split && this.#handlers.get(split.scheme);
		return handler && split ? { url, scheme: split.scheme, handler } : undefined;
	}

	#route(input: string): { parsed: InternalUrl; handler: ProtocolHandler } {
		const parsed = parseInternalUrl(this.normalize(input));
		const scheme = parsed.protocol.replace(/:$/, "").toLowerCase();
		const handler = this.#handlers.get(scheme);
		if (!handler) {
			const available = Array.from(this.#handlers.keys())
				.map(candidate => `${candidate}://`)
				.join(", ");
			throw new Error(`Unknown protocol: ${scheme}://\nSupported: ${available || "none"}`);
		}
		return { parsed, handler };
	}
}
