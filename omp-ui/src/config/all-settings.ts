/**
 * Every settings domain, in settings-panel order. Importing this module registers every setting;
 * {@link orderedSettings} lists them by domain order, then declaration order within a domain.
 */
import { all, type AnySetting, Setting } from "./registry";
import * as modesSettings from "../modes/settings";
import * as sessionSettings from "../session/settings";
import * as configModelSettings from "./model-settings";

const DOMAINS: readonly Readonly<Record<string, unknown>>[] = [
	configModelSettings,
	modesSettings,
	sessionSettings,
];

let ordered: readonly AnySetting[] | undefined;

/** Every registered setting: domains in panel order, each in declaration order. */
export function orderedSettings(): readonly AnySetting[] {
	if (ordered) return ordered;
	const sequence = new Map(all().map((handle, index) => [handle, index]));
	const seen = new Set<AnySetting>();
	const domainHandles = (domain: Readonly<Record<string, unknown>>): AnySetting[] => {
		const handles: AnySetting[] = [];
		const collect = (value: unknown) => {
			if (value instanceof Setting) {
				if (!seen.has(value)) {
					seen.add(value);
					handles.push(value);
				}
			} else if (value && typeof value === "object" && !Array.isArray(value)) {
				for (const nested of Object.values(value)) collect(nested);
			}
		};
		for (const key in domain) collect(domain[key]);
		return handles.sort((a, b) => (sequence.get(a) ?? 0) - (sequence.get(b) ?? 0));
	};
	const result: AnySetting[] = [];
	for (const domain of DOMAINS) {
		for (const handle of domainHandles(domain)) result.push(handle);
	}
	ordered = result;
	return result;
}
